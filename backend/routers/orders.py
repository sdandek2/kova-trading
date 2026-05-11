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

    # Validate symbol exists and is tradeable
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import MarketOrderRequest, GetAssetsRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, AssetStatus
        from config import settings
        client = TradingClient(settings.alpaca_api_key, settings.alpaca_secret_key, paper=True)
        try:
            asset = client.get_asset(symbol)
            if not asset.tradable:
                raise HTTPException(status_code=400, detail=f"{symbol} is not currently tradeable")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=400, detail=f"Symbol '{symbol}' not found or cannot be traded")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # For sell orders, verify the user actually owns enough shares
    if req.side == "sell":
        positions = alpaca_service.get_positions()
        position = next((p for p in positions if p.symbol == symbol), None)
        if position is None:
            raise HTTPException(status_code=400, detail=f"No position in {symbol}")
        owned = int(position.qty)
        if req.qty > owned:
            raise HTTPException(status_code=400, detail=f"You only own {owned} shares of {symbol}")

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


@router.delete("/orders/{order_id}")
def cancel_order(order_id: str):
    try:
        from alpaca.trading.client import TradingClient
        from config import settings
        client = TradingClient(settings.alpaca_api_key, settings.alpaca_secret_key, paper=True)
        client.cancel_order_by_id(order_id)
        return {"status": "cancelled", "order_id": order_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
