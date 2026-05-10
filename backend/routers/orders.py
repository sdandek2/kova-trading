from fastapi import APIRouter
from models.trade import Order
from services import alpaca_service

router = APIRouter(prefix="/api", tags=["orders"])


@router.get("/orders", response_model=list[Order])
def get_orders():
    return alpaca_service.get_orders(limit=50)
