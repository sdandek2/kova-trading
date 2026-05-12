from fastapi import APIRouter
from services import alpaca_service

router = APIRouter(prefix="/api", tags=["news"])

@router.get("/news")
def get_news():
    return alpaca_service.get_news()
