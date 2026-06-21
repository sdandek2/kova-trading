from fastapi import APIRouter, Query
from services import alpaca_service
from services.alpaca_service import _score_headline

router = APIRouter(prefix="/api", tags=["news"])

@router.get("/news")
def get_news():
    return alpaca_service.get_news()

@router.get("/sentiment")
def get_sentiment(symbols: str = Query(..., description="Comma-separated symbols, e.g. NVDA,INTC,AAPL")):
    """Score today's news for each symbol: positive=bullish, negative=bearish."""
    from collections import defaultdict
    sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    articles = alpaca_service.get_news(limit=50)
    scores: dict = defaultdict(int)
    detail: dict = defaultdict(list)
    for art in articles:
        hl   = art.get("headline") or ""
        sm   = art.get("summary")  or ""
        syms = art.get("symbols")  or []
        direction = _score_headline(hl, sm)
        label = "bullish" if direction == 1 else "bearish" if direction == -1 else "neutral"
        for sym in syms:
            if sym in sym_list:
                scores[sym] += direction
                detail[sym].append({"headline": hl, "direction": label})
    result = []
    for sym in sym_list:
        net   = scores.get(sym, 0)
        pts   = max(-15, min(20, net * 5))
        result.append({
            "symbol":        sym,
            "net_score":     net,
            "signal_points": pts,
            "sentiment":     "bullish" if net > 0 else "bearish" if net < 0 else "neutral",
            "articles":      detail.get(sym, []),
        })
    return result
