from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from models.trade import Order
from services import alpaca_service

router = APIRouter(prefix="/api", tags=["orders"])


class ManualOrderRequest(BaseModel):
    symbol: str
    side: str   # "buy" or "sell"
    qty: int


@router.get("/orders", response_model=list[Order])
def get_orders():
    return alpaca_service.get_orders(limit=50)


@router.post("/orders/manual")
def place_manual_order(req: ManualOrderRequest):
    symbol = req.symbol.upper().strip()
    if req.side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side must be buy or sell")
    if req.qty < 1:
        raise HTTPException(status_code=400, detail="qty must be at least 1")
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        from config import settings
        client = TradingClient(settings.alpaca_api_key, settings.alpaca_secret_key, paper=True)
        order = client.submit_order(MarketOrderRequest(
            symbol=symbol,
            qty=req.qty,
            side=OrderSide.BUY if req.side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        ))
        return {"status": "submitted", "order_id": str(order.id), "symbol": symbol, "side": req.side, "qty": req.qty}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
