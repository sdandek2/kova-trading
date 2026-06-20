"""
Analyst estimate signals via Financial Modeling Prep (FMP) API.

Replaces the original yfinance implementation which got rate-limited (429) by Yahoo
Finance on Railway's cloud IP daily, zeroing out all analyst conviction boosts.

Uses FMP price-target-summary to measure analyst consensus vs current price.
API key env var: FMP_API_KEY (shared with fmp_earnings.py; 250 calls/day free tier)
Cache: 24h per symbol — analyst targets update at most once daily.

Signal (analyst consensus target vs current stock price):
  upside >  20%  → analysts strongly bullish  +20 pts
  upside >  10%  → analysts moderately bullish +10 pts
  downside > 10% → analysts bearish           -20 pts
  else           → unchanged                    0 pts
"""
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://financialmodelingprep.com/stable"
_CACHE_TTL = 86_400  # 24h — analyst targets don't change intraday
_cache: dict[str, tuple[dict, float]] = {}


def _get_api_key() -> str:
    try:
        from config import settings
        return getattr(settings, "fmp_api_key", "") or os.environ.get("FMP_API_KEY", "")
    except Exception:
        return os.environ.get("FMP_API_KEY", "")


def _log_health(result: dict, no_key: bool = False) -> None:
    try:
        from services.db import log_connector_call
        status = "no_key" if no_key else ("ok" if result.get("signal") != "unavailable" else "unavailable")
        log_connector_call("fmp", status, result.get("details", ""))
    except Exception:
        pass


def _unavailable(reason: str) -> dict:
    return {"signal": "unavailable", "conviction_boost": 0, "details": reason}


def get_estimate_revision(symbol: str, current_price: float = 0.0) -> dict:
    """
    Return analyst estimate signal for a symbol using FMP price target API.

    Args:
        symbol: ticker symbol
        current_price: current stock price for computing upside vs analyst target.
                       Passed in from signals.py which already has the price.

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

    api_key = _get_api_key()
    if not api_key:
        result = _unavailable("FMP_API_KEY not set")
        _log_health(result, no_key=True)
        return result

    try:
        resp = httpx.get(
            f"{_API_BASE}/price-target-summary/{symbol}",
            params={"apikey": api_key},
            timeout=10,
            headers={"User-Agent": "Kova Trading kova@trading.com"},
        )
        if resp.status_code in (401, 403):
            result = _unavailable(f"FMP auth error {resp.status_code}")
            _log_health(result)
            return result
        if resp.status_code != 200:
            result = _unavailable(f"FMP HTTP {resp.status_code}")
            _cache[symbol] = (result, time.time() + 3600)
            _log_health(result)
            return result

        data = resp.json()
        if isinstance(data, list):
            data = data[0] if data else {}

        target_consensus = float(data.get("targetConsensus") or 0)

        if target_consensus <= 0:
            result = _unavailable("no analyst price target data from FMP")
            _cache[symbol] = (result, time.time() + _CACHE_TTL)
            _log_health(result)
            return result

        if current_price <= 0:
            result = _unavailable("current price unavailable — cannot compute analyst upside")
            _cache[symbol] = (result, time.time() + _CACHE_TTL)
            _log_health(result)
            return result

        upside = (target_consensus - current_price) / current_price

        if upside > 0.20:
            result = {
                "signal": "raised",
                "conviction_boost": 20,
                "details": f"FMP: analyst consensus ${target_consensus:.0f} is +{upside:.0%} above current",
            }
        elif upside > 0.10:
            result = {
                "signal": "raised",
                "conviction_boost": 10,
                "details": f"FMP: analyst target ${target_consensus:.0f} is +{upside:.0%} above current",
            }
        elif upside < -0.10:
            result = {
                "signal": "lowered",
                "conviction_boost": -20,
                "details": f"FMP: analyst target ${target_consensus:.0f} is {upside:.0%} below current (bearish consensus)",
            }
        else:
            result = {
                "signal": "unchanged",
                "conviction_boost": 0,
                "details": f"FMP: analyst target ${target_consensus:.0f} ({upside:+.0%} from current — neutral)",
            }

        _cache[symbol] = (result, time.time() + _CACHE_TTL)
        _log_health(result)
        logger.info("FMP price-target %s: %s", symbol, result.get("details", "ok"))
        return result

    except Exception as e:
        result = _unavailable(str(e))
        _log_health(result)
        logger.warning("FMP price-target %s error: %s", symbol, e)
        return result
