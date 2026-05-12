import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def should_confirm_entry(
    symbol: str,
    action: str,
    closing_prices: list[float],
    current_price: float,
    strategy_key: str = "balanced",
    positions_count: int = 0,
) -> tuple[bool, str]:
    """
    Strategy-aware entry confirmation.

    AGGRESSIVE mode (designed for 2-5 trades/day):
    - Only blocks on confirmed panic selling (>5% down + volume collapse)
    - RSI ceiling raised to 80 — momentum stocks legitimately run this hot
    - MA20 extension raised to 10% — breakouts happen above MA20 by definition
    - If portfolio has < 3 positions, entry bar is lowered further

    BALANCED / CONSERVATIVE mode (original rules):
    - Down >2% from yesterday → skip
    - RSI > 75 → skip
    - Price > MA20 * 1.05 → skip
    """
    if not closing_prices or len(closing_prices) < 2:
        return True, "Insufficient data for confirmation, proceeding anyway"

    from services.indicators import compute_rsi, compute_moving_averages

    yesterday_close = closing_prices[-2]
    rsi = compute_rsi(closing_prices)
    mas = compute_moving_averages(closing_prices)
    ma20 = mas.get("ma20")
    is_aggressive = strategy_key == "aggressive"

    # When portfolio is thin (< 3 positions) be more accepting — idle cash is dead money
    needs_positions = positions_count < 3

    if action == "buy":
        if is_aggressive:
            # Rule 1: Only block real panic selling (>5% down), not normal gap-downs
            # A stock down 2-4% from yesterday is often a buy-the-dip opportunity
            if current_price < yesterday_close * 0.95 and not needs_positions:
                return False, (
                    f"{symbol} down >5% from yesterday (${yesterday_close:.2f}→${current_price:.2f}) "
                    f"— confirmed weakness, skipping"
                )

            # Rule 2: RSI ceiling at 80 — momentum stocks often run 75-85 on breakout days
            if rsi > 80:
                return False, f"{symbol} RSI {rsi:.1f} — extremely overbought even for aggressive"

            # Rule 3: Don't chase if price is >10% above MA20 (parabolic, not breakout)
            if ma20 and current_price > ma20 * 1.10 and not needs_positions:
                return False, (
                    f"{symbol} is 10%+ above MA20 (${ma20:.2f}) — parabolic, not a breakout"
                )

            return True, (
                f"{symbol} approved [AGGRESSIVE]: RSI={rsi:.1f}, "
                f"vs yesterday {((current_price/yesterday_close)-1)*100:+.1f}%"
            )

        else:
            # Original conservative/balanced rules
            if current_price < yesterday_close * 0.98:
                return False, (
                    f"{symbol} is down >2% from yesterday's close "
                    f"(${yesterday_close:.2f}→${current_price:.2f}) — waiting for stabilization"
                )
            if rsi > 75:
                return False, f"{symbol} RSI is {rsi:.1f} — overbought, waiting for pullback"
            if ma20 and current_price > ma20 * 1.05:
                return False, (
                    f"{symbol} is 5%+ above MA20 (${ma20:.2f}) — avoid chasing"
                )
            return True, f"{symbol} passes entry confirmation: RSI={rsi:.1f}, price vs MA20 ok"

    elif action == "sell":
        # Don't sell at the absolute bottom — may bounce
        rsi_floor = 20 if is_aggressive else 25
        if rsi < rsi_floor:
            return False, f"{symbol} RSI is {rsi:.1f} — oversold, may bounce, holding"
        return True, f"{symbol} sell confirmed: RSI={rsi:.1f}"

    return True, "No confirmation needed for hold"


def get_scale_in_quantity(
    base_quantity: int,
    confidence: str,
    existing_position_qty: float = 0,
    max_total_qty: int = 0,
    strategy_key: str = "balanced",
) -> int:
    """
    Position sizing on entry.

    AGGRESSIVE: Take full position on first entry — the entry confirmation
    already validated the trade, timid scale-in loses alpha on the best moves.
    Only scale in when adding to an existing position.

    BALANCED / CONSERVATIVE: Original 50-75% scale-in logic.
    """
    if existing_position_qty > 0:
        # Adding to existing — buy the remainder up to max
        remaining = max(0, max_total_qty - int(existing_position_qty))
        return max(1, min(remaining, base_quantity))

    if strategy_key == "aggressive":
        # Full position on first entry for high/medium confidence
        return max(1, base_quantity)

    # Balanced / conservative — original scale-in
    scale_pct = 0.75 if confidence == "high" else 0.50
    return max(1, int(base_quantity * scale_pct))


def should_scale_out(
    position_unrealized_pl_percent: float,
    rsi: float,
    symbol: str,
    strategy_key: str = "balanced",
    high_watermark: float = None,
    current_price: float = None,
    trail_pct: float = 0.05,
) -> tuple[bool, float, str]:
    """
    Partial profit-taking and loss-cutting rules, with trailing stop support.

    Trailing stop logic:
    - Tracks the peak price of each position (high_watermark)
    - If price drops trail_pct% from peak while still in profit → exit to lock in gains
    - Only triggers after position is profitable — doesn't replace the hard stop loss

    AGGRESSIVE: Let winners run longer before taking profits.
    Tighter loss-cutting to redeploy cash into better opportunities.
    """
    is_aggressive = strategy_key == "aggressive"

    # ── Trailing stop: exit if price drops trail_pct% from peak while in profit ──
    if high_watermark and current_price and high_watermark > 0 and position_unrealized_pl_percent > 2.0:
        drop_from_peak = (high_watermark - current_price) / high_watermark
        if drop_from_peak >= trail_pct:
            return True, 1.0, (
                f"{symbol} trailing stop hit: dropped {drop_from_peak*100:.1f}% from peak "
                f"${high_watermark:.2f} → ${current_price:.2f} "
                f"(trail={trail_pct*100:.0f}%, still up {position_unrealized_pl_percent:.1f}% from entry) — locking in gains"
            )

    if is_aggressive:
        # Let winners breathe more — only trim at 20%+, cut hard at 6%+ loss
        if position_unrealized_pl_percent >= 20.0:
            return True, 0.50, (
                f"{symbol} up {position_unrealized_pl_percent:.1f}% — taking 50% profits, letting 50% ride"
            )
        elif position_unrealized_pl_percent >= 12.0 and rsi > 75:
            return True, 0.33, (
                f"{symbol} up {position_unrealized_pl_percent:.1f}% with RSI {rsi:.1f} — trimming 33%"
            )
        elif position_unrealized_pl_percent <= -6.0:
            return True, 1.0, (
                f"{symbol} down {position_unrealized_pl_percent:.1f}% — cutting losses, redeploying cash"
            )
    else:
        # Original balanced rules
        if position_unrealized_pl_percent >= 15.0:
            return True, 0.75, (
                f"{symbol} up {position_unrealized_pl_percent:.1f}% — taking 75% profits"
            )
        elif position_unrealized_pl_percent >= 8.0 and rsi > 70:
            return True, 0.50, (
                f"{symbol} up {position_unrealized_pl_percent:.1f}% with RSI {rsi:.1f} — taking 50% profits"
            )
        elif position_unrealized_pl_percent <= -5.0 and rsi < 35:
            return True, 1.0, (
                f"{symbol} down {position_unrealized_pl_percent:.1f}% with RSI {rsi:.1f} — cutting losses"
            )

    return False, 0.0, ""


def is_good_trading_window() -> tuple[bool, str]:
    """
    Time-of-day filter. Avoids the chaotic first 15 minutes after open
    where spreads are wide and moves are often reversed.
    Returns (should_trade: bool, reason: str)

    Market hours (EST = UTC-4 in summer, UTC-5 in winter):
    - 9:30 AM open = 13:30 UTC
    - 9:45 AM = 13:45 UTC  ← start trading
    - 4:00 PM close = 20:00 UTC
    """
    now_utc = datetime.now(timezone.utc)
    hour, minute = now_utc.hour, now_utc.minute
    minutes_since_midnight_utc = hour * 60 + minute

    market_open_utc = 13 * 60 + 30    # 9:30 AM EST
    trading_start_utc = 13 * 60 + 45  # 9:45 AM EST — skip opening volatility
    market_close_utc = 20 * 60        # 4:00 PM EST

    if minutes_since_midnight_utc < trading_start_utc:
        return False, "Opening 15 min window — waiting for volatility to settle"
    if minutes_since_midnight_utc >= market_close_utc:
        return False, "Market closed"

    # Power hour (3-4 PM EST = 19:00-20:00 UTC) — best liquidity for exits
    if minutes_since_midnight_utc >= 19 * 60:
        return True, "Power hour — prime exit window"

    return True, "Normal trading hours"
