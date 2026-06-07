"""
Session 6 — Analyst Recommendation Trends via Finnhub (free, no API key beyond free tier).

Measures whether Wall Street analysts are collectively upgrading or downgrading a stock.
This is the revision DIRECTION signal — not the current rating, but whether consensus
is shifting bullish or bearish month-over-month.

Free tier: 60 calls/minute. This module uses ~1 call per symbol, cached 24h.
API key env var: FINNHUB_API_KEY

Signal (comparing current month vs last month):
  strongBuy+buy count grew  → analysts upgrading → +10 conviction pts
  strongBuy+buy count shrank → analysts downgrading → -10 conviction pts

Why this matters: when multiple analysts raise their rating in the same period,
institutions follow their own analysts' models — money flows in within 1-4 weeks.
"""
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://finnhub.io/api/v1"
_CACHE_TTL = 86_400  # 24 hours — analyst ratings update monthly
_cache: dict[str, dict] = {}


def _get_api_key() -> str:
    try:
        from config import settings
        return getattr(settings, "finnhub_api_key", "") or os.environ.get("FINNHUB_API_KEY", "")
    except Exception:
        return os.environ.get("FINNHUB_API_KEY", "")


def get_recommendation_signal(symbol: str) -> dict:
    """
    Return analyst recommendation trend signal for a symbol.

    Returns:
        {
          "signal": "upgrading" | "downgrading" | "neutral" | "unavailable",
          "conviction_boost": int,
          "details": str
        }
    """
    sym = symbol.upper().strip()
    cached = _cache.get(sym)
    if cached and time.time() < cached.get("_expires", 0):
        return {k: v for k, v in cached.items() if not k.startswith("_")}

    api_key = _get_api_key()
    if not api_key:
        return {"signal": "unavailable", "conviction_boost": 0, "details": "FINNHUB_API_KEY not set"}

    try:
        resp = httpx.get(
            f"{_API_BASE}/stock/recommendation",
            params={"symbol": sym, "token": api_key},
            timeout=10,
            headers={"User-Agent": "Kova Trading kova@trading.com"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.debug("Finnhub recommendation %s: %s", sym, e)
        result = {"signal": "unavailable", "conviction_boost": 0, "details": str(e)}
        _cache[sym] = {**result, "_expires": time.time() + 300}  # short cache on error
        return result

    if not data or len(data) < 2:
        result = {"signal": "neutral", "conviction_boost": 0, "details": "insufficient data (need 2+ months)"}
        _cache[sym] = {**result, "_expires": time.time() + _CACHE_TTL}
        return result

    # data[0] = most recent month, data[1] = previous month
    current = data[0]
    previous = data[1]

    current_bullish = current.get("strongBuy", 0) + current.get("buy", 0)
    previous_bullish = previous.get("strongBuy", 0) + previous.get("buy", 0)
    current_bearish = current.get("strongSell", 0) + current.get("sell", 0)
    previous_bearish = previous.get("strongSell", 0) + previous.get("sell", 0)

    bullish_delta = current_bullish - previous_bullish  # positive = more upgrades
    bearish_delta = current_bearish - previous_bearish  # positive = more downgrades

    period = current.get("period", "")

    if bullish_delta >= 2 and bullish_delta > bearish_delta:
        boost = 10
        signal = "upgrading"
        detail = (f"analysts upgrading: {previous_bullish}→{current_bullish} buy/strongBuy "
                  f"({bullish_delta:+d} change) as of {period}")
    elif bearish_delta >= 2 and bearish_delta > bullish_delta:
        boost = -10
        signal = "downgrading"
        detail = (f"analysts downgrading: {previous_bearish}→{current_bearish} sell/strongSell "
                  f"({bearish_delta:+d} change) as of {period}")
    elif bullish_delta == 1:
        boost = 5
        signal = "upgrading"
        detail = (f"slight analyst upgrade: {previous_bullish}→{current_bullish} buy/strongBuy as of {period}")
    elif bearish_delta == 1:
        boost = -5
        signal = "downgrading"
        detail = (f"slight analyst downgrade: {previous_bearish}→{current_bearish} sell/strongSell as of {period}")
    else:
        boost = 0
        signal = "neutral"
        detail = (f"stable analyst consensus: {current_bullish} buy, "
                  f"{current.get('hold', 0)} hold, {current_bearish} sell as of {period}")

    result = {"signal": signal, "conviction_boost": boost, "details": detail}
    _cache[sym] = {**result, "_expires": time.time() + _CACHE_TTL}
    return result
