from fastapi import APIRouter, HTTPException
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


@router.get("/macro")
def get_macro():
    from services.macro import get_macro_context, get_sector_rotation
    macro = get_macro_context()
    macro["sector_rotation"] = get_sector_rotation()
    return macro
