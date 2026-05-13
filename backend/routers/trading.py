from fastapi import APIRouter, HTTPException, Query
from models.trade import TradingStatus, AIAnalysis
from services import trading_engine

router = APIRouter(prefix="/api/trading", tags=["trading"])


@router.get("/activity")
def get_bot_activity(limit: int = Query(50, ge=1, le=200)):
    """
    Return recent bot activity events: scans, approvals, rejections,
    earnings blocks, circuit breakers, trailing stop triggers.
    Powers the real-time activity log in the iOS app.
    """
    from services.db import get_bot_activity_log
    return get_bot_activity_log(limit=limit)


@router.get("/status", response_model=TradingStatus)
def get_status():
    return trading_engine.get_status()


@router.post("/start")
async def start_trading():
    trading_engine.start()
    return {"message": "Trading bot started"}


@router.post("/stop")
async def stop_trading():
    trading_engine.stop()
    return {"message": "Trading bot stopped"}


@router.get("/analysis", response_model=AIAnalysis)
def get_analysis():
    analysis = trading_engine.get_latest_analysis()
    if not analysis:
        return AIAnalysis(reasoning="Waiting for first analysis cycle...")
    return analysis


@router.get("/analysis/recent")
def get_recent_ai_decisions(limit: int = Query(20, ge=1, le=100)):
    """
    Return the most recent AI decisions (approved trades + entry rejections).
    Each item includes: timestamp, symbol, event_type, message (full reasoning).
    Powers the 'Recent AI Decisions' list in the iOS app.
    """
    from services.db import get_bot_activity_log
    all_events = get_bot_activity_log(limit=200)
    decisions = [
        e for e in all_events
        if e.get("event_type") in ("approved", "order_placed", "entry_rejected", "earnings_block", "circuit_breaker")
    ]
    return decisions[:limit]


@router.get("/history")
def get_trade_history(limit: int = Query(50, ge=1, le=500)):
    """Return the most recent AI trade decisions from the trade_log table."""
    from services.db import _get_conn
    conn = _get_conn()
    if not conn:
        raise HTTPException(status_code=503, detail="Database unavailable")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, timestamp, action, symbol, quantity,
                       reasoning, confidence, market_regime, geo_risk
                FROM trade_log
                ORDER BY timestamp DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        columns = ["id", "timestamp", "action", "symbol", "quantity",
                   "reasoning", "confidence", "market_regime", "geo_risk"]
        # Bug fix: psycopg2 returns timestamp as a datetime object which FastAPI
        # cannot JSON-serialize from a plain dict. Convert to ISO string explicitly.
        result = []
        for row in rows:
            d = dict(zip(columns, row))
            if d.get("timestamp") and hasattr(d["timestamp"], "isoformat"):
                d["timestamp"] = d["timestamp"].isoformat()
            result.append(d)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch trade history: {e}")


@router.get("/macro")
def get_macro():
    from services.macro import get_macro_context, get_sector_rotation
    macro = get_macro_context()
    macro["sector_rotation"] = get_sector_rotation()
    return macro


@router.get("/premarket")
def get_premarket():
    """
    Return the latest pre-market scan result.
    Runs automatically 9:00-9:30 AM EST each trading day.
    Returns top stocks by news sentiment + macro regime + headlines.
    """
    from services.db import cache_get
    data = cache_get("premarket_scan")
    if not data:
        return {
            "available": False,
            "message": "Pre-market scan runs at 9:00-9:30 AM EST before market open.",
            "scanned_at": None,
            "macro_regime": None,
            "top_stocks": [],
            "headlines": [],
        }

    # Sort sentiment dict into ranked list
    sentiment = data.get("sentiment", {})
    top_stocks = sorted(
        [{"symbol": sym, "mentions": cnt} for sym, cnt in sentiment.items()],
        key=lambda x: x["mentions"],
        reverse=True,
    )[:10]

    return {
        "available": True,
        "scanned_at": data.get("scanned_at"),
        "macro_regime": data.get("macro_regime"),
        "top_stocks": top_stocks,
        "headlines": data.get("headlines", [])[:10],
    }
