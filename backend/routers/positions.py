from fastapi import APIRouter, Query
from models.trade import Position
from services import alpaca_service

router = APIRouter(prefix="/api", tags=["positions"])


@router.get("/positions", response_model=list[Position])
def get_positions():
    return alpaca_service.get_positions()


@router.get("/positions/history")
def get_position_history(limit: int = Query(50, ge=1, le=200)):
    """
    Return closed trade round-trips from position_log.
    Each row has: symbol, entry_time, exit_time, entry_price, exit_price,
    quantity, realized_pl, realized_pl_pct, hold_duration_mins, exit_reason,
    strategy, claude_reasoning, market_regime.
    """
    from services.db import get_position_history
    return get_position_history(limit=limit)


@router.get("/positions/performance")
def get_performance_summary():
    """
    Aggregate stats across all closed trades:
    win rate, avg P&L, total realized profit, best/worst symbols.
    """
    from services.db import get_trade_performance_summary
    return get_trade_performance_summary()


@router.get("/positions/watermarks")
def get_watermarks():
    """
    Return current trailing-stop high watermarks for all open positions.
    iOS uses this to show users exactly where their trailing stop sits.
    """
    from services.trading_engine import _position_high_watermarks
    return {"watermarks": _position_high_watermarks}
