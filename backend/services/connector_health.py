"""
connector_health.py — Monitors connector failure rates and pushes iOS alerts.

Runs hourly inside trading_engine.py.

Thresholds:
  > 80% failure rate for 24hrs  → WARNING notification
  > 80% failure rate for 72hrs  → CRITICAL notification + weight set to 1
  0 calls in 48hrs              → SILENT (connector not firing — check wiring)

Each connector must call log_connector_call() on every invocation.
Use @track_api("name") decorator to auto-log any function.
"""
import logging
import functools
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def track_api(connector_name: str):
    """
    Decorator that auto-logs success/failure of any function to connector_health_log.

    Usage:
        @track_api("alpaca_orders")
        def submit_market_order(...):
            ...

    On success  → logs status='ok'
    On exception → logs status='error', re-raises so caller still sees the error
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                result = fn(*args, **kwargs)
                _log(connector_name, "ok")
                return result
            except Exception as e:
                _log(connector_name, "error", str(e)[:500])
                raise
        return wrapper
    return decorator


def _log(connector_name: str, status: str, details: str = "") -> None:
    try:
        from services.db import log_connector_call
        log_connector_call(connector_name, status, details)
    except Exception:
        pass

# Connectors we actively monitor
MONITORED_CONNECTORS = [
    # Market data / alternative data
    "barchart",
    "finnhub",
    "sec_insider",
    "fmp_earnings",
    "quiver",
    "unusual_whales",
    "macro_fred",
    # Core trading infrastructure
    "alpaca_account",
    "alpaca_positions",
    "alpaca_orders",
    "alpaca_market_data",
    # AI + news
    "claude_ai",
    "news_api",
]

_WARNING_THRESHOLD_PCT = 80   # % failure rate to trigger alert
_WARNING_HOURS = 24           # window for warning
_CRITICAL_HOURS = 72          # window for critical alert
_SILENT_HOURS = 48            # hours with 0 calls = not firing


def check_and_alert(manager=None) -> list[dict]:
    """
    Check all connector failure rates. Send iOS push notifications for issues.
    Returns list of unhealthy connectors for logging.

    Called hourly from trading_engine.py. manager = websocket broadcast manager.
    """
    from services.db import get_connector_health, log_bot_activity

    issues = []

    # Check 24hr window (warnings)
    health_24h = {r["connector_name"]: r for r in get_connector_health(hours=24)}
    # Check 72hr window (critical)
    health_72h = {r["connector_name"]: r for r in get_connector_health(hours=72)}
    # Check 48hr for silent connectors
    health_48h = {r["connector_name"]: r for r in get_connector_health(hours=48)}

    for connector in MONITORED_CONNECTORS:
        row_24 = health_24h.get(connector)
        row_72 = health_72h.get(connector)
        row_48 = health_48h.get(connector)

        # Key not configured — expected, not a failure. Show in dashboard but don't alert.
        if row_48 and row_48.get("key_missing") and row_48.get("total_calls", 0) == 0:
            logger.debug(f"CONNECTOR_NO_KEY: {connector} — API key not set, skipping health check")
            continue

        # Silent — connector not firing at all
        if not row_48 or row_48["total_calls"] == 0:
            issue = {
                "connector": connector,
                "severity": "silent",
                "message": f"{connector} has fired 0 times in 48hrs — check wiring",
                "failure_pct": 0,
            }
            issues.append(issue)
            logger.warning(f"CONNECTOR_SILENT: {issue['message']}")
            continue

        # Critical — 72hr sustained failure
        if row_72 and row_72["failure_pct"] >= _WARNING_THRESHOLD_PCT:
            issue = {
                "connector": connector,
                "severity": "critical",
                "message": (
                    f"{connector} failing {row_72['failure_pct']}% over 72hrs "
                    f"({row_72['failed_calls']}/{row_72['total_calls']} calls). "
                    f"Last error: {row_72['last_error'] or 'unknown'}"
                ),
                "failure_pct": row_72["failure_pct"],
            }
            issues.append(issue)
            logger.error(f"CONNECTOR_CRITICAL: {issue['message']}")
            _send_push(connector, "critical", issue["message"], manager)
            _disable_connector_weight(connector)
            log_bot_activity("connector_critical", issue["message"])
            continue

        # Warning — 24hr failure
        if row_24 and row_24["failure_pct"] >= _WARNING_THRESHOLD_PCT:
            issue = {
                "connector": connector,
                "severity": "warning",
                "message": (
                    f"{connector} failing {row_24['failure_pct']}% over 24hrs "
                    f"({row_24['failed_calls']}/{row_24['total_calls']} calls). "
                    f"Check API key or endpoint."
                ),
                "failure_pct": row_24["failure_pct"],
            }
            issues.append(issue)
            logger.warning(f"CONNECTOR_WARNING: {issue['message']}")
            _send_push(connector, "warning", issue["message"], manager)
            log_bot_activity("connector_warning", issue["message"])

    return issues


def _send_push(connector: str, severity: str, message: str, manager) -> None:
    """Broadcast a WebSocket push so the iOS app shows a local notification."""
    if not manager:
        return
    import asyncio
    title = f"{'⚠️' if severity == 'warning' else '🔴'} Connector {'Warning' if severity == 'warning' else 'Critical'}"
    payload = {
        "type": "connector_alert",
        "data": {
            "connector": connector,
            "severity": severity,
            "title": title,
            "body": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    }
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(manager.broadcast(payload))
    except Exception as e:
        logger.debug(f"connector push failed (non-fatal): {e}")


def _disable_connector_weight(connector: str) -> None:
    """
    For critically failing connectors that map to a signal weight,
    set weight to 1 so it stops influencing scores.
    Recovers automatically when win rate > 70% again.
    """
    # Map connector names to signal weight names
    _connector_to_signal = {
        "barchart": ["barchart_very_unusual", "barchart_unusual"],
        "finnhub": ["analyst_revision"],
        "sec_insider": ["insider_buy_large", "insider_buy_small"],
        "fmp_earnings": ["earnings_surprise_strong", "earnings_surprise_mild"],
        "quiver": [],  # institutional data — no direct signal weight
        "unusual_whales": ["options_flow_fallback"],
        # Core infrastructure — no signal weight mapping; if these fail, bot can't trade at all
        "alpaca_account": [],
        "alpaca_positions": [],
        "alpaca_orders": [],
        "alpaca_market_data": [],
        "claude_ai": [],
        "news_api": [],
        "macro_fred": [],
    }
    signals = _connector_to_signal.get(connector, [])
    if not signals:
        return

    from services.db import update_signal_weight
    for signal in signals:
        update_signal_weight(
            signal_name=signal,
            new_weight=1,
            reason=f"auto-disabled: {connector} failing >80% for 72hrs",
        )
        logger.warning(f"CONNECTOR_DISABLED: {signal} weight set to 1 (connector {connector} critical)")
