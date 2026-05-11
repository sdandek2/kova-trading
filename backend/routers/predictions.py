import httpx
from fastapi import APIRouter, HTTPException, Query
from services.prediction_service import get_stock_prediction, get_top_suggestions

router = APIRouter(prefix="/api/predictions", tags=["predictions"])


@router.get("/suggestions")
def suggestions():
    """Top stock/ETF suggestions right now — short and long-term thesis."""
    return {"suggestions": get_top_suggestions()}


@router.get("/search")
def search_ticker(q: str = Query(..., min_length=1, description="Company name or partial ticker")):
    """
    Search for ticker symbols by company name.
    Example: GET /api/predictions/search?q=apple
    Returns EQUITY quotes matching the query from Yahoo Finance.
    """
    url = (
        f"https://query1.finance.yahoo.com/v1/finance/search"
        f"?q={q}&quotesCount=5&newsCount=0"
    )
    try:
        resp = httpx.get(url, timeout=5.0, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Yahoo Finance search failed: {e}")

    quotes = payload.get("quotes", [])
    results = []
    for q_item in quotes:
        if q_item.get("quoteType") != "EQUITY":
            continue
        results.append({
            "symbol": q_item.get("symbol", ""),
            "name": q_item.get("longname") or q_item.get("shortname") or "",
            "exchange": q_item.get("exchange", ""),
        })

    return {"results": results}


@router.get("/{symbol}")
def predict(symbol: str):
    """Full long-term prediction for a specific stock: targets, scenarios, catalysts, risks."""
    symbol = symbol.upper().strip()
    if not symbol or len(symbol) > 10:
        raise HTTPException(status_code=400, detail="Invalid symbol")
    result = get_stock_prediction(symbol)
    return result
