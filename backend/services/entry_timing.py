import logging

logger = logging.getLogger(__name__)


def should_confirm_entry(
    symbol: str,
    action: str,
    closing_prices: list[float],
    current_price: float,
) -> tuple[bool, str]:
    """
    Checks entry confirmation signals before executing a trade.
    Returns (should_trade: bool, reason: str)

    Confirmation rules for BUY:
    - Price must be above yesterday's close (not buying into weakness)
    - RSI must not be above 75 (not overbought at entry)
    - Price must not be more than 5% above MA20 (not chasing)

    Confirmation rules for SELL:
    - RSI must not be below 25 (not selling at the very bottom)
    """
    if not closing_prices or len(closing_prices) < 2:
        return True, "Insufficient data for confirmation, proceeding anyway"

    from services.indicators import compute_rsi, compute_moving_averages

    yesterday_close = closing_prices[-2]
    rsi = compute_rsi(closing_prices)
    mas = compute_moving_averages(closing_prices)
    ma20 = mas.get("ma20")

    if action == "buy":
        # Rule 1: Don't buy if price is already down >2% from yesterday (negative momentum)
        if current_price < yesterday_close * 0.98:
            return False, (
                f"{symbol} is down >2% from yesterday's close "
                f"(${yesterday_close:.2f} → ${current_price:.2f}) — waiting for stabilization"
            )

        # Rule 2: Don't buy overbought
        if rsi > 75:
            return False, f"{symbol} RSI is {rsi:.1f} — overbought, waiting for pullback"

        # Rule 3: Don't chase — price too far above MA20
        if ma20 and current_price > ma20 * 1.05:
            return False, (
                f"{symbol} is 5%+ above MA20 (${ma20:.2f}) — avoid chasing, wait for pullback"
            )

        return True, f"{symbol} passes entry confirmation: RSI={rsi:.1f}, price vs MA20 ok"

    elif action == "sell":
        # Don't sell at the absolute bottom — may bounce
        if rsi < 25:
            return False, f"{symbol} RSI is {rsi:.1f} — oversold, may bounce, holding"

        return True, f"{symbol} sell confirmed: RSI={rsi:.1f}"

    return True, "No confirmation needed for hold"


def get_scale_in_quantity(
    base_quantity: int,
    confidence: str,
    existing_position_qty: float = 0,
    max_total_qty: int = 0,
) -> int:
    """
    Scale into positions gradually instead of all at once.

    First entry: buy 50% of planned quantity
    If already have position: buy remaining 50% (averaging in)
    High confidence: buy 75% on first entry
    """
    if existing_position_qty > 0:
        # Already have a position — buy the remainder up to max
        remaining = max(0, max_total_qty - int(existing_position_qty))
        return max(1, min(remaining, base_quantity))

    # First entry — scale in based on confidence
    scale_pct = 0.75 if confidence == "high" else 0.50
    return max(1, int(base_quantity * scale_pct))


def should_scale_out(
    position_unrealized_pl_percent: float,
    rsi: float,
    symbol: str,
) -> tuple[bool, float, str]:
    """
    Decides if we should take partial profits on an existing position.
    Returns (should_scale_out: bool, sell_fraction: float, reason: str)

    Rules:
    - Up 15%+: sell 75% (lock in most gains)
    - Up 8%+ and RSI > 70: sell 50% (take half profits)
    - Down 5%+ and RSI < 35: sell 100% (cut losses, stop-loss didn't trigger)
    """
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
