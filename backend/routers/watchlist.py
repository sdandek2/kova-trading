import json
from pathlib import Path
from fastapi import APIRouter

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

WATCHLIST_FILE = Path(__file__).parent.parent / "watchlist.json"
DEFAULT_WATCHLIST = ["AAPL", "MSFT", "GOOGL", "TSLA", "SPY", "NVDA"]


def load_watchlist() -> list[str]:
    if WATCHLIST_FILE.exists():
        try:
            return json.loads(WATCHLIST_FILE.read_text())
        except Exception:
            pass
    return DEFAULT_WATCHLIST.copy()


def save_watchlist(symbols: list[str]):
    WATCHLIST_FILE.write_text(json.dumps(symbols))


@router.get("/")
def get_watchlist():
    return {"watchlist": load_watchlist()}


@router.post("/")
def set_watchlist(body: dict):
    symbols = [s.upper().strip() for s in body.get("watchlist", []) if s.strip()]
    symbols = list(dict.fromkeys(symbols))  # deduplicate, preserve order
    save_watchlist(symbols)
    return {"watchlist": symbols}
