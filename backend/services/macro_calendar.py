"""
macro_calendar.py — High-impact macro event calendar

Tracks three categories of market-moving scheduled events:
  1. FOMC meetings  — rate decision day, market swings 1-3% at 2 PM ET
  2. CPI/PPI/Jobs   — macro data releases, market swings 0.5-2%
  3. FDA PDUFA      — biotech binary events, stock swings 30-80%

On each event type the bot applies different risk rules:
  FOMC day     → reduce all position sizes 50%, block new entries after 1:45 PM ET
  CPI/Jobs day → reduce all position sizes 30%
  FDA day      → same treatment as earnings (predict direction or block)

Update the date sets below at the start of each year.
"""

import logging
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)

# ── FOMC Meeting Dates (rate decision announced ~2 PM ET on 2nd day) ─────────
# Source: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
# Update annually — the Fed publishes the next year's dates in November.
FOMC_DATES = {
    # 2025
    date(2025, 1, 29),
    date(2025, 3, 19),
    date(2025, 5, 7),
    date(2025, 6, 18),
    date(2025, 7, 30),
    date(2025, 9, 17),
    date(2025, 10, 29),
    date(2025, 12, 10),
    # 2026
    date(2026, 1, 28),
    date(2026, 3, 18),
    date(2026, 4, 29),
    date(2026, 6, 17),
    date(2026, 7, 29),
    date(2026, 9, 16),
    date(2026, 10, 28),
    date(2026, 12, 9),
}

# ── CPI Release Dates (BLS Consumer Price Index, ~8:30 AM ET) ────────────────
# Source: https://www.bls.gov/schedule/news_release/cpi.htm
CPI_DATES = {
    # 2025
    date(2025, 1, 15), date(2025, 2, 12), date(2025, 3, 12),
    date(2025, 4, 10), date(2025, 5, 13), date(2025, 6, 11),
    date(2025, 7, 15), date(2025, 8, 12), date(2025, 9, 10),
    date(2025, 10, 15), date(2025, 11, 13), date(2025, 12, 10),
    # 2026 — source: https://www.bls.gov/schedule/news_release/cpi.htm
    date(2026, 1, 13), date(2026, 2, 13), date(2026, 3, 11),
    date(2026, 4, 10), date(2026, 5, 12), date(2026, 6, 10),
    date(2026, 7, 14), date(2026, 8, 12), date(2026, 9, 11),
    date(2026, 10, 14), date(2026, 11, 10), date(2026, 12, 10),
}

# ── Non-Farm Payrolls / Jobs Report Dates (BLS, first Friday of month, ~8:30 AM ET) ──
JOBS_DATES = {
    # 2025
    date(2025, 1, 10), date(2025, 2, 7),  date(2025, 3, 7),
    date(2025, 4, 4),  date(2025, 5, 2),  date(2025, 6, 6),
    date(2025, 7, 3),  date(2025, 8, 1),  date(2025, 9, 5),
    date(2025, 10, 3), date(2025, 11, 7), date(2025, 12, 5),
    # 2026 — source: https://www.bls.gov/schedule/news_release/empsit.htm
    date(2026, 1, 9),  date(2026, 2, 11), date(2026, 3, 6),
    date(2026, 4, 3),  date(2026, 5, 8),  date(2026, 6, 5),
    date(2026, 7, 2),  date(2026, 8, 7),  date(2026, 9, 4),
    date(2026, 10, 2), date(2026, 11, 6), date(2026, 12, 4),
}

# ── Known Biotech / FDA-sensitive sectors ────────────────────────────────────
_BIOTECH_SECTORS = {"Biotech", "Pharmaceutical", "Healthcare", "Biotech/Pharma"}

# Keywords that indicate an FDA binary event in news headlines
_FDA_KEYWORDS = [
    "fda approval", "fda approved", "fda rejects", "fda rejected",
    "pdufa", "nda approval", "bla approval", "complete response letter",
    "advisory committee", "adcom", "fda decision", "fda action",
    "new drug application", "biologics license",
]


def get_macro_event_today() -> dict:
    """
    Returns today's macro event context.

    Returns:
    {
        "event": "fomc" | "cpi" | "jobs" | None,
        "risk_level": "high" | "medium" | None,
        "position_size_multiplier": float,   # 1.0 = normal, 0.5 = half size
        "block_entries_after_et": int | None, # minutes since midnight ET to block entries
        "message": str,
    }
    """
    today = datetime.now(timezone.utc).date()

    if today in FOMC_DATES:
        return {
            "event": "fomc",
            "risk_level": "high",
            "position_size_multiplier": 0.50,   # half size on FOMC day
            "block_entries_after_et": 13 * 60 + 45,  # 1:45 PM ET — 15 min before 2 PM announcement
            "message": f"FOMC rate decision today — position sizes halved, no new entries after 1:45 PM ET",
        }

    if today in CPI_DATES:
        return {
            "event": "cpi",
            "risk_level": "medium",
            "position_size_multiplier": 0.70,   # 30% smaller on CPI day
            "block_entries_after_et": None,
            "message": f"CPI release today — position sizes reduced 30% (market volatility risk)",
        }

    if today in JOBS_DATES:
        return {
            "event": "jobs",
            "risk_level": "medium",
            "position_size_multiplier": 0.70,
            "block_entries_after_et": None,
            "message": f"Jobs report today — position sizes reduced 30% (market volatility risk)",
        }

    return {
        "event": None,
        "risk_level": None,
        "position_size_multiplier": 1.0,
        "block_entries_after_et": None,
        "message": "",
    }


def is_fomc_entry_blocked() -> bool:
    """
    Returns True if it's an FOMC day AND current ET time is past 1:45 PM.
    New entries should be blocked to avoid the 2 PM announcement spike.
    """
    today = datetime.now(timezone.utc).date()
    if today not in FOMC_DATES:
        return False
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        from datetime import timedelta
        now_et = datetime.now(timezone.utc) - timedelta(hours=4)
    return now_et.hour * 60 + now_et.minute >= 13 * 60 + 45


def check_fda_event(symbol: str, news_headlines: list, sector: str = "") -> dict:
    """
    Detect if a stock has an FDA binary event today based on news headlines.
    Returns same shape as earnings_map entry for consistent handling.

    Returns {"has_fda_event": bool, "timing": "today/tomorrow" | "this_week" | None}
    """
    if not news_headlines:
        return {"has_fda_event": False, "timing": None}

    symbol_lower = symbol.lower()
    for headline in news_headlines:
        headline_lower = headline.lower()
        # Must mention the symbol AND an FDA keyword
        if symbol_lower in headline_lower and any(kw in headline_lower for kw in _FDA_KEYWORDS):
            return {"has_fda_event": True, "timing": "today/tomorrow"}

    return {"has_fda_event": False, "timing": None}
