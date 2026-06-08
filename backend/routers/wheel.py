"""
routers/wheel.py — Wheel Bot API

All endpoints are /wheel/* — completely separate from all Kova endpoints.
Powers the iOS "Wheel" tab. No overlap with Kova trading logic.

Endpoints:
  GET  /wheel/status           — full dashboard
  GET  /wheel/positions        — active positions
  GET  /wheel/summary          — P&L totals
  GET  /wheel/scan             — preview opportunities (no orders placed)
  POST /wheel/execute          — run full cycle: scan + trade
  POST /wheel/check            — assignments + expirations only
  GET  /wheel/universe         — AI-discovered stock universe with scores
  POST /wheel/universe/refresh — trigger manual universe refresh via AI
  GET  /wheel/optimizer        — per-symbol performance stats
  POST /wheel/optimizer/run    — trigger manual optimizer run
  GET  /wheel/config           — current config (paper/live mode, limits)
"""

import logging
from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/wheel", tags=["wheel"])


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("/status")
def wheel_status():
    """Full dashboard — positions, P&L, universe, config. iOS Wheel tab home."""
    try:
        from services.wheel_engine import get_wheel_status
        return get_wheel_status()
    except Exception as e:
        logger.error(f"GET /wheel/status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/positions")
def wheel_positions():
    """Active wheel positions with phase, strikes, premiums, P&L."""
    try:
        from services.wheel_engine import get_active_wheel_positions
        positions = get_active_wheel_positions()
        for p in positions:
            for k, v in p.items():
                if hasattr(v, "isoformat"):
                    p[k] = v.isoformat()
        return {"positions": positions, "count": len(positions)}
    except Exception as e:
        logger.error(f"GET /wheel/positions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/summary")
def wheel_summary():
    """P&L totals — total premium collected, realized P&L, cycle counts."""
    try:
        from services.wheel_engine import get_wheel_summary
        return get_wheel_summary()
    except Exception as e:
        logger.error(f"GET /wheel/summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Trading ───────────────────────────────────────────────────────────────────

@router.get("/scan")
def wheel_scan():
    """
    Scan current universe for put opportunities.
    Returns ranked list WITHOUT placing any orders.
    Safe to call anytime — preview only.
    """
    try:
        from services.wheel_engine import scan_opportunities
        opps = scan_opportunities()
        return {
            "opportunities": opps,
            "count": len(opps),
            "note": "Preview only. POST /wheel/execute to trade.",
        }
    except Exception as e:
        logger.error(f"GET /wheel/scan: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/execute")
def wheel_execute():
    """
    Run full wheel cycle: check expirations → check assignments → scan + place puts.
    Same logic as the Monday 9:45 AM scheduled run.
    """
    try:
        from services.wheel_engine import run_wheel_cycle
        run_wheel_cycle()
        return {"status": "ok", "message": "Wheel cycle complete"}
    except Exception as e:
        logger.error(f"POST /wheel/execute: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/check")
def wheel_check():
    """Check assignments + expirations only. No new trades."""
    try:
        from services.wheel_engine import check_assignments, check_expirations
        check_expirations()
        check_assignments()
        return {"status": "ok", "message": "Assignment and expiration check complete"}
    except Exception as e:
        logger.error(f"POST /wheel/check: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Universe ──────────────────────────────────────────────────────────────────

@router.get("/universe")
def wheel_universe():
    """
    AI-discovered stock universe with scores, reasons, and IV profiles.
    This is what the wheel bot trades — no hardcoded watchlist.
    """
    try:
        from services.wheel_universe import get_universe_details
        universe = get_universe_details()
        return {
            "universe": universe,
            "count": len(universe),
            "note": "Refreshed every Sunday 8 PM ET by AI. POST /wheel/universe/refresh to update now.",
        }
    except Exception as e:
        logger.error(f"GET /wheel/universe: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/universe/refresh")
def wheel_universe_refresh():
    """
    Trigger immediate AI universe refresh.
    AI screens market, selects best 12-15 stocks for wheel, saves to DB.
    Takes 10-30 seconds.
    """
    try:
        from services.wheel_universe import refresh_universe
        result = refresh_universe()
        return {
            "status": "ok",
            "universe": result,
            "count": len(result),
            "message": f"Universe refreshed: {len(result)} stocks selected by AI",
        }
    except Exception as e:
        logger.error(f"POST /wheel/universe/refresh: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Self-improvement / Optimizer ──────────────────────────────────────────────

@router.get("/optimizer")
def wheel_optimizer_stats():
    """
    Per-symbol performance stats — win rates, average P&L, premium yield.
    Shows how the self-improvement engine is scoring each stock.
    """
    try:
        from services.wheel_optimizer import get_optimizer_stats
        return get_optimizer_stats()
    except Exception as e:
        logger.error(f"GET /wheel/optimizer: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/optimizer/run")
def wheel_optimizer_run():
    """
    Trigger manual optimizer run.
    Aggregates performance data, adjusts universe scores, returns weekly report.
    Normally runs automatically every Friday 4:30 PM ET.
    """
    try:
        from services.wheel_optimizer import run_optimizer
        report = run_optimizer()
        return {"status": "ok", "report": report}
    except Exception as e:
        logger.error(f"POST /wheel/optimizer/run: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Config ────────────────────────────────────────────────────────────────────

@router.get("/config")
def wheel_config():
    """
    Current wheel bot configuration.
    Shows paper/live mode, position limits, DTE range, etc.
    Change ALPACA_WHEEL_PAPER on Railway to switch paper → live.
    """
    try:
        from config import settings
        from services.wheel_engine import (
            MAX_ACTIVE_POSITIONS, MIN_PREMIUM_YIELD, MIN_DTE, MAX_DTE,
            TARGET_DELTA, ASSIGNMENT_CALL_BUFFER, _is_paper
        )
        return {
            "mode": "paper" if _is_paper() else "live",
            "paper_env_var": "ALPACA_WHEEL_PAPER",
            "paper_env_value": settings.alpaca_wheel_paper,
            "using_dedicated_wheel_keys": bool(settings.alpaca_wheel_key),
            "max_active_positions": MAX_ACTIVE_POSITIONS,
            "min_premium_yield_pct": round(MIN_PREMIUM_YIELD * 100, 1),
            "min_dte": MIN_DTE,
            "max_dte": MAX_DTE,
            "target_delta": TARGET_DELTA,
            "call_buffer_above_cost_pct": round(ASSIGNMENT_CALL_BUFFER * 100, 0),
            "schedule": {
                "universe_refresh": "Sunday 8:00 PM ET",
                "daily_cycle": "Mon-Fri 9:45 AM ET",
                "optimizer": "Friday 4:30 PM ET",
            },
        }
    except Exception as e:
        logger.error(f"GET /wheel/config: {e}")
        raise HTTPException(status_code=500, detail=str(e))
