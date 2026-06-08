"""
Phase 7I — Institutional positioning via yfinance (FREE, no API key needed).

Uses yfinance institutional holders to approximate dark pool accumulation.
Real dark pool data is delayed/expensive; institutional holder changes are a
reasonable free proxy — large funds disclose positions quarterly via 13F.

Signal: top 10 institutional holders as % of float
  inst_pct >= 70%  → strong institutional backing +15 pts
  inst_pct >= 50%  → moderate institutional backing +8 pts
  inst_pct <  20%  → low institutional interest (retail-driven) 0 pts

Note: this data is quarterly (SEC 13F), not real-time. Lower conviction than
real dark pool data, but still useful for filtering out thinly-held stocks.
Cache: 24 hours per symbol.

Railway IP note: Yahoo Finance rate-limits cloud IPs (429). A daily circuit
breaker stops all calls after the first 429, retrying the next calendar day.
"""
import logging
import time
from datetime import date

logger = logging.getLogger(__name__)

# Silence yfinance's own noisy internal logger — it logs every 429 retry as ERROR
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

_CACHE_TTL = 86_400

# Daily circuit breaker — stop calling Yahoo after first 429, reset next day
_blocked_date: date | None = None

_cache: dict[str, tuple[dict, float]] = {}

_ETF_SUFFIXES = ("ETF", "ETN", "FUND")
_KNOWN_ETFS = {
    "SPY","QQQ","IWM","DIA","GLD","SLV","TLT","HYG","LQD","IEF",
    "XLF","XLK","XLE","XLV","XLI","XLY","XLB","XLP","XLU","XLRE",
    "TQQQ","SQQQ","SPXL","SPXS","SOXL","SOXS","UVXY","SVXY","ARKK",
    "ARKW","ARKG","SMH","GDX","USO","IAU","IBIT","GBTC","VOO","VTI",
    "AGG","SOXX","ARKF","ARKE","SCO","OIH","EWY","IUXX",
}


def _log_health(result: dict) -> None:
    try:
        from services.db import log_connector_call
        status = "ok" if result.get("signal") not in ("unavailable",) else "unavailable"
        log_connector_call("quiver", status, result.get("details", ""))
    except Exception:
        pass


def _unavailable(reason: str) -> dict:
    return {"signal": "unavailable", "conviction_boost": 0, "details": reason}


def get_darkpool_signal(symbol: str) -> dict:
    """
    Return institutional positioning signal for a symbol using yfinance.

    Returns:
        {
          "signal": "accumulating" | "neutral" | "unavailable",
          "conviction_boost": int,
          "details": str
        }
    """
    global _blocked_date

    cached = _cache.get(symbol)
    if cached and time.time() < cached[1]:
        return cached[0]

    # ETFs and index products have no institutional holder data
    if symbol in _KNOWN_ETFS or any(symbol.endswith(s) for s in _ETF_SUFFIXES):
        result = _unavailable("ETF — no institutional holder data")
        _cache[symbol] = (result, time.time() + _CACHE_TTL)
        return result

    # Daily circuit breaker — Yahoo rate-limits Railway IP after a burst
    today = date.today()
    if _blocked_date == today:
        return _unavailable("Yahoo Finance rate-limited today — retry tomorrow")

    try:
        result = _fetch_institutional_data(symbol)
        if result.get("signal") != "unavailable":
            logger.info("yfinance institutional %s: %s", symbol, result.get("details", "ok"))
        else:
            logger.debug("yfinance institutional %s unavailable: %s", symbol, result.get("details", ""))
    except Exception as e:
        logger.warning("yfinance institutional %s error: %s", symbol, e)
        result = _unavailable(str(e))

    _cache[symbol] = (result, time.time() + _CACHE_TTL)
    _log_health(result)
    return result


def _fetch_institutional_data(symbol: str) -> dict:
    global _blocked_date

    try:
        import yfinance as yf
    except ImportError as e:
        return _unavailable(f"yfinance import failed: {e}")
    except Exception as e:
        return _unavailable(f"yfinance load error: {type(e).__name__}: {e}")

    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
    except Exception as e:
        err = str(e)
        if "429" in err or "Too Many Requests" in err:
            _blocked_date = date.today()
            logger.warning("yfinance: Yahoo Finance 429 — circuit breaker engaged for today")
            return _unavailable("Yahoo Finance rate-limited (429) — blocked for today")
        return _unavailable(f"yfinance fetch error: {err}")

    # institutionsPercentHeld is 0.0-1.0
    inst_pct = float(info.get("institutionsPercentHeld") or 0) * 100

    if inst_pct == 0:
        # Fallback: try to derive from institutional holders DataFrame
        try:
            holders = ticker.institutional_holders
            if holders is not None and not holders.empty and "% Out" in holders.columns:
                inst_pct = float(holders["% Out"].sum()) * 100
        except Exception:
            pass

    if inst_pct == 0:
        return _unavailable("institutional data not available")

    if inst_pct >= 70:
        return {"signal": "accumulating", "conviction_boost": 15,
                "details": f"institutions hold {inst_pct:.0f}% of float (strong backing)"}

    if inst_pct >= 50:
        return {"signal": "accumulating", "conviction_boost": 8,
                "details": f"institutions hold {inst_pct:.0f}% of float"}

    return {"signal": "neutral", "conviction_boost": 0,
            "details": f"institutions hold {inst_pct:.0f}% of float (below 50% threshold)"}
