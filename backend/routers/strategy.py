from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from services import strategy as strategy_service

router = APIRouter(prefix="/api/strategy", tags=["strategy"])


@router.get("/")
def get_strategy():
    return strategy_service.get_strategy()


@router.get("/all")
def get_all_strategies():
    return strategy_service.get_all_strategies()


@router.post("/set/{key}")
def set_strategy(key: str):
    if not strategy_service.set_strategy(key):
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {key}. Choose from: conservative, balanced, aggressive")
    return {"message": f"Strategy set to {key}"}
