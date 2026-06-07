"""
Phase 7 — External data connectors package.

Provides a single get_all_signals(symbol) call that aggregates all three
data sources and returns a combined conviction boost + status dict.

Each connector degrades silently when its API key is missing — the system
never fails because of a missing optional key.
"""
import logging

logger = logging.getLogger(__name__)


def get_all_signals(symbol: str) -> dict:
    """
    Fetch all available external signals for a symbol and return a summary.

    Returns:
        {
          "total_boost": int,           # sum of all active conviction boosts
          "options_flow": dict,         # Unusual Whales result
          "earnings_revision": dict,    # FMP result
          "dark_pool": dict,            # Quiver result
          "active_sources": list[str],  # which APIs had keys set
        }
    """
    from .unusual_whales import get_options_flow
    from .fmp import get_estimate_revision
    from .quiver import get_darkpool_signal

    flow    = get_options_flow(symbol)
    rev     = get_estimate_revision(symbol)
    dark    = get_darkpool_signal(symbol)

    total_boost = 0
    active = []

    for name, result in [("unusual_whales", flow), ("fmp", rev), ("quiver", dark)]:
        boost = result.get("conviction_boost", 0)
        total_boost += boost
        if result.get("signal") != "unavailable":
            active.append(name)

    logger.debug(
        "connectors %s: total_boost=%+d active=%s",
        symbol, total_boost, active or ["none"],
    )

    return {
        "total_boost":        total_boost,
        "options_flow":       flow,
        "earnings_revision":  rev,
        "dark_pool":          dark,
        "active_sources":     active,
    }


def connector_status() -> dict:
    """
    Return which connectors are active (have API keys configured).
    Useful for the /status API endpoint and startup logs.
    """
    try:
        from config import settings
        return {
            "unusual_whales": bool(settings.uw_api_key),
            "fmp":            bool(settings.fmp_api_key),
            "quiver":         bool(settings.quiver_api_key),
        }
    except Exception:
        return {"unusual_whales": False, "fmp": False, "quiver": False}


def log_connector_status() -> None:
    """Log which Phase 7 connectors are active at startup."""
    status = connector_status()
    active   = [k for k, v in status.items() if v]
    inactive = [k for k, v in status.items() if not v]

    if active:
        logger.info("Phase 7 connectors ACTIVE: %s", ", ".join(active))
    if inactive:
        logger.info(
            "Phase 7 connectors INACTIVE (no API key): %s — "
            "set keys in .env to enable",
            ", ".join(inactive),
        )
