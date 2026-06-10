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
