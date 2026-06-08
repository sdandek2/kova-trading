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
"""
import logging
import time

logger = logging.getLogger(__name__)

_CACHE_TTL = 86_400


def _log_health(result: dict) -> None:
    try:
        from services.db import log_connector_call
        status = "ok" if result.get("signal") not in ("unavailable",) else "unavailable"
        log_connector_call("quiver", status, result.get("details", ""))
    except Exception:
        pass  # 24 hours (quarterly data doesn't need frequent refresh)
_cache: dict[str, tuple[dict, float]] = {}


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
    cached = _cache.get(symbol)
    if cached and time.time() < cached[1]:
        return cached[0]

    # ETFs and leveraged products have no institutional holder data on Yahoo Finance
    # — skip immediately rather than making a doomed API call
    _ETF_SUFFIXES = ("ETF", "ETN", "FUND")
    _KNOWN_ETFS = {
        "SPY","QQQ","IWM","DIA","GLD","SLV","TLT","HYG","LQD","IEF",
        "XLF","XLK","XLE","XLV","XLI","XLY","XLB","XLP","XLU","XLRE",
        "TQQQ","SQQQ","SPXL","SPXS","SOXL","SOXS","UVXY","SVXY","ARKK",
        "ARKW","ARKG","SMH","GDX","USO","IAU","IBIT","GBTC","VOO","VTI",
        "AGG","SOXX","ARKF","ARKE",
    }
    if symbol in _KNOWN_ETFS or any(symbol.endswith(s) for s in _ETF_SUFFIXES):
        result = _unavailable("ETF — no institutional holder data")
        _cache[symbol] = (result, time.time() + _CACHE_TTL)
        return result

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
    try:
        import yfinance as yf
    except ImportError as e:
        return _unavailable(f"yfinance import failed: {e}")
    except Exception as e:
        return _unavailable(f"yfinance load error: {type(e).__name__}: {e}")

    ticker = yf.Ticker(symbol)
    info = ticker.info or {}

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
