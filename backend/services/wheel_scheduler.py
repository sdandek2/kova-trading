"""
wheel_scheduler.py — Wheel Bot cron scheduler

Schedule (all times ET):
  Monday    09:45  Full scan + place new puts
  Wednesday 09:45  Mid-week scan (fill any open slots)
  Daily     09:45  Check assignments + expirations
  Friday    16:30  End-of-week expiration cleanup

Runs in a background thread — completely separate from Kova's asyncio loop.
"""

import logging
import threading
import time
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

_scheduler_thread = None  # type: threading.Thread | None
_stop_event = threading.Event()

# ET offset from UTC (EST = -5, EDT = -4)
# Using pytz-free approach: check approximate offset
def _now_et() -> datetime:
    """Current time in US/Eastern (approximate — handles DST roughly)."""
    utc_now = datetime.now(timezone.utc)
    # EDT = UTC-4 (Mar-Nov), EST = UTC-5 (Nov-Mar)
    month = utc_now.month
    et_offset = -4 if 3 <= month <= 11 else -5
    return utc_now + timedelta(hours=et_offset)


def _should_run_cycle(now_et: datetime) -> tuple[bool, str]:
    """
    Determine if wheel cycle should run right now.
    Returns (should_run, reason).
    """
    weekday = now_et.weekday()   # 0=Mon, 4=Fri
    hour = now_et.hour
    minute = now_et.minute

    # Daily assignment + expiration check: 9:45 AM ET any weekday
    if weekday < 5 and hour == 9 and 45 <= minute <= 55:
        return True, "daily_check"

    # End-of-week cleanup: Friday 4:30 PM ET
    if weekday == 4 and hour == 16 and 30 <= minute <= 40:
        return True, "friday_cleanup"

    return False, ""


_last_run_date: str = ""  # "YYYY-MM-DD" — prevents double-firing within same day slot


def _scheduler_loop():
    """Background thread: check every 5 minutes if it's time to run."""
    global _last_run_date

    logger.info("Wheel scheduler: started")
    while not _stop_event.is_set():
        try:
            now_et = _now_et()
            should_run, reason = _should_run_cycle(now_et)

            # Key: only fire once per slot using date+hour as dedup key
            slot_key = f"{now_et.date()}_{now_et.hour}"
            if should_run and slot_key != _last_run_date:
                _last_run_date = slot_key
                logger.info(f"Wheel scheduler: firing cycle ({reason})")
                try:
                    from services.wheel_engine import run_wheel_cycle
                    run_wheel_cycle()
                except Exception as e:
                    logger.error(f"Wheel cycle error: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Wheel scheduler loop error: {e}")

        # Check every 5 minutes
        _stop_event.wait(timeout=300)

    logger.info("Wheel scheduler: stopped")


def start_wheel_scheduler():
    """Start the wheel scheduler in a background daemon thread."""
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        logger.info("Wheel scheduler already running")
        return
    _stop_event.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        name="wheel-scheduler",
        daemon=True,
    )
    _scheduler_thread.start()
    logger.info("Wheel scheduler: daemon thread started")


def stop_wheel_scheduler():
    """Stop the wheel scheduler gracefully."""
    _stop_event.set()
    if _scheduler_thread:
        _scheduler_thread.join(timeout=10)
    logger.info("Wheel scheduler: stopped")
