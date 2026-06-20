"""Pure-AI experiment endpoints — fully isolated from Kova and Wheel."""
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from services import pureai_engine

router = APIRouter(prefix="/pureai", tags=["pureai"])


class PureAISettings(BaseModel):
    enabled: Optional[bool] = None
    max_buys_per_cycle: Optional[int] = None
    cycle_interval_minutes: Optional[int] = None
    max_position_pct: Optional[float] = None
    max_searches_per_cycle: Optional[int] = None
    model: Optional[str] = None
    profit_reserve_pct: Optional[float] = None


class ReserveResetRequest(BaseModel):
    confirm: bool = False
    amount: Optional[float] = None


@router.get("/status")
def status():
    return pureai_engine.get_pureai_status()


@router.get("/config")
def get_config():
    cfg = pureai_engine.get_pureai_settings()
    cfg["model_options"] = pureai_engine.PUREAI_MODEL_OPTIONS
    return cfg


@router.post("/config")
def update_config(body: PureAISettings):
    updated = pureai_engine.update_pureai_settings(
        body.model_dump(exclude_none=True))
    return {"message": "PureAI settings updated", "settings": updated}


@router.post("/run")
def run_cycle():
    """Trigger one decision cycle manually (works even when market closed)."""
    return pureai_engine.run_pureai_cycle(force=True)


@router.get("/decisions")
def decisions(limit: int = 20):
    return pureai_engine.get_recent_decisions(limit=min(limit, 100))


@router.get("/reserve")
def get_reserve():
    """Return current PureAI profit reserve balance and settings."""
    reserved = pureai_engine.get_pureai_reserve()
    cfg = pureai_engine.get_pureai_settings()
    reserve_pct = float(cfg.get("profit_reserve_pct", 0.0))
    return {
        "reserved_cash":      round(reserved, 2),
        "profit_reserve_pct": reserve_pct,
        "enabled":            reserve_pct > 0,
        "message":            f"${reserved:,.2f} set aside from Pure-AI profits — not used for trading."
                              if reserved > 0 else "No profits reserved yet.",
    }


@router.post("/reserve/reset")
def reset_reserve(req: ReserveResetRequest):
    """Withdraw from the PureAI profit reserve. Requires confirm=True."""
    if not req.confirm:
        return {"error": "Pass confirm=true to withdraw the reserve balance."}
    from services.db import cache_set
    current = pureai_engine.get_pureai_reserve()
    if req.amount is not None:
        if float(req.amount) < 0:
            return {"error": "amount must be non-negative"}
        deduct = min(float(req.amount), current)
    else:
        deduct = current
    new_balance = round(current - deduct, 2)
    cache_set(pureai_engine._RESERVE_CACHE_KEY, new_balance, 365 * 24 * 3600)
    return {
        "withdrawn":   round(deduct, 2),
        "new_balance": new_balance,
        "message":     f"${deduct:,.2f} withdrawn from Pure-AI reserve. New balance: ${new_balance:,.2f}",
    }
