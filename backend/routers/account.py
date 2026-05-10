from fastapi import APIRouter
from models.account import AccountInfo
from services import alpaca_service

router = APIRouter(prefix="/api", tags=["account"])


@router.get("/account", response_model=AccountInfo)
def get_account():
    return alpaca_service.get_account()


@router.get("/portfolio/history")
def get_portfolio_history(period: str = "1W"):
    return alpaca_service.get_portfolio_history(period=period)
