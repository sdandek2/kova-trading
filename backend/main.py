import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from routers import account, positions, orders, trading, news, risk, strategy, performance, geopolitical, predictions, picks, watchlist
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


@app.get("/health")
def health():
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # keep connection alive
    except WebSocketDisconnect:
        manager.disconnect(websocket)
