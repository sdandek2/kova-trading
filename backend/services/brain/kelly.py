"""
Kelly Criterion position sizing.

Kelly formula: f = (win_rate * avg_win - loss_rate * avg_loss) / avg_win
We use half-Kelly (50% of Kelly output) to reduce variance — full Kelly
is mathematically optimal but psychologically brutal and practically risky.

Falls back to ATR-based sizing when trade history is insufficient (<10 trades).
Hard cap: never exceed strategy max_position_pct regardless of Kelly output.
"""
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Minimum trades needed before Kelly kicks in.
# Below this, we don't have enough data to trust the win rate.
_MIN_TRADES_FOR_KELLY = 10

# Half-Kelly multiplier — reduces full Kelly by 50% for safety
_HALF_KELLY = 0.5

# Hard floor: always size at least 2% of portfolio even if Kelly says less
_MIN_POSITION_PCT = 0.02

# Hard ceiling per strategy (applied on top of Kelly)
_MAX_POSITION_BY_STRATEGY = {
    "aggressive": 0.15,
    "balanced":   0.10,
    "conservative": 0.05,
}


@dataclass
class KellyResult:
    shares: int
    dollar_amount: float
    kelly_pct: float      # raw Kelly formula output
    applied_pct: float    # half-Kelly after caps
    rationale: str
    used_kelly: bool      # False = fell back to ATR sizing


def _get_win_stats(trade_history: list[dict], signal_type: Optional[str] = None) -> Optional[dict]:
    """
    Extract win rate and avg win/loss from trade history.
    Optionally filter by signal_type for per-setup Kelly.
    Returns None if insufficient data.
    """
    relevant = trade_history or []
    if signal_type:
        filtered = [t for t in relevant if t.get("signal_type") == signal_type]
        if len(filtered) >= _MIN_TRADES_FOR_KELLY:
            relevant = filtered
        # If not enough per-signal-type data, fall back to all trades

    if len(relevant) < _MIN_TRADES_FOR_KELLY:
        return None

    wins = [t for t in relevant if (t.get("pl_pct") or 0) > 0]
    losses = [t for t in relevant if (t.get("pl_pct") or 0) <= 0]

    if not wins or not losses:
        return None

    win_rate = len(wins) / len(relevant)
    avg_win = sum(t.get("pl_pct", 0) for t in wins) / len(wins) / 100   # as decimal
    avg_loss = abs(sum(t.get("pl_pct", 0) for t in losses) / len(losses)) / 100

    return {
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "sample_size": len(relevant),
    }


def kelly_size(
    symbol: str,
    signal_type: Optional[str],
    conviction: str,              # "high" | "medium" | "low"
    portfolio_value: float,
    price: float,
    atr: float,
    trade_history: list[dict],
    strategy_key: str = "aggressive",
    rs_percentile: float = 50.0,
) -> KellyResult:
    """
    Calculate position size using Kelly Criterion when enough history exists,
    otherwise fall back to ATR-based volatility sizing.

    RS percentile bonus: top 20% RS stocks get 10% more size (they have the wind behind them).
    Conviction multiplier: high=1.0x, medium=0.75x, low=0.5x.
    """
    max_pct = _MAX_POSITION_BY_STRATEGY.get(strategy_key, 0.15)
    conviction_mult = {"high": 1.0, "medium": 0.75, "low": 0.5}.get(conviction, 0.75)

    # RS bonus: top 20% of market outperformers get slightly larger positions
    rs_mult = 1.10 if rs_percentile >= 80 else 1.0

    stats = _get_win_stats(trade_history, signal_type)

    if stats and stats["avg_win"] > 0:
        # Kelly formula
        win_rate = stats["win_rate"]
        avg_win = stats["avg_win"]
        avg_loss = stats["avg_loss"]

        raw_kelly = (win_rate * avg_win - (1 - win_rate) * avg_loss) / avg_win
        raw_kelly = max(0.0, raw_kelly)  # Kelly can be negative — floor at 0

        # Half-Kelly + conviction + RS
        applied_pct = raw_kelly * _HALF_KELLY * conviction_mult * rs_mult

        # Apply hard caps
        applied_pct = max(_MIN_POSITION_PCT, min(applied_pct, max_pct))

        dollar_amount = portfolio_value * applied_pct
        shares = max(1, int(dollar_amount / price)) if price > 0 else 1

        rationale = (
            f"Kelly({stats['win_rate']:.0%}wr, {stats['avg_win']*100:.1f}%avgW, "
            f"{stats['avg_loss']*100:.1f}%avgL, n={stats['sample_size']}) "
            f"raw={raw_kelly*100:.1f}% → half={raw_kelly*_HALF_KELLY*100:.1f}% "
            f"× {conviction}({conviction_mult:.2f}) × RS({rs_mult:.2f}) "
            f"= {applied_pct*100:.1f}% = ${dollar_amount:,.0f}"
        )
        logger.info(f"Kelly size {symbol}: {rationale}")

        return KellyResult(
            shares=shares,
            dollar_amount=round(dollar_amount, 2),
            kelly_pct=round(raw_kelly, 4),
            applied_pct=round(applied_pct, 4),
            rationale=rationale,
            used_kelly=True,
        )

    else:
        # Fallback: ATR-based sizing (risk 1.5% of portfolio per ATR unit)
        risk_pct = 0.015 * conviction_mult * rs_mult
        risk_amount = portfolio_value * risk_pct

        if atr > 0 and price > 0:
            shares_by_risk = int(risk_amount / atr)
            shares_by_max = int((portfolio_value * max_pct) / price)
            shares = max(1, min(shares_by_risk, shares_by_max))
        elif price > 0:
            shares = max(1, int((portfolio_value * max_pct * conviction_mult) / price))
        else:
            shares = 1

        dollar_amount = shares * price
        applied_pct = dollar_amount / portfolio_value if portfolio_value > 0 else 0

        rationale = (
            f"ATR fallback (insufficient history < {_MIN_TRADES_FOR_KELLY} trades): "
            f"risk={risk_pct*100:.1f}% × ATR={atr:.2f} → {shares} shares "
            f"= ${dollar_amount:,.0f} ({applied_pct*100:.1f}%)"
        )
        logger.info(f"ATR size {symbol}: {rationale}")

        return KellyResult(
            shares=shares,
            dollar_amount=round(dollar_amount, 2),
            kelly_pct=0.0,
            applied_pct=round(applied_pct, 4),
            rationale=rationale,
            used_kelly=False,
        )


def get_trade_history_for_kelly(limit: int = 100) -> list[dict]:
    """
    Load recent closed trades from DB for Kelly win-rate calculation.
    Returns list of dicts with keys: symbol, signal_type, pl_pct, action.
    """
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return []
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, action,
                       CASE WHEN exit_price IS NOT NULL AND entry_price IS NOT NULL AND entry_price > 0
                            THEN ((exit_price - entry_price) / entry_price * 100)
                            ELSE NULL
                       END as pl_pct
                FROM position_log
                WHERE exit_price IS NOT NULL
                ORDER BY closed_at DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
        return [
            {"symbol": r[0], "action": r[1], "pl_pct": float(r[2]) if r[2] is not None else 0.0}
            for r in rows if r[2] is not None
        ]
    except Exception as e:
        logger.warning(f"Could not load trade history for Kelly ({e}) — using ATR fallback")
        return []
