"""
routers/wheel.py — Wheel Bot API endpoints

Powers the iOS "Wheel" tab. All reads/writes go to the wheel_positions table.
No interaction with Kova's trading logic.
"""

import logging
from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/wheel", tags=["wheel"])


@router.get("/status")
def wheel_status():
    """
    Full wheel dashboard — active positions, P&L, config.
    iOS Wheel tab loads this on open.
    """
    try:
        from services.wheel_engine import get_wheel_status
        return get_wheel_status()
    except Exception as e:
        logger.error(f"GET /wheel/status error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/positions")
def wheel_positions():
    """Active wheel positions only."""
    try:
        from services.wheel_engine import get_active_wheel_positions
        positions = get_active_wheel_positions()
        # Serialize dates
        for p in positions:
            for k, v in p.items():
                if hasattr(v, "isoformat"):
                    p[k] = v.isoformat()
        return {"positions": positions, "count": len(positions)}
    except Exception as e:
        logger.error(f"GET /wheel/positions error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/scan")
def wheel_scan():
    """
    Run opportunity scan and return results WITHOUT placing orders.
    Use this to preview what the bot would do.
    """
    try:
        from services.wheel_engine import scan_opportunities
        opportunities = scan_opportunities()
        return {
            "opportunities": opportunities,
            "count": len(opportunities),
            "note": "Preview only — no orders placed. Use POST /wheel/execute to trade.",
        }
    except Exception as e:
        logger.error(f"GET /wheel/scan error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/execute")
def wheel_execute():
    """
    Run a full wheel cycle: scan + place best put orders.
    Respects MAX_ACTIVE_POSITIONS cap.
    Safe to call manually — same logic as the Monday morning cron.
    """
    try:
        from services.wheel_engine import run_wheel_cycle
        run_wheel_cycle()
        return {"status": "ok", "message": "Wheel cycle executed"}
    except Exception as e:
        logger.error(f"POST /wheel/execute error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/check")
def wheel_check():
    """
    Check assignments + expirations only (no new trades).
    Run this if you want to update position states without placing new orders.
    """
    try:
        from services.wheel_engine import check_assignments, check_expirations
        check_expirations()
        check_assignments()
        return {"status": "ok", "message": "Assignment and expiration check complete"}
    except Exception as e:
        logger.error(f"POST /wheel/check error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/summary")
def wheel_summary():
    """P&L summary — total premium collected, realized P&L, active vs completed."""
    try:
        from services.wheel_engine import get_wheel_summary
        summary = get_wheel_summary()
        return summary
    except Exception as e:
        logger.error(f"GET /wheel/summary error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
