import logging
import os
import warnings
from contextlib import asynccontextmanager

# Suppress Pydantic v2 serializer warning from alpaca-py SDK
# ("Expected `enum` but got `str`") — caused by alpaca-py passing string values
# to fields typed as enums in their internal models. Harmless but spams logs.
warnings.filterwarnings("ignore", message=".*Expected `enum` but got `str`.*")

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from routers import account, positions, orders, trading, news, risk, strategy, performance, geopolitical, predictions, picks, watchlist, eod, finance, prompt, model_settings, wheel, pureai, experiments
from websocket.manager import manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from services import trading_engine
    from services.brain.connectors import log_connector_status
    logger.info("Trading app backend starting up...")
    log_connector_status()
    trading_engine.start()
    logger.info("Trading bot auto-started on launch.")
    from services.alerts import alert_system_start
    alert_system_start()
    # Wheel bot scheduler — runs independently alongside Kova
    from services.wheel_scheduler import start_wheel_scheduler
    start_wheel_scheduler()
    logger.info("Wheel bot scheduler started.")
    # Pure-AI experiment scheduler — third isolated book (no-op if keys unset)
    from services.pureai_engine import start_pureai_scheduler
    start_pureai_scheduler()
    # Experiment engines — squeeze, spillover, revision (no-op if keys unset)
    try:
        from services.squeeze_engine import start_squeeze_scheduler
        start_squeeze_scheduler()
    except Exception as e:
        logger.warning(f"Squeeze scheduler not started: {e}")
    try:
        from services.spillover_engine import start_spillover_scheduler
        start_spillover_scheduler()
    except Exception as e:
        logger.warning(f"Spillover scheduler not started: {e}")
    try:
        from services.revision_engine import start_revision_scheduler
        start_revision_scheduler()
    except Exception as e:
        logger.warning(f"Revision scheduler not started: {e}")
    yield
    trading_engine.stop()
    from services.alerts import alert_system_stop
    alert_system_stop("Trading engine shut down gracefully.")
    logger.info("Trading app backend shut down.")


app = FastAPI(title="Lakshmi", version="1.0.0", lifespan=lifespan)

# ── API key auth ───────────────────────────────────────────────────────────────
# Set KOVA_API_KEY in Railway env vars. iOS app must send X-API-Key header.
# /health is exempt so Railway health checks still work.
_KOVA_API_KEY = os.getenv("KOVA_API_KEY", "")

class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        if _KOVA_API_KEY:
            key = request.headers.get("X-API-Key", "")
            if key != _KOVA_API_KEY:
                return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
        return await call_next(request)

app.add_middleware(APIKeyMiddleware)

# ── CORS — restrict to your Railway domain only ───────────────────────────────
_ALLOWED_ORIGINS = [
    "https://kova-trading-production.up.railway.app",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key"],
)

app.include_router(account.router)
app.include_router(positions.router)
app.include_router(orders.router)
app.include_router(trading.router)
app.include_router(news.router)
app.include_router(risk.router)
app.include_router(strategy.router)
app.include_router(performance.router)
app.include_router(geopolitical.router)
app.include_router(predictions.router)
app.include_router(picks.router)
app.include_router(watchlist.router)
app.include_router(eod.router)
app.include_router(finance.router)
app.include_router(prompt.router)
app.include_router(model_settings.router)
app.include_router(wheel.router)
app.include_router(pureai.router)
app.include_router(experiments.router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    import asyncio
    await manager.connect(websocket)
    try:
        while True:
            try:
                # Wait for client message with 30s timeout
                await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                # No message in 30s — send ping to keep connection alive
                # Prevents Railway proxy and iOS NAT from dropping idle connections
                await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
