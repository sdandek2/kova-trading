from fastapi import APIRouter, HTTPException, BackgroundTasks
from services.prediction_service import get_stock_prediction, get_top_suggestions

router = APIRouter(prefix="/api/predictions", tags=["predictions"])


@router.get("/suggestions")
def suggestions():
    """Top stock/ETF suggestions right now — short and long-term thesis."""
    return {"suggestions": get_top_suggestions()}


@router.get("/{symbol}")
def predict(symbol: str):
    """Full long-term prediction for a specific stock: targets, scenarios, catalysts, risks."""
    symbol = symbol.upper().strip()
    if not symbol or len(symbol) > 10:
        raise HTTPException(status_code=400, detail="Invalid symbol")
    result = get_stock_prediction(symbol)
    return result
