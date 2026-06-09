"""
wheel_scheduler.py — Wheel Bot cron scheduler

Schedule (all times ET):
  Sunday    20:00  Universe refresh — AI discovers next week's stocks
  Monday    09:45  Full cycle: scan + place new puts
  Wednesday 09:45  Mid-week scan — fill any open position slots
  Daily     09:45  Check assignments + expirations
  Friday    16:30  Optimizer run + EOW cleanup

Completely isolated from Kova's asyncio loop — runs as a daemon thread.
"""

import logging
import threading
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

_scheduler_thread = None  # type: threading.Thread | None
_stop_event = threading.Event()


def _now_et() -> datetime:
    """Approximate US/Eastern time (handles DST roughly: EDT Mar-Nov, EST Nov-Mar)."""
    utc = datetime.now(timezone.utc)
    offset = -4 if 3 <= utc.month <= 11 else -5
    return utc + timedelta(hours=offset)


def _should_fire(now_et: datetime) -> tuple[bool, str]:
    """Return (should_fire, event_name) for the current moment."""
    wd = now_et.weekday()  # 0=Mon, 6=Sun
    h, m = now_et.hour, now_et.minute

    # Sunday 8 PM — AI universe refresh
    if wd == 6 and h == 20 and 0 <= m < 10:
        return True, "universe_refresh"

    # Daily 9:45 AM weekdays — assignments + expirations + (Mon/Wed) scan
    if wd < 5 and h == 9 and 45 <= m < 55:
        return True, "daily_cycle"

    # Friday 4:30 PM — optimizer + EOW cleanup
    if wd == 4 and h == 16 and 30 <= m < 40:
        return True, "optimizer"

    return False, ""


_last_fired: str = ""   # "YYYY-MM-DD_HH" — prevents double-firing in same slot


def _scheduler_loop():
    global _last_fired
    logger.info("Wheel scheduler: started (checks every 5 min)")

    while not _stop_event.is_set():
        try:
            now_et = _now_et()
            should_fire, event = _should_fire(now_et)
            slot_key = f"{now_et.date()}_{now_et.hour}_{event}"

            if should_fire and slot_key != _last_fired:
                _last_fired = slot_key
                logger.info(f"Wheel scheduler: firing [{event}]")

                if event == "universe_refresh":
                    try:
                        # Step 1: Sprint report — log last week's performance
                        from services.wheel_universe import get_wheel_sprint_report, refresh_universe
                        from services.db import cache_set
                        report = get_wheel_sprint_report()
                        cache_set("wheel:last_sprint_report", report, 8 * 24 * 3600)
                        wr   = report.get("win_rate_30d", {}).get("win_rate_pct")
                        fr   = report.get("fill_rate_14d", {}).get("fill_rate_pct")
                        prem = report.get("summary", {}).get("collected_premium", 0)
                        logger.info(
                            f"Wheel sprint report: win_rate={wr}% | "
                            f"fill_rate={fr}% | premium_collected=${prem:.2f}"
                        )
                        # Step 2: Refresh universe (auto-adjusts thresholds inside)
                        result = refresh_universe()
                        logger.info(f"Wheel universe refreshed: {len(result)} stocks selected")
                    except Exception as e:
                        logger.error(f"Wheel universe refresh failed: {e}", exc_info=True)

                elif event == "daily_cycle":
                    try:
                        from services.wheel_engine import run_wheel_cycle
                        run_wheel_cycle()
                    except Exception as e:
                        logger.error(f"Wheel daily cycle failed: {e}", exc_info=True)

                elif event == "optimizer":
                    try:
                        from services.wheel_optimizer import run_optimizer
                        report = run_optimizer()
                        logger.info(f"Wheel optimizer: {report.get('total_completed_cycles', 0)} cycles, "
                                    f"{report.get('overall_win_rate_pct', 0)}% win rate")
                    except Exception as e:
                        logger.error(f"Wheel optimizer failed: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Wheel scheduler loop error: {e}")

        _stop_event.wait(timeout=300)  # 5 min

    logger.info("Wheel scheduler: stopped")


def start_wheel_scheduler():
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
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
    _stop_event.set()
    if _scheduler_thread:
        _scheduler_thread.join(timeout=10)
