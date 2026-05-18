import logging
import warnings
from contextlib import asynccontextmanager

# Suppress Pydantic v2 serializer warning from alpaca-py SDK
# ("Expected `enum` but got `str`") — caused by alpaca-py passing string values
# to fields typed as enums in their internal models. Harmless but spams logs.
warnings.filterwarnings("ignore", message=".*Expected `enum` but got `str`.*")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from routers import account, positions, orders, trading, news, risk, strategy, performance, geopolitical, predictions, picks, watchlist, eod, finance, prompt, model_settings
from websocket.manager import manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from services import trading_engine
    logger.info("Trading app backend starting up...")
    trading_engine.start()
    logger.info("Trading bot auto-started on launch.")
    yield
    trading_engine.stop()
    logger.info("Trading app backend shut down.")


app = FastAPI(title="Kova", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
