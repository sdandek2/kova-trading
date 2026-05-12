from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from services import trading_engine

router = APIRouter(prefix="/api/risk", tags=["risk"])


class RiskSettings(BaseModel):
    daily_loss_limit_pct: float
    stop_loss_pct: float
    take_profit_pct: float
    min_daily_trades: Optional[int] = 2
    afternoon_pressure_hour: Optional[int] = 14   # EST hour, e.g. 14 = 2:00 PM


@router.get("/settings", response_model=RiskSettings)
def get_risk_settings():
    return trading_engine._risk_settings


@router.post("/settings")
def update_risk_settings(settings: RiskSettings):
    trading_engine._risk_settings.update(settings.model_dump(exclude_none=True))
    trading_engine._save_risk_settings(trading_engine._risk_settings)
    return {"message": "Risk settings updated", "settings": trading_engine._risk_settings}
