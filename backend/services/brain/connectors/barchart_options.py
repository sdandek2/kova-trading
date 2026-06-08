"""
Session 6 — Unusual Options Flow via Barchart (free, no API key needed).

Replaces unusual_whales.py which used raw Alpaca OI without filtering for
unusual activity. Barchart provides pre-filtered unusual options with
volumeOpenInterestRatio — volume / open interest — which separates genuinely
new aggressive positioning from routine trading.

Key insight: when volume >> open_interest, this means traders opened NEW
positions today (not just trading existing contracts). This is the unusual
signal we care about.

Authentication: session cookie scraping (no account required)
  Step 1: GET main page → capture XSRF-TOKEN and laravel_session cookies
  Step 2: URL-decode XSRF token → use as X-XSRF-TOKEN header
  Step 3: API call returns JSON with real unusual options flow

Signal thresholds:
  vol/OI ratio > 10x = unusual positioning
  vol/OI ratio > 50x = highly unusual (very strong signal)

Conviction boosts (applied per stock signal in signals.py):
  Unusual call (bullish): +18 pts (very unusual) / +10 pts (unusual)
  Unusual put (bearish):  -18 pts (very unusual) / -10 pts (unusual)

Cache: 15 minutes (data updates throughout the trading day)
"""
import logging
import time
import urllib.parse
from collections import defaultdict

import httpx

logger = logging.getLogger(__name__)

_CACHE_TTL = 900  # 15 minutes
_cache_data: dict[str, dict] = {}         # symbol → {signal, conviction_boost, details}
_short_candidates: dict[str, dict] = {}   # symbol → {volume, ratio, details} for heavy put flow
_cache_fetched_at: float = 0.0

# Barchart session state
_session = None  # type: httpx.Client | None
_session_refreshed_at: float = 0.0
_SESSION_TTL = 1800  # refresh session every 30 minutes

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_MAIN_PAGE = "https://www.barchart.com/options/unusual-activity/stocks"
_API_URL = (
    "https://www.barchart.com/proxies/core-api/v1/options/get"
    "?fields=baseSymbol,symbolType,strikePrice,expirationDate,daysToExpiration,"
    "volume,openInterest,volumeOpenInterestRatio,tradeTime"
    "&unusual=1&limit=100&rawData=1&orderBy=volumeOpenInterestRatio&orderDir=desc"
)

# Thresholds
_RATIO_VERY_UNUSUAL = 50.0   # vol > 50x OI → very strong new positioning
_RATIO_UNUSUAL = 10.0        # vol > 10x OI → unusual positioning
_MIN_VOLUME = 500            # absolute floor — filters zero-liquidity instruments only

# Conviction boosts
_BOOST_VERY_UNUSUAL = 18
_BOOST_UNUSUAL = 10

# Short trigger threshold — put flow this heavy means institutional bearish conviction.
# Proactively inject the stock as a short candidate rather than just reducing score.
# Must ALSO have ratio >= _RATIO_VERY_UNUSUAL (50x) to avoid noise.
# Today's example: MU 9,500 contracts 1,357× → heavy short trigger
#                  SBUX 1,311 contracts 1,311× → bearish score only (not heavy enough)
_SHORT_TRIGGER_VOLUME = 5_000   # contracts — institutional scale put flow

# Exclude ETFs, bond funds, commodity funds, crypto ETFs, leveraged funds.
# These show unusual flow for macro hedging reasons unrelated to individual stock signals.
# Data-verified list — cross-checked against today's Barchart feed (2026-06-07).
_EXCLUDED_SYMBOLS = {
    # Broad index ETFs
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "VQQ",
    # Sector ETFs
    "XLK", "XLF", "XLV", "XLE", "XLY", "XLI", "XLU", "XLP", "XLB", "XLRE",
    "SOXX", "SMH", "IBB", "XBI", "XRT", "XHB", "KRE", "KBE",
    # International / EM ETFs
    "EEM", "EFA", "VEA", "VWO", "KWEB", "ASHR",
    # Bond / rate ETFs
    "TLT", "IEF", "SHY", "HYG", "LQD", "AGG", "BND",
    # Commodity ETFs
    "GLD", "IAU", "SLV", "SILJ", "GDX", "GDXJ", "USO", "UNG",
    # Crypto ETFs
    "IBIT", "ETHA", "FBTC", "GBTC", "WGMI", "MARA",  # MARA is a miner, more stock-like but very crypto-correlated
    # Volatility
    "VIX", "UVXY", "SVXY",
    # Leveraged / inverse
    "TQQQ", "SQQQ", "SPXL", "SPXS", "SOXL", "SOXS", "LABU", "LABD",
    # China / thematic ETFs
    "NASA", "XOVR", "ARKK", "ARKG", "ARKW",
}


def _log_health(result: dict) -> None:
    try:
        from services.db import log_connector_call
        status = "ok" if result.get("signal") not in ("unavailable",) else "unavailable"
        log_connector_call("barchart", status, result.get("details", ""))
    except Exception:
        pass


def _get_session() -> httpx.Client:
    """Return a valid Barchart session, refreshing if needed."""
    global _session, _session_refreshed_at

    if _session and time.time() - _session_refreshed_at < _SESSION_TTL:
        return _session

    if _session:
        _session.close()

    client = httpx.Client(follow_redirects=True, timeout=20)
    try:
        r = client.get(_MAIN_PAGE, headers={"User-Agent": _UA})
        r.raise_for_status()
    except Exception as e:
        client.close()
        raise RuntimeError(f"Barchart session init failed: {e}") from e

    _session = client
    _session_refreshed_at = time.time()
    return client


def _fetch_unusual_flow() -> list[dict]:
    """Fetch the top-100 unusual options by vol/OI ratio from Barchart."""
    client = _get_session()

    # URL-decode the XSRF token (Barchart encodes it as a URL component)
    xsrf_raw = client.cookies.get("XSRF-TOKEN", "")
    xsrf = urllib.parse.unquote(xsrf_raw)

    headers = {
        "User-Agent": _UA,
        "Referer": _MAIN_PAGE,
        "X-XSRF-TOKEN": xsrf,
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }

    r = client.get(_API_URL, headers=headers)
    if r.status_code == 401:
        # Session expired — force refresh on next call
        global _session_refreshed_at
        _session_refreshed_at = 0
        raise RuntimeError("Barchart 401 — session expired, will retry")

    r.raise_for_status()
    data = r.json()
    return data.get("data", [])


def _parse_ratio(val) -> float:
    """Parse volumeOpenInterestRatio which may be formatted as '1,589.65'."""
    if val is None:
        return 0.0
    try:
        return float(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def _parse_volume(val) -> int:
    """Parse volume which may be formatted as '122,403'."""
    if val is None:
        return 0
    try:
        return int(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return 0


def _refresh_cache() -> None:
    """Fetch fresh unusual options flow and populate per-symbol cache."""
    global _cache_fetched_at

    try:
        records = _fetch_unusual_flow()
    except Exception as e:
        logger.warning("Barchart unusual options: %s", e)
        # Do NOT update _cache_fetched_at on failure — allows retry next cycle.
        # If we set it here, a Barchart outage would silently blackout the signal
        # for 15 minutes with no data, rather than retrying every cycle.
        return

    # Aggregate per symbol — a stock may appear multiple times (different strikes/expiries)
    # Keep the strongest signal per symbol
    sym_signals: dict[str, dict] = {}

    for row in records:
        symbol = (row.get("baseSymbol") or "").upper().strip().lstrip("$")
        if not symbol:
            continue

        option_type = (row.get("symbolType") or "").lower()  # "call" or "put"
        ratio = _parse_ratio(row.get("volumeOpenInterestRatio"))
        volume = _parse_volume(row.get("volume"))

        # Filter noise
        if volume < _MIN_VOLUME:
            continue
        if ratio < _RATIO_UNUSUAL:
            continue
        # Skip foreign tickers (contain dot = TSX, LSE, etc.) or >5 chars
        import re as _re
        if not _re.match(r'^[A-Z]{1,5}$', symbol):
            continue
        # Skip ETFs, bond funds, commodity funds — their options flow is macro
        # hedging, not individual stock conviction. Full list in _EXCLUDED_SYMBOLS.
        if symbol in _EXCLUDED_SYMBOLS:
            continue

        is_call = option_type == "call"
        is_put = option_type == "put"

        if ratio >= _RATIO_VERY_UNUSUAL:
            boost = _BOOST_VERY_UNUSUAL if is_call else (-_BOOST_VERY_UNUSUAL if is_put else 0)
            label = "very unusual"
        else:
            boost = _BOOST_UNUSUAL if is_call else (-_BOOST_UNUSUAL if is_put else 0)
            label = "unusual"

        signal = "bullish" if boost > 0 else ("bearish" if boost < 0 else "neutral")
        detail = (f"{label} {option_type} flow: vol/OI={ratio:.0f}x "
                  f"(vol={volume:,}) strike={row.get('strikePrice')} "
                  f"exp={row.get('expirationDate')}")

        # Keep strongest absolute boost per symbol
        existing = sym_signals.get(symbol)
        if existing is None or abs(boost) > abs(existing["conviction_boost"]):
            sym_signals[symbol] = {
                "signal": signal,
                "conviction_boost": boost,
                "details": detail,
            }

    # ── Identify heavy put flow → short candidates ────────────────────────────
    # Stocks with institutional-scale put volume (≥5,000 contracts AND ratio ≥ 50×)
    # get flagged as proactive short candidates, not just score reductions.
    # This lets the system inject them into the universe with a bearish intent
    # rather than waiting to stumble across them via movers/volume.
    new_shorts: dict[str, dict] = {}
    for sym, sig in sym_signals.items():
        if sig["conviction_boost"] >= 0:
            continue  # calls only — we want puts
        # Re-scan raw records for this symbol to get actual volume
        # (sym_signals only stores the strongest signal, not raw volume)
        pass  # handled below in raw_put_vol lookup

    # Build short candidates from raw records directly
    raw_put_vol: dict[str, int] = {}
    raw_put_ratio: dict[str, float] = {}
    for row in records:
        sym = (row.get("baseSymbol") or "").upper().strip().lstrip("$")
        if not sym or sym in _EXCLUDED_SYMBOLS:
            continue
        if (row.get("symbolType") or "").lower() != "put":
            continue
        vol = _parse_volume(row.get("volume"))
        ratio = _parse_ratio(row.get("volumeOpenInterestRatio"))
        if vol > raw_put_vol.get(sym, 0):
            raw_put_vol[sym] = vol
            raw_put_ratio[sym] = ratio

    for sym, vol in raw_put_vol.items():
        ratio = raw_put_ratio.get(sym, 0)
        if vol >= _SHORT_TRIGGER_VOLUME and ratio >= _RATIO_VERY_UNUSUAL:
            new_shorts[sym] = {
                "volume": vol,
                "ratio": ratio,
                "details": f"heavy put flow: {vol:,} contracts at {ratio:.0f}× vol/OI",
            }

    _cache_data.clear()
    _cache_data.update(sym_signals)
    _short_candidates.clear()
    _short_candidates.update(new_shorts)
    _cache_fetched_at = time.time()

    bullish = sum(1 for v in sym_signals.values() if v["conviction_boost"] > 0)
    bearish = sum(1 for v in sym_signals.values() if v["conviction_boost"] < 0)
    logger.info(
        "Barchart unusual options: %d stocks (%d bullish, %d bearish), %d short triggers: %s",
        len(sym_signals), bullish, bearish,
        len(new_shorts), list(new_shorts.keys()),
    )


def _ensure_fresh() -> None:
    if time.time() - _cache_fetched_at > _CACHE_TTL:
        _refresh_cache()


# ── Public API ────────────────────────────────────────────────────────────────

def get_options_flow(symbol: str) -> dict:
    """
    Return unusual options flow signal for a symbol.

    Replaces unusual_whales.get_options_flow() — same interface.

    Returns:
        {
          "signal": "bullish" | "bearish" | "neutral" | "unavailable",
          "conviction_boost": int,
          "details": str
        }
    """
    _ensure_fresh()

    result = _cache_data.get(symbol.upper().strip())
    if result:
        _log_health(result)
        return result

    neutral = {"signal": "neutral", "conviction_boost": 0, "details": "no unusual options activity"}
    _log_health(neutral)
    return neutral


def get_all_unusual_symbols() -> list[str]:
    """
    Return all symbols with unusual options activity this session.
    Used to expand the tradeable universe with bullish call flow candidates.
    """
    _ensure_fresh()
    return list(_cache_data.keys())


def get_short_candidates() -> dict[str, dict]:
    """
    Return symbols with heavy institutional put flow — proactive short candidates.

    Criteria: volume >= 5,000 contracts AND vol/OI ratio >= 50x.
    This is institutional-scale bearish positioning, not retail hedging.

    Returns:
        {symbol: {"volume": int, "ratio": float, "details": str}}

    Example today: {"MU": {"volume": 9500, "ratio": 1357.0, "details": "..."}}
    """
    _ensure_fresh()
    return dict(_short_candidates)


def get_bearish_symbols() -> list[str]:
    """
    Return all symbols with ANY unusual put flow (vol/OI > 10x, vol > 500).
    Used for universe injection of bearish candidates (lighter threshold than short_candidates).
    """
    _ensure_fresh()
    return [s for s, v in _cache_data.items() if v["conviction_boost"] < 0]
