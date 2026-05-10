from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class Position(BaseModel):
    symbol: str
    qty: float
    avg_entry_price: float
    current_price: float
    unrealized_pl: float
    unrealized_pl_percent: float
    market_value: float


class Order(BaseModel):
    id: str
    symbol: str
    side: str  # "buy" | "sell"
    qty: float
    status: str
    filled_avg_price: Optional[float] = None
    created_at: datetime


class TradeDecision(BaseModel):
    action: str  # "buy" | "sell" | "hold"
    symbol: Optional[str] = None
    quantity: Optional[int] = None
    reasoning: str


class TradingStatus(BaseModel):
    is_running: bool
    last_analysis_at: Optional[datetime] = None
    next_run_in_seconds: Optional[int] = None


class AIAnalysis(BaseModel):
    reasoning: str
    last_action: Optional[str] = None
    symbol: Optional[str] = None
    timestamp: Optional[datetime] = None
