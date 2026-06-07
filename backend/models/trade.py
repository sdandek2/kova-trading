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
    holding_period: str = "intraday"          # "intraday" | "swing" — options routing
    signal_type: str = "momentum"             # passed through from ScoredCandidate


class TradingStatus(BaseModel):
    is_running: bool
    last_analysis_at: Optional[datetime] = None
    next_run_in_seconds: Optional[int] = None
    brain_regime: Optional[str] = None          # bull | bear | chop
    vix_level: Optional[str] = None             # low | normal | high | extreme
    regime_confidence: Optional[float] = None   # 0.0–1.0
    regime_capital_mult: Optional[float] = None # 0.4–1.0


class AIAnalysis(BaseModel):
    reasoning: str
    last_action: Optional[str] = None
    symbol: Optional[str] = None
    timestamp: Optional[datetime] = None
