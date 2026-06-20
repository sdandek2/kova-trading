"""
Phase 7I — Trend strength confirmation via FMP stable quote API.

Original yfinance-based institutional holder lookup was 429-blocked daily on
Railway's cloud IP. FMP free tier doesn't expose institutional ownership either.

Instead, uses FMP stable quote to measure trend strength signals not otherwise
available in signals.py (which only computes MA20):
  MA50, MA200, 52-week high proximity → structural accumulation proxy

Signal:
  within 5% of 52-week high + above MA50 + MA200  → strong trend  +15 pts
  above MA50 + golden cross (MA50 > MA200)          → solid trend   +8 pts
  else                                               → neutral        0 pts

API key env var: FMP_API_KEY (250 calls/day free tier; shared with fmp_earnings.py)
Cache: 4h per symbol.
"""
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://financialmodelingprep.com/stable"
_CACHE_TTL = 86_400  # 24h — MA50/MA200 are daily values, no point re-fetching intraday

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
    Return trend strength signal for a symbol using FMP stable quote (MA50/MA200/52w-high).

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

    # ETFs are often near 52-week highs in bull markets — suppress false positives
    if symbol in _KNOWN_ETFS or any(symbol.endswith(s) for s in _ETF_SUFFIXES):
        return _unavailable("ETF — trend strength filter skipped")

    api_key = _get_api_key()
    if not api_key:
        result = _unavailable("FMP_API_KEY not set")
        _log_health(result, no_key=True)
        return result

    try:
        resp = httpx.get(
            f"{_API_BASE}/quote",
            params={"symbol": symbol, "apikey": api_key},
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

        price = float(data.get("price") or 0)
        year_high = float(data.get("yearHigh") or 0)
        ma50 = float(data.get("priceAvg50") or 0)
        ma200 = float(data.get("priceAvg200") or 0)

        if price <= 0 or year_high <= 0 or ma50 <= 0:
            result = _unavailable("FMP: quote data incomplete")
            _cache[symbol] = (result, time.time() + _CACHE_TTL)
            _log_health(result)
            return result

        pct_from_52w_high = (price - year_high) / year_high  # negative number
        above_50ma = price > ma50
        above_200ma = ma200 > 0 and price > ma200
        golden_cross = ma200 > 0 and ma50 > ma200

        if pct_from_52w_high >= -0.05 and above_50ma and above_200ma:
            result = {
                "signal": "accumulating",
                "conviction_boost": 8,
                "details": (
                    f"FMP: {pct_from_52w_high:.0%} from 52w-high, "
                    f"above MA50(${ma50:.0f}) + MA200(${ma200:.0f})"
                ),
            }
        elif above_50ma and golden_cross:
            result = {
                "signal": "accumulating",
                "conviction_boost": 4,
                "details": f"FMP: above MA50(${ma50:.0f}), golden cross (MA50 > MA200)",
            }
        else:
            result = {
                "signal": "neutral",
                "conviction_boost": 0,
                "details": (
                    f"FMP: {'below MA50' if not above_50ma else 'no golden cross'}"
                    f" (price=${price:.2f}, MA50=${ma50:.0f}, MA200=${ma200:.0f})"
                ),
            }

        _cache[symbol] = (result, time.time() + _CACHE_TTL)
        _log_health(result)
        logger.info("FMP trend-strength %s: %s", symbol, result.get("details", "ok"))
        return result

    except Exception as e:
        result = _unavailable(str(e))
        _log_health(result)
        logger.warning("FMP trend-strength %s error: %s", symbol, e)
        return result
