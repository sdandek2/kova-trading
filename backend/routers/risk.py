from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from services import trading_engine
from services.db import get_trading_budget, set_trading_budget

router = APIRouter(prefix="/api/risk", tags=["risk"])


class RiskSettings(BaseModel):
    daily_loss_limit_pct: float
    stop_loss_pct: float
    take_profit_pct: float
    min_daily_trades: Optional[int] = 3
    afternoon_pressure_hour: Optional[int] = 13   # EST hour, e.g. 13 = 1:00 PM
    max_trades_per_cycle: Optional[int] = 3       # max new buys/shorts per cycle; sells/covers never blocked
    max_penny_position_pct: Optional[float] = 3.0 # max position size % for stocks under $5 (vs normal sizing)
    cycle_interval_seconds: Optional[int] = 600   # bot cycle frequency in seconds (600=10min, 300=5min)
    profit_reserve_pct: Optional[float] = 0.0     # % of each realized profit to move to reserve (0 = disabled)
    max_positions: Optional[int] = 6              # max total open long positions (0 = no limit)
    sector_cap: Optional[int] = 2                 # max positions in same sector (0 = no limit)


@router.get("/settings", response_model=RiskSettings)
def get_risk_settings():
    return trading_engine._risk_settings


@router.post("/settings")
def update_risk_settings(settings: RiskSettings):
    trading_engine._risk_settings.update(settings.model_dump(exclude_none=True))
    trading_engine._save_risk_settings(trading_engine._risk_settings)
    return {"message": "Risk settings updated", "settings": trading_engine._risk_settings}


class BudgetRequest(BaseModel):
    amount: Optional[float] = None  # None or 0 = use full portfolio


@router.get("/budget")
def get_budget():
    """
    Return the active trading budget and real portfolio value.
    trading_budget=null means the bot uses the full portfolio.
    """
    from services.alpaca_service import get_account
    portfolio_value = None
    try:
        account = get_account()
        portfolio_value = float(account.portfolio_value)
    except Exception:
        pass

    budget = get_trading_budget()
    return {
        "trading_budget": budget,
        "portfolio_value": portfolio_value,
        "using_full_portfolio": budget is None or budget <= 0,
    }


@router.post("/budget")
def set_budget(req: BudgetRequest):
    """
    Set the trading budget cap. The bot will size positions as if the portfolio
    is this amount, leaving the rest of your account untouched.
    Pass amount=null or amount=0 to remove the cap (use full portfolio).
    """
    if req.amount and req.amount > 0:
        set_trading_budget(req.amount)
        return {"message": f"Trading budget set to ${req.amount:,.2f}", "trading_budget": req.amount}
    else:
        set_trading_budget(None)
        return {"message": "Trading budget cleared — using full portfolio", "trading_budget": None}


@router.delete("/budget")
def clear_budget():
    """Remove the trading budget cap — bot uses full portfolio value."""
    set_trading_budget(None)
    return {"message": "Trading budget cleared — using full portfolio", "trading_budget": None}
