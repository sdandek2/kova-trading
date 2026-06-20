"""
Phase 7I — Institutional positioning via Financial Modeling Prep (FMP) API.

Replaces the original yfinance implementation which got rate-limited (429) by Yahoo
Finance on Railway's cloud IP daily, blocking all conviction boosts for the day.

Uses FMP company profile to get institutional ownership %.
API key env var: FMP_API_KEY (shared with fmp_earnings.py; 250 calls/day free tier)
Cache: 24h per symbol — quarterly 13F data doesn't change intraday.

Signal:
  inst_pct >= 70%  → strong institutional backing  +15 pts
  inst_pct >= 50%  → moderate institutional backing  +8 pts
  inst_pct <  50%  → low institutional interest       0 pts
"""
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://financialmodelingprep.com/api/v3"
_CACHE_TTL = 86_400  # 24h — 13F filings are quarterly; no point re-fetching intraday

_cache: dict[str, tuple[dict, float]] = {}

_KNOWN_ETFS = {
    "SPY","QQQ","IWM","DIA","GLD","SLV","TLT","HYG","LQD","IEF",
    "XLF","XLK","XLE","XLV","XLI","XLY","XLB","XLP","XLU","XLRE",
    "TQQQ","SQQQ","SPXL","SPXS","SOXL","SOXS","UVXY","SVXY","ARKK",
    "ARKW","ARKG","SMH","GDX","USO","IAU","IBIT","GBTC","VOO","VTI",
    "AGG","SOXX","ARKF","ARKE","SCO","OIH","EWY","IUXX",
}
_ETF_SUFFIXES = ("ETF", "ETN", "FUND")


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
        log_connector_call("quiver", status, result.get("details", ""))
    except Exception:
        pass


def _unavailable(reason: str) -> dict:
    return {"signal": "unavailable", "conviction_boost": 0, "details": reason}


def get_darkpool_signal(symbol: str) -> dict:
    """
    Return institutional positioning signal for a symbol using FMP API.

    Returns:
        {
          "signal": "accumulating" | "neutral" | "unavailable",
          "conviction_boost": int,
          "details": str
        }
    """
    cached = _cache.get(symbol)
    if cached and time.time() < cached[1]:
        return cached[0]

    if symbol in _KNOWN_ETFS or any(symbol.endswith(s) for s in _ETF_SUFFIXES):
        return _unavailable("ETF — no institutional holder data")

    api_key = _get_api_key()
    if not api_key:
        result = _unavailable("FMP_API_KEY not set")
        _log_health(result, no_key=True)
        return result

    try:
        resp = httpx.get(
            f"{_API_BASE}/profile/{symbol}",
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

        # institutionalOwnershipPercentage is a 0.0–1.0 decimal in FMP profile
        raw_pct = data.get("institutionalOwnershipPercentage") or 0
        inst_pct = float(raw_pct) * 100

        if inst_pct >= 70:
            result = {
                "signal": "accumulating",
                "conviction_boost": 15,
                "details": f"FMP: institutions hold {inst_pct:.0f}% of float (strong backing)",
            }
        elif inst_pct >= 50:
            result = {
                "signal": "accumulating",
                "conviction_boost": 8,
                "details": f"FMP: institutions hold {inst_pct:.0f}% of float",
            }
        elif inst_pct > 0:
            result = {
                "signal": "neutral",
                "conviction_boost": 0,
                "details": f"FMP: institutions hold {inst_pct:.0f}% of float (below 50% threshold)",
            }
        else:
            result = _unavailable("FMP: institutional ownership data not available")

        _cache[symbol] = (result, time.time() + _CACHE_TTL)
        _log_health(result)
        logger.info("FMP institutional %s: %s", symbol, result.get("details", "ok"))
        return result

    except Exception as e:
        result = _unavailable(str(e))
        _log_health(result)
        logger.warning("FMP institutional %s error: %s", symbol, e)
        return result
