"""
Session 6 — Earnings Surprise via Financial Modeling Prep (FMP).

Detects stocks that beat/missed EPS estimates and injects them into the
tradeable universe for 21 days post-earnings to capture post-earnings drift.

Free tier: 250 calls/day (this module uses ~2 calls/day — bulk fetch, cached 24h)
API key env var: FMP_API_KEY

Signal:
  EPS beat > 10%  → +12 conviction pts, inject into universe for 21 days
  EPS miss > 10%  → -12 conviction pts (bearish flag, still needs score ≥55)
"""
import logging
import os
import time
from datetime import date, timedelta

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://financialmodelingprep.com/stable"


def _log_health(result: dict, no_key: bool = False) -> None:
    try:
        from services.db import log_connector_call
        if no_key:
            status = "no_key"
        elif result.get("signal") == "unavailable":
            status = "unavailable"
        else:
            status = "ok"
        log_connector_call("fmp_earnings", status, result.get("details", ""))
    except Exception:
        pass
_CACHE_TTL = 86_400  # 24 hours — earnings data changes once/day at most

# symbol → {beat_pct, beat_date, eps_actual, eps_estimate, direction}
_earnings_cache: dict[str, dict] = {}
_cache_fetched_at: float = 0.0

# Inject window: track symbols with recent surprises
# symbol → expiry_timestamp
_inject_until: dict[str, float] = {}


def _get_api_key() -> str:
    try:
        from config import settings
        return getattr(settings, "fmp_api_key", "") or os.environ.get("FMP_API_KEY", "")
    except Exception:
        return os.environ.get("FMP_API_KEY", "")


def _fetch_earnings_range(from_date: str, to_date: str, api_key: str) -> list[dict]:
    """Fetch earnings calendar for a date range from FMP stable API."""
    url = f"{_API_BASE}/earnings-calendar"
    try:
        resp = httpx.get(
            url,
            params={"from": from_date, "to": to_date, "apikey": api_key},
            timeout=15,
            headers={"User-Agent": "Kova Trading kova@trading.com"},
        )
        if resp.status_code == 402:
            logger.warning("FMP earnings: 402 — date range too wide or free tier exhausted")
            return []
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("FMP earnings fetch %s→%s: %s", from_date, to_date, e)
        return []


def _refresh_cache() -> None:
    """Refresh earnings cache: fetch past 21 days + next 21 days (2 API calls)."""
    global _cache_fetched_at

    api_key = _get_api_key()
    if not api_key:
        logger.warning("FMP earnings: FMP_API_KEY not set — skipping earnings signal")
        _log_health({"signal": "unavailable", "details": "FMP_API_KEY not set"}, no_key=True)
        _cache_fetched_at = time.time()
        return

    today = date.today()
    # Past beats — free tier allows up to 21 days back
    past_from = (today - timedelta(days=21)).isoformat()
    past_to = today.isoformat()
    # Upcoming earnings — forward calendar
    future_from = today.isoformat()
    future_to = (today + timedelta(days=21)).isoformat()

    past = _fetch_earnings_range(past_from, past_to, api_key)
    upcoming = _fetch_earnings_range(future_from, future_to, api_key)

    all_earnings = past + upcoming

    new_cache: dict[str, dict] = {}
    new_inject: dict[str, float] = {}

    for record in all_earnings:
        symbol = (record.get("symbol") or "").upper().strip()
        if not symbol:
            continue

        eps_actual = record.get("epsActual")
        eps_estimate = record.get("epsEstimated")
        earnings_date_str = record.get("date") or record.get("reportDate") or ""

        if eps_actual is None or eps_estimate is None:
            continue

        # Skip if no estimate or negative estimate (math breaks)
        try:
            eps_actual = float(eps_actual)
            eps_estimate = float(eps_estimate)
        except (TypeError, ValueError):
            continue

        if eps_estimate == 0:
            continue

        # Bug fix: do NOT skip negative estimates. Growth companies often guide for
        # a small loss (e.g. estimate -$0.10) and beat it (actual -$0.03).
        # That's a meaningful positive surprise — +70% beat on abs basis.
        # Use abs(eps_estimate) so the percentage math works regardless of sign.
        beat_pct = ((eps_actual - eps_estimate) / abs(eps_estimate)) * 100

        # Only care about meaningful beats/misses
        if abs(beat_pct) < 5:
            continue

        direction = "beat" if beat_pct > 0 else "miss"

        new_cache[symbol] = {
            "beat_pct": beat_pct,
            "beat_date": earnings_date_str,
            "eps_actual": eps_actual,
            "eps_estimate": eps_estimate,
            "direction": direction,
        }

        # Inject window: 21 days from earnings date
        try:
            edate = date.fromisoformat(earnings_date_str[:10])
            inject_expiry = time.time() + (edate - today).days * 86400 + 21 * 86400
        except Exception:
            inject_expiry = time.time() + 21 * 86400

        if beat_pct > 10:  # Only inject strong beats into universe
            new_inject[symbol] = inject_expiry

    _earnings_cache.clear()
    _earnings_cache.update(new_cache)
    _inject_until.clear()
    _inject_until.update(new_inject)
    _cache_fetched_at = time.time()

    beats = sum(1 for v in new_cache.values() if v["direction"] == "beat")
    misses = sum(1 for v in new_cache.values() if v["direction"] == "miss")
    logger.info("FMP earnings cache: %d beats, %d misses, %d inject candidates",
                beats, misses, len(new_inject))


def _ensure_fresh() -> None:
    if time.time() - _cache_fetched_at > _CACHE_TTL:
        _refresh_cache()


# ── Public API ────────────────────────────────────────────────────────────────

def get_universe_additions() -> list[str]:
    """
    Return list of symbols to inject into the tradeable universe.
    These are stocks with >10% EPS beat in the last 21 days — targets post-earnings drift.
    Called by alpaca_service.get_tradeable_universe() once per cycle.
    """
    _ensure_fresh()
    now = time.time()
    return [sym for sym, expiry in _inject_until.items() if expiry > now]


def get_earnings_signal(symbol: str) -> dict:
    """
    Return earnings surprise signal for a symbol.

    Returns:
        {
          "signal": "beat" | "miss" | "neutral" | "unavailable",
          "conviction_boost": int,
          "details": str
        }
    """
    _ensure_fresh()

    data = _earnings_cache.get(symbol.upper())
    if not data:
        return {"signal": "neutral", "conviction_boost": 0, "details": "no recent earnings surprise"}

    beat_pct = data["beat_pct"]
    direction = data["direction"]

    if beat_pct > 10:
        boost = 12
    elif beat_pct > 5:
        boost = 6
    elif beat_pct < -10:
        boost = -12
    elif beat_pct < -5:
        boost = -6
    else:
        boost = 0

    detail = (f"EPS {direction}: {beat_pct:+.1f}% vs estimate "
              f"(actual {data['eps_actual']:.2f} vs est {data['eps_estimate']:.2f}) "
              f"on {data['beat_date']}")

    result = {
        "signal": direction if abs(boost) > 0 else "neutral",
        "conviction_boost": boost,
        "details": detail,
    }
    _log_health(result)
    return result
