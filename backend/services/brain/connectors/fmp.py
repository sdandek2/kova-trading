"""
Phase 7H — Analyst estimate signals via yfinance (FREE, no API key needed).

Uses yfinance to get analyst price targets and recommendations.
  targetMeanPrice vs currentPrice: > 20% upside  → raised +20 pts
  recommendationKey "buy" or "strong_buy"         → raised +10 pts
  recommendationKey "sell" or "strong_sell"        → lowered -20 pts
  targetMeanPrice downside > 10%                   → lowered -10 pts

Cache: 4 hours per symbol (analyst estimates change slowly).
"""
import logging
import time

logger = logging.getLogger(__name__)

_CACHE_TTL = 14_400  # 4 hours
_cache: dict[str, tuple[dict, float]] = {}


def _unavailable(reason: str) -> dict:
    return {"signal": "unavailable", "conviction_boost": 0, "details": reason}


def get_estimate_revision(symbol: str) -> dict:
    """
    Return analyst estimate signal for a symbol using yfinance.

    Returns:
        {
          "signal": "raised" | "lowered" | "unchanged" | "unavailable",
          "conviction_boost": int,
          "details": str
        }
    """
    cached = _cache.get(symbol)
    if cached and time.time() < cached[1]:
        return cached[0]

    try:
        result = _fetch_yfinance_estimates(symbol)
    except Exception as e:
        logger.debug("yfinance estimates %s: %s", symbol, e)
        result = _unavailable(str(e))

    _cache[symbol] = (result, time.time() + _CACHE_TTL)
    return result


def _fetch_yfinance_estimates(symbol: str) -> dict:
    try:
        import yfinance as yf
    except ImportError:
        return _unavailable("yfinance not installed — run: pip install yfinance")

    ticker = yf.Ticker(symbol)
    info = ticker.info or {}

    current_price  = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0)
    target_mean    = float(info.get("targetMeanPrice") or 0)
    recommendation = str(info.get("recommendationKey") or "").lower()

    if current_price == 0 or target_mean == 0:
        return _unavailable("price or target data not available")

    upside_pct = (target_mean - current_price) / current_price * 100

    # Strong buy signals
    if recommendation in ("strong_buy", "buy") and upside_pct >= 20:
        return {"signal": "raised", "conviction_boost": 20,
                "details": f"analysts: {recommendation}, target ${target_mean:.0f} ({upside_pct:+.0f}% upside)"}

    if recommendation in ("strong_buy", "buy") or upside_pct >= 20:
        return {"signal": "raised", "conviction_boost": 10,
                "details": f"analysts: {recommendation}, target ${target_mean:.0f} ({upside_pct:+.0f}%)"}

    # Sell signals
    if recommendation in ("strong_sell", "sell") and upside_pct <= -10:
        return {"signal": "lowered", "conviction_boost": -20,
                "details": f"analysts: {recommendation}, target ${target_mean:.0f} ({upside_pct:+.0f}%)"}

    if recommendation in ("strong_sell", "sell") or upside_pct <= -10:
        return {"signal": "lowered", "conviction_boost": -10,
                "details": f"analysts: {recommendation}, target ${target_mean:.0f} ({upside_pct:+.0f}%)"}

    return {"signal": "unchanged", "conviction_boost": 0,
            "details": f"analysts: {recommendation or 'hold'}, target ${target_mean:.0f} ({upside_pct:+.0f}%)"}
