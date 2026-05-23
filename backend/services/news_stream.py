import asyncio
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import websockets

from config import settings
from services.db import cache_get, cache_set, log_bot_activity

logger = logging.getLogger(__name__)

ALPACA_NEWS_WS_URL = "wss://stream.data.alpaca.markets/v1beta1/news"
NEWS_CACHE_KEY = "news:realtime"
NEWS_CACHE_TTL_SECONDS = 24 * 3600
MAX_CACHED_ARTICLES = 250

_task: Optional[asyncio.Task] = None
_running = False
_last_trigger_at: dict[str, datetime] = {}
_last_global_trigger_at: Optional[datetime] = None

_BULLISH_PATTERNS = {
    "analyst_upgrade": r"\b(upgrade|upgraded|raises? price target|initiated .*buy|outperform)\b",
    "earnings_beat": r"\b(earnings beat|beats estimates|beats expectations|better-than-expected|record revenue)\b",
    "guidance_raise": r"\b(raises? guidance|boosts? outlook|raises? forecast|increases? outlook)\b",
    "mna": r"\b(acquire|acquires|acquisition|merger|buyout|takeover|strategic alternatives)\b",
    "fda_positive": r"\b(fda approval|fda approves|phase 3 met|positive trial|clinical trial success)\b",
}

_BEARISH_PATTERNS = {
    "analyst_downgrade": r"\b(downgrade|downgraded|cuts? price target|underperform|sell rating)\b",
    "earnings_miss": r"\b(earnings miss|misses estimates|misses expectations|weaker-than-expected)\b",
    "guidance_cut": r"\b(cuts? guidance|lowers? outlook|reduces? forecast|withdraws? guidance)\b",
    "dilution": r"\b(share offering|stock offering|secondary offering|registered direct|atm offering|dilution)\b",
    "legal_probe": r"\b(sec investigation|doj investigation|lawsuit|class action|fraud probe|subpoena)\b",
    "fda_negative": r"\b(fda rejection|complete response letter|clinical hold|trial failed|missed endpoint)\b",
}

_HIGH_IMPACT_TYPES = {
    "analyst_upgrade",
    "analyst_downgrade",
    "earnings_beat",
    "earnings_miss",
    "guidance_raise",
    "guidance_cut",
    "dilution",
    "mna",
    "fda_positive",
    "fda_negative",
    "legal_probe",
}


def _parse_dt(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


def _score_event(headline: str, summary: str = "") -> dict[str, Any]:
    text = f"{headline} {summary}".lower()
    event_types: list[str] = []
    score = 0

    for event_type, pattern in _BULLISH_PATTERNS.items():
        if re.search(pattern, text, flags=re.IGNORECASE):
            event_types.append(event_type)
            score += 3

    for event_type, pattern in _BEARISH_PATTERNS.items():
        if re.search(pattern, text, flags=re.IGNORECASE):
            event_types.append(event_type)
            score -= 3

    if "halt" in text or "trading halted" in text:
        event_types.append("halt")
        score -= 4

    impact = "high" if any(t in _HIGH_IMPACT_TYPES or t == "halt" for t in event_types) else "normal"
    sentiment = "bullish" if score > 0 else "bearish" if score < 0 else "neutral"
    return {
        "event_types": event_types,
        "event_score": score,
        "event_impact": impact,
        "event_sentiment": sentiment,
    }


def _normalize_message(message: dict[str, Any]) -> Optional[dict[str, Any]]:
    if message.get("T") != "n":
        return None

    headline = (message.get("headline") or "").strip()
    if not headline:
        return None

    summary = (message.get("summary") or "").strip()
    symbols = [s for s in (message.get("symbols") or []) if isinstance(s, str)]
    scored = _score_event(headline, summary)
    created_at = message.get("created_at") or datetime.now(timezone.utc).isoformat()

    return {
        "id": str(message.get("id") or f"{headline}:{created_at}"),
        "headline": headline,
        "summary": summary[:400],
        "author": message.get("author") or "",
        "created_at": created_at,
        "url": message.get("url") or "",
        "symbols": symbols,
        "source": message.get("source") or "Alpaca/Benzinga",
        **scored,
    }


def _load_cache() -> list[dict[str, Any]]:
    cached = cache_get(NEWS_CACHE_KEY)
    return cached if isinstance(cached, list) else []


def get_cached_news(limit: int = 100, max_age_minutes: Optional[int] = None) -> list[dict[str, Any]]:
    articles = _load_cache()
    if max_age_minutes is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        articles = [a for a in articles if _parse_dt(a.get("created_at")) >= cutoff]
    return articles[:limit]


def _save_article(article: dict[str, Any]) -> None:
    articles = _load_cache()
    seen = {str(a.get("id")) for a in articles}
    if str(article.get("id")) in seen:
        return

    articles.insert(0, article)
    articles = articles[:MAX_CACHED_ARTICLES]
    cache_set(NEWS_CACHE_KEY, articles, NEWS_CACHE_TTL_SECONDS)


def _should_trigger(article: dict[str, Any]) -> bool:
    global _last_global_trigger_at
    symbols = article.get("symbols") or []
    if not symbols or article.get("event_impact") != "high":
        return False

    now = datetime.now(timezone.utc)
    if _last_global_trigger_at and (now - _last_global_trigger_at).total_seconds() < 120:
        return False

    for symbol in symbols[:5]:
        key = f"{symbol}:{','.join(article.get('event_types') or [])}"
        last = _last_trigger_at.get(key)
        if last and (now - last).total_seconds() < 15 * 60:
            return False
        _last_trigger_at[key] = now
    _last_global_trigger_at = now
    return True


async def _handle_article(article: dict[str, Any]) -> None:
    _save_article(article)

    if _should_trigger(article):
        symbols = article.get("symbols") or []
        message = (
            f"High-impact news detected for {', '.join(symbols[:5])}: "
            f"{article['headline']} [{article.get('event_sentiment')}]"
        )
        log_bot_activity("news_trigger", message, symbol=symbols[0] if symbols else None)
        try:
            from services import trading_engine

            trading_engine.request_urgent_cycle(symbols=symbols, reason=message)
        except Exception as exc:
            logger.warning(f"Could not request urgent trading cycle: {exc}")


async def _stream_loop() -> None:
    backoff = 1
    while _running:
        try:
            async with websockets.connect(ALPACA_NEWS_WS_URL, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({
                    "action": "auth",
                    "key": settings.alpaca_api_key,
                    "secret": settings.alpaca_secret_key,
                }))
                await ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
                logger.info("Alpaca news stream connected.")
                backoff = 1

                async for raw in ws:
                    payload = json.loads(raw)
                    messages = payload if isinstance(payload, list) else [payload]
                    for message in messages:
                        article = _normalize_message(message)
                        if article:
                            await _handle_article(article)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"Alpaca news stream disconnected: {exc}. Reconnecting in {backoff}s.")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def start() -> None:
    global _running, _task
    if _running:
        return
    _running = True
    _task = asyncio.create_task(_stream_loop())
    logger.info("News stream started.")


def stop() -> None:
    global _running, _task
    _running = False
    if _task:
        _task.cancel()
        _task = None
    logger.info("News stream stopped.")
