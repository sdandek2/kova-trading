from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class Position(BaseModel):
    symbol: str
    qty: float
    side: str = "long"                   # "long" | "short"
    avg_entry_price: float
    current_price: float
    unrealized_pl: float
    unrealized_pl_percent: float
    market_value: float


class Order(BaseModel):
    id: str
    symbol: str
    side: str  # "buy" | "sell" | "short"
    qty: float
    status: str
    filled_avg_price: Optional[float] = None
    created_at: datetime


class TradeDecision(BaseModel):
    action: str                          # "buy" | "sell" | "hold" | "short"
    symbol: Optional[str] = None
    quantity: Optional[int] = None
    reasoning: str
    take_profit_pct: Optional[float] = None   # e.g. 0.15 = 15% — Claude sets dynamically
    stop_loss_pct: Optional[float] = None     # e.g. 0.05 = 5% trailing stop
    partial_exit: bool = False                # sell 50% at TP, let other 50% ride
    high_conviction: bool = False             # A+ breakout/catalyst fast lane
    predictive_expectancy_pct: Optional[float] = None
    predictive_trades: int = 0


class TradingStatus(BaseModel):
    is_running: bool
    last_analysis_at: Optional[datetime] = None
    next_run_in_seconds: Optional[int] = None


class AIAnalysis(BaseModel):
    reasoning: str
    last_action: Optional[str] = None
    symbol: Optional[str] = None
    timestamp: Optional[datetime] = None
