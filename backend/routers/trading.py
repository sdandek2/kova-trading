from fastapi import APIRouter, HTTPException, Query
from models.trade import TradingStatus, AIAnalysis
from services import trading_engine

router = APIRouter(prefix="/api/trading", tags=["trading"])


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
        return [dict(zip(columns, row)) for row in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch trade history: {e}")


@router.get("/macro")
def get_macro():
    from services.macro import get_macro_context, get_sector_rotation
    macro = get_macro_context()
    macro["sector_rotation"] = get_sector_rotation()
    return macro
