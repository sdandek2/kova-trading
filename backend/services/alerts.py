"""
alerts.py — Background push notifications via ntfy.sh.

ntfy.sh is free, no account needed. Set NTFY_TOPIC in Railway env vars to a
unique secret string (e.g. "kova-siddhesh-abc123"). Install the ntfy iOS app
and subscribe to that topic to receive push notifications even when the app
is closed.

Events that trigger alerts:
  - Circuit breaker trips (daily loss limit hit)
  - Unhandled trading cycle error
  - System start/stop
"""
import logging
import os
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_NTFY_BASE  = "https://ntfy.sh"
_NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")


def _send(title: str, body: str, priority: str = "default", tags: str = "") -> None:
    """Fire-and-forget POST to ntfy.sh in a background thread."""
    if not _NTFY_TOPIC:
        return

    def _post():
        try:
            import httpx
            headers = {
                "Title":    title,
                "Priority": priority,
                "Tags":     tags,
            }
            httpx.post(
                f"{_NTFY_BASE}/{_NTFY_TOPIC}",
                content=body.encode(),
                headers=headers,
                timeout=5,
            )
        except Exception as e:
            logger.debug(f"ntfy alert failed (non-fatal): {e}")

    threading.Thread(target=_post, daemon=True).start()


def alert_circuit_breaker(day_pl_pct: float, limit_pct: float) -> None:
    _send(
        title    = "⛔ Lakshmi circuit breaker tripped",
        body     = f"Down {abs(day_pl_pct):.1f}% today (limit {limit_pct}%). New buys blocked for rest of day.",
        priority = "high",
        tags     = "rotating_light",
    )


def alert_cycle_error(error: str) -> None:
    _send(
        title    = "🔴 Lakshmi cycle error",
        body     = f"{error[:200]}",
        priority = "high",
        tags     = "warning",
    )


def alert_system_start() -> None:
    _send(
        title    = "✅ Lakshmi started",
        body     = f"Trading engine online — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        priority = "low",
        tags     = "white_check_mark",
    )


def alert_system_stop(reason: str = "") -> None:
    _send(
        title    = "🛑 Lakshmi stopped",
        body     = reason or "Trading engine shut down.",
        priority = "default",
        tags     = "stop_sign",
    )
