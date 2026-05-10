from fastapi import APIRouter
from pydantic import BaseModel
from services import trading_engine

router = APIRouter(prefix="/api/risk", tags=["risk"])


class RiskSettings(BaseModel):
    daily_loss_limit_pct: float
    stop_loss_pct: float
    take_profit_pct: float


@router.get("/settings", response_model=RiskSettings)
def get_risk_settings():
    return trading_engine._risk_settings


@router.post("/settings")
def update_risk_settings(settings: RiskSettings):
    trading_engine._risk_settings.update(settings.model_dump())
    return {"message": "Risk settings updated"}
