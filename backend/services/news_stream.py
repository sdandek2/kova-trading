"""
Real-time news stream via Alpaca Benzinga WebSocket.
wss://stream.data.alpaca.markets/v1beta1/news

When high-impact news is detected, calls trading_engine.request_urgent_cycle()
which sets an asyncio.Event — the trading loop wakes immediately instead of
waiting the full 10-minute interval. News is processed within seconds.

Pattern detection: earnings beats/misses, M&A, FDA, analyst upgrades/downgrades,
CEO changes, bankruptcies, dividend raises, product launches.

Cache: stores last 250 articles (24h TTL) so alpaca_service.get_news() can
pull from the stream cache for richer context in the next cycle.

Auto-reconnect with exponential backoff (max 60s between retries).
"""
import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_NEWS_WS_URL = "wss://stream.data.alpaca.markets/v1beta1/news"

# ── Article cache (shared with alpaca_service.get_news) ──────────────────────
_cache: list[dict] = []          # most-recent-first
_cache_max  = 250
_cache_ttl  = 86_400             # 24 hours

# ── Dedup ─────────────────────────────────────────────────────────────────────
_seen: dict[str, float] = {}     # {headline_hash: expiry_epoch}
_DEDUP_WINDOW = 1_800            # 30 minutes

# ── Rate limiting ─────────────────────────────────────────────────────────────
_last_global_trigger: float = 0.0
_last_symbol_trigger: dict[str, float] = {}
_GLOBAL_THROTTLE  = 120          # min seconds between global triggers
_SYMBOL_THROTTLE  = 900          # min seconds between triggers for same symbol

# ── Impact scoring ────────────────────────────────────────────────────────────
_PATTERNS: list[tuple[int, str, list[str]]] = [
    (90, "bullish",  ["acqui", "merger", "buyout", "takeover", "going private", "to be acquired"]),
    (85, "bearish",  ["bankruptcy", "chapter 11", "delisting", "fraud", "sec charges", "restatement"]),
    (80, "bullish",  ["fda approv", "fda grants", "breakthrough therapy"]),
    (80, "bearish",  ["fda reject", "fda complete response", "clinical hold"]),
    (75, "bullish",  ["earnings beat", "beats estimates", "above consensus",
                      "raises guidance", "raises outlook", "record revenue", "record profit"]),
    (75, "bearish",  ["earnings miss", "misses estimates", "below consensus",
                      "cuts guidance", "lowers outlook", "withdraws guidance"]),
    (65, "bullish",  ["upgrade", "initiated with buy", "strong buy",
                      "price target raised", "price target increased"]),
    (65, "bearish",  ["downgrade", "initiated with sell", "strong sell",
                      "price target cut", "price target lowered"]),
    (60, "bearish",  ["ceo resign", "ceo fired", "ceo steps down", "cfo resign"]),
    (55, "bullish",  ["dividend increase", "special dividend", "buyback", "share repurchase"]),
    (45, "bullish",  ["partnership", "contract win", "wins contract", "new product launch"]),
]

_HIGH_IMPACT_THRESHOLD = 65      # score >= this → urgent cycle

# ── Trading windows (ET) ──────────────────────────────────────────────────────
_ET = ZoneInfo("America/New_York")
_PRE_MARKET_START  = dtime(8, 30)   # 8:30 AM ET
_MARKET_OPEN       = dtime(9, 30)   # 9:30 AM ET
_MARKET_CLOSE      = dtime(16, 0)   # 4:00 PM ET
_AFTER_HOURS_END   = dtime(18, 0)   # 6:00 PM ET


def _get_trading_window() -> Optional[str]:
    """
    Returns the current trading window or None if outside all windows.
      "premarket"   — 8:30–9:30 AM ET  → limit orders only
      "regular"     — 9:30 AM–4:00 PM ET → normal market orders
      "afterhours"  — 4:00–6:00 PM ET  → limit orders, earnings plays only
      None          — outside all windows → cache article, skip trigger
    """
    now_et = datetime.now(_ET).time()
    if _PRE_MARKET_START <= now_et < _MARKET_OPEN:
        return "premarket"
    if _MARKET_OPEN <= now_et < _MARKET_CLOSE:
        return "regular"
    if _MARKET_CLOSE <= now_et < _AFTER_HOURS_END:
        return "afterhours"
    return None


def _score(headline: str) -> tuple[int, str]:
    """Return (score, sentiment) for a headline."""
    text = headline.lower()
    best_score, best_sent = 0, "neutral"
    for score, sentiment, keywords in _PATTERNS:
        if any(kw in text for kw in keywords):
            if score > best_score:
                best_score, best_sent = score, sentiment
    return best_score, best_sent


def _headline_hash(headline: str) -> str:
    return hashlib.md5(headline.encode()).hexdigest()[:12]


def _clean_seen() -> None:
    now = time.time()
    for k in [k for k, v in _seen.items() if v < now]:
        del _seen[k]


def get_cached_news(symbols: Optional[list[str]] = None) -> list[dict]:
    """
    Return cached articles from the live stream.
    Called by alpaca_service.get_news() to supplement REST results.
    """
    now = time.time()
    fresh = [a for a in _cache if a.get("_cached_at", 0) + _cache_ttl > now]
    if symbols:
        sym_set = set(s.upper() for s in symbols)
        return [a for a in fresh if sym_set.intersection(set(a.get("symbols", [])))]
    return fresh


def _add_to_cache(article: dict) -> None:
    article["_cached_at"] = time.time()
    _cache.insert(0, article)
    if len(_cache) > _cache_max:
        _cache.pop()


async def _handle_article(article: dict) -> None:
    """Process one article — score it, cache it, trigger cycle if high-impact."""
    global _last_global_trigger

    headline = article.get("headline", "")
    if not headline:
        return

    _add_to_cache(article)

    h_hash = _headline_hash(headline)
    _clean_seen()
    if h_hash in _seen:
        return
    _seen[h_hash] = time.time() + _DEDUP_WINDOW

    score, sentiment = _score(headline)
    symbols = article.get("symbols", [])

    logger.debug("News [%s score=%d]: %s", sentiment, score, headline[:80])

    if score < _HIGH_IMPACT_THRESHOLD:
        return

    # Check trading window — suppress trigger outside market hours
    window = _get_trading_window()
    if window is None:
        logger.debug("News trigger suppressed (outside trading hours): %s", headline[:60])
        return  # article already cached above; will be used in next regular cycle

    # After-hours: only fire for earnings-related news (score 75+)
    if window == "afterhours" and score < 75:
        logger.debug("News trigger suppressed (after-hours, non-earnings): %s", headline[:60])
        return

    # Global rate limit
    now = time.time()
    if now - _last_global_trigger < _GLOBAL_THROTTLE:
        logger.debug("News trigger rate-limited (global): %s", headline[:60])
        return

    # Per-symbol rate limit (check all symbols in article)
    throttled_syms = [s for s in symbols if now - _last_symbol_trigger.get(s, 0) < _SYMBOL_THROTTLE]
    if symbols and len(throttled_syms) == len(symbols):
        logger.debug("News trigger rate-limited (per-symbol): %s", headline[:60])
        return

    # Fire the urgent cycle
    _last_global_trigger = now
    for s in symbols:
        _last_symbol_trigger[s] = now

    reason = f"[{sentiment.upper()} score={score} window={window}] {', '.join(symbols[:3])}: {headline[:80]}"
    logger.info("🚨 Breaking news — urgent cycle triggered | %s", reason)

    try:
        from services.trading_engine import request_urgent_cycle
        request_urgent_cycle(symbols=symbols, reason=reason, trading_window=window)
    except Exception as e:
        logger.warning("Could not trigger urgent cycle: %s", e)

    try:
        from services.db import log_bot_activity
        log_bot_activity("news_trigger", f"Breaking news: {reason}")
    except Exception:
        pass


async def _run_stream() -> None:
    """Connect to Alpaca news WebSocket and process messages."""
    try:
        import websockets as _ws_lib
    except ImportError:
        logger.error("websockets package not available — news stream disabled")
        return

    from config import settings

    retry_delay = 5.0

    while True:
        try:
            logger.info("News stream: connecting to %s", _NEWS_WS_URL)
            async with _ws_lib.connect(
                _NEWS_WS_URL,
                extra_headers={"Content-Type": "application/json"},
                ping_interval=30,
                ping_timeout=60,
            ) as ws:
                # Wait for server's "connected" handshake before sending auth.
                # Alpaca times out auth if we send before the server is ready.
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                msgs = json.loads(raw) if isinstance(json.loads(raw), list) else [json.loads(raw)]
                if not any(m.get("T") == "connected" for m in msgs):
                    logger.warning("News stream: expected connected msg, got %s", raw[:100])

                # Auth
                await ws.send(json.dumps({
                    "action": "auth",
                    "key":    settings.alpaca_api_key,
                    "secret": settings.alpaca_secret_key,
                }))

                # Wait for authenticated confirmation before subscribing
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                msgs = json.loads(raw) if isinstance(json.loads(raw), list) else [json.loads(raw)]
                if any(m.get("T") == "error" for m in msgs):
                    err = next(m for m in msgs if m.get("T") == "error")
                    raise RuntimeError(f"Auth rejected: {err}")

                # Subscribe to all news
                await ws.send(json.dumps({
                    "action": "subscribe",
                    "news":   ["*"],
                }))

                retry_delay = 5.0  # reset on successful connect
                logger.info("News stream: connected and subscribed")

                async for raw_msg in ws:
                    try:
                        messages = json.loads(raw_msg)
                        if not isinstance(messages, list):
                            messages = [messages]
                        for msg in messages:
                            msg_type = msg.get("T") or msg.get("type")
                            if msg_type == "n":          # news article
                                article = {
                                    "headline": msg.get("headline", ""),
                                    "symbols":  msg.get("symbols", []),
                                    "source":   msg.get("source", ""),
                                    "url":      msg.get("url", ""),
                                    "created_at": msg.get("created_at", ""),
                                }
                                await _handle_article(article)
                            elif msg_type in ("connected", "authenticated", "subscription"):
                                logger.debug("News stream: %s", msg)
                            elif msg_type == "error":
                                logger.warning("News stream error msg: %s", msg)
                    except Exception as e:
                        logger.debug("News stream message parse error: %s", e)

        except Exception as e:
            logger.warning("News stream disconnected (%s) — retrying in %.0fs", e, retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60.0)  # exponential backoff, max 60s


async def start() -> None:
    """Start the news stream as a background task. Called by trading_engine."""
    asyncio.create_task(_run_stream())
    logger.info("News stream task created.")
