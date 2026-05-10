from fastapi import APIRouter
from services.geopolitical import get_geopolitical_context, get_trend_forecast
from services.macro import get_macro_context

router = APIRouter(prefix="/api/geopolitical", tags=["geopolitical"])


@router.get("/")
def get_geopolitical():
    geo = get_geopolitical_context()
    macro = get_macro_context()
    geo["trend_forecast"] = get_trend_forecast(macro, geo)
    return geo
