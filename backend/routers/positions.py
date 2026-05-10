from fastapi import APIRouter
from models.trade import Position
from services import alpaca_service

router = APIRouter(prefix="/api", tags=["positions"])


@router.get("/positions", response_model=list[Position])
def get_positions():
    return alpaca_service.get_positions()
