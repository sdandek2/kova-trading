from fastapi import APIRouter
from services.daily_picks_service import get_daily_picks

router = APIRouter(prefix="/api/picks", tags=["picks"])


@router.get("/daily")
def daily_picks(refresh: bool = False):
    """
    High-conviction daily growth picks — short-term (1-2 weeks) and long-term (1-3 months).
    Cached per trading day. Pass ?refresh=true to force a new analysis.
    """
    return get_daily_picks(force_refresh=refresh)
