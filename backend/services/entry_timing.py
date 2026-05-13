import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Ticker-aware MA20 thresholds ───────────────────────────────────────────
# Leveraged ETFs (3×) naturally run 15-20% above MA20 during bull phases.
# A flat 10% block misfires constantly on these instruments.
_LEVERAGED_ETFS = {
    "TQQQ", "SQQQ", "SOXL", "SOXS", "SPXL", "SPXS", "UPRO", "SPXU",
    "TECL", "TECS", "LABU", "LABD", "FNGU", "FNGS", "CURE", "DFEN",
    "TNA", "TZA", "UDOW", "SDOW", "URTY", "SRTY",
}

# Broad non-leveraged ETFs — slightly wider band than individual stocks
_BROAD_ETFS = {
    "QQQ", "SPY", "IWM", "XLE", "XLK", "XLF", "XLV", "XLI", "GLD",
    "SLV", "EEM", "EFA", "VTI", "VOO", "ARKK", "ARKW", "ARKG",
}


def _ma20_extension_limit(symbol: str) -> float:
    """
    Return the maximum allowed % extension above MA20 for this symbol.
    - Leveraged 3× ETFs: 18%  (they routinely run hot in bull markets)
    - Broad / non-leveraged ETFs: 12%
    - Individual stocks: 10% (original rule)
    """
    if symbol in _LEVERAGED_ETFS:
        return 0.18
    if symbol in _BROAD_ETFS:
        return 0.12
    return 0.10


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

            # Rule 3: Ticker-aware MA20 extension limit.
            # Leveraged ETFs (TQQQ/SOXL/SPXL): 18% allowed — they run hot in bull markets.
            # Broad ETFs (QQQ/SPY/XLE): 12%.  Individual stocks: 10%.
            # When portfolio is thin (<3 positions) we lower the bar, but NEVER bypass
            # the check entirely — a hard cap at 1.5× the normal limit prevents chasing
            # something already 30-40% extended (e.g. QUCY at 42% above MA20).
            if ma20:
                ext_limit = _ma20_extension_limit(symbol)
                hard_cap  = ext_limit * 1.5   # e.g. 15% for stocks, 27% for leveraged ETFs
                pct_above  = (current_price / ma20 - 1) * 100

                if needs_positions:
                    # Thin portfolio: allow up to hard_cap (not unlimited)
                    if current_price > ma20 * (1 + hard_cap):
                        return False, (
                            f"{symbol} is {pct_above:.0f}% above MA20 (${ma20:.2f}) — "
                            f"too extended even with thin portfolio (cap={hard_cap*100:.0f}%)"
                        )
                else:
                    if current_price > ma20 * (1 + ext_limit):
                        return False, (
                            f"{symbol} is {pct_above:.0f}%+ above MA20 (${ma20:.2f}) — "
                            f"parabolic, not a breakout (limit={ext_limit*100:.0f}%)"
                        )

            return True, (
                f"{symbol} approved [AGGRESSIVE]: RSI={rsi:.1f}, "
                f"vs yesterday {((current_price/yesterday_close)-1)*100:+.1f}%"
            )

        else:
            # Conservative/balanced rules — same ticker-aware MA20 limits but tighter RSI
            if current_price < yesterday_close * 0.98:
                return False, (
                    f"{symbol} is down >2% from yesterday's close "
                    f"(${yesterday_close:.2f}→${current_price:.2f}) — waiting for stabilization"
                )
            if rsi > 75:
                return False, f"{symbol} RSI is {rsi:.1f} — overbought, waiting for pullback"
            if ma20:
                # Use half the aggressive limit for conservative mode (5% stocks, 9% leveraged ETFs)
                ext_limit = _ma20_extension_limit(symbol) * 0.5
                pct_above  = (current_price / ma20 - 1) * 100
                if current_price > ma20 * (1 + ext_limit):
                    return False, (
                        f"{symbol} is {pct_above:.0f}%+ above MA20 (${ma20:.2f}) — avoid chasing"
                    )
            return True, f"{symbol} passes entry confirmation: RSI={rsi:.1f}, price vs MA20 ok"

    elif action == "sell":
        # Don't sell at the absolute bottom — may bounce
        rsi_floor = 20 if is_aggressive else 25
        if rsi < rsi_floor:
            return False, f"{symbol} RSI is {rsi:.1f} — oversold, may bounce, holding"
        return True, f"{symbol} sell confirmed: RSI={rsi:.1f}"

    elif action == "short":
        # Don't short into deeply oversold conditions — high bounce risk
        if rsi < 35:
            return False, f"{symbol} RSI {rsi:.1f} — oversold, short bounce risk, skipping"
        # Don't short a stock already down >5% today — late entry, most of the move is gone
        if current_price < yesterday_close * 0.95:
            return False, f"{symbol} already down >5% today — late short entry, skipping"
        # Don't short in aggressive mode if RSI < 45 — could be bottoming, not breaking down
        if is_aggressive and rsi < 45:
            return False, f"{symbol} RSI {rsi:.1f} — not overbought enough to short confidently"
        return True, f"{symbol} short confirmed: RSI={rsi:.1f}, price vs yesterday ok"

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


def should_cover_short(
    symbol: str,
    position_unrealized_pl_percent: float,
    rsi: float,
    low_watermark: float = None,
    current_price: float = None,
    trail_pct: float = 0.05,
    strategy_key: str = "aggressive",
) -> tuple[bool, float, str]:
    """
    Exit logic for short positions (cover = buy back to close).

    Profits when price FALLS. Rules mirror long side but inverted:
    - Trailing stop: if price rises trail_pct% from the lowest point seen → cover to lock gains
    - Take profit: cover at 12%+ gain
    - Stop loss: cover if position is down 5%+ (price rose against us)
    """
    is_aggressive = strategy_key == "aggressive"

    # ── Trailing stop for shorts: cover if price rises trail_pct% from low watermark ──
    if low_watermark and current_price and low_watermark > 0 and position_unrealized_pl_percent > 2.0:
        rise_from_low = (current_price - low_watermark) / low_watermark
        if rise_from_low >= trail_pct:
            return True, 1.0, (
                f"{symbol} SHORT trailing stop: price rose {rise_from_low*100:.1f}% from low "
                f"${low_watermark:.2f} → ${current_price:.2f} "
                f"(still up {position_unrealized_pl_percent:.1f}% from entry) — locking in short gains"
            )

    if is_aggressive:
        if position_unrealized_pl_percent >= 15.0:
            return True, 0.50, f"{symbol} SHORT up {position_unrealized_pl_percent:.1f}% — covering half, letting rest ride"
        elif position_unrealized_pl_percent >= 10.0 and rsi < 35:
            return True, 0.33, f"{symbol} SHORT up {position_unrealized_pl_percent:.1f}% with RSI {rsi:.1f} oversold — trimming 33%"
        elif position_unrealized_pl_percent <= -5.0:
            return True, 1.0, f"{symbol} SHORT down {abs(position_unrealized_pl_percent):.1f}% (price rose) — covering to stop loss"
    else:
        if position_unrealized_pl_percent >= 12.0:
            return True, 1.0, f"{symbol} SHORT hit target +{position_unrealized_pl_percent:.1f}% — covering"
        elif position_unrealized_pl_percent <= -4.0:
            return True, 1.0, f"{symbol} SHORT stop: down {abs(position_unrealized_pl_percent):.1f}% — covering"

    return False, 0.0, ""


def is_good_trading_window() -> tuple[str, str]:
    """
    Time-of-day filter.
    Returns (mode: str, reason: str) where mode is one of:
      "full"        — normal trading, entries + exits allowed
      "exits_only"  — first 15 min after open: allow AI exits, block new entries
      "closed"      — outside market hours, skip entire cycle

    Why exits_only instead of blocking everything:
    9:30-9:45 AM is chaotic for NEW entries (wide spreads, opening volatility).
    But if we already hold a losing/stale position, we want to exit ASAP — not wait
    15 extra minutes while the loss compounds. Trailing stops and scale-outs already
    run before this check; this opens the door for AI-decided sells too.

    Market hours (EDT = UTC-4 in summer):
    - 9:30 AM open  = 13:30 UTC
    - 9:45 AM       = 13:45 UTC  ← entries allowed from here
    - 4:00 PM close = 20:00 UTC
    """
    now_utc = datetime.now(timezone.utc)
    hour, minute = now_utc.hour, now_utc.minute
    minutes_since_midnight_utc = hour * 60 + minute

    market_open_utc  = 13 * 60 + 30   # 9:30 AM EST
    entries_start_utc = 13 * 60 + 45  # 9:45 AM EST — skip opening volatility for new entries
    market_close_utc = 20 * 60        # 4:00 PM EST

    if minutes_since_midnight_utc < market_open_utc:
        return "closed", "Pre-market — market not yet open"
    if minutes_since_midnight_utc >= market_close_utc:
        return "closed", "Market closed"
    if minutes_since_midnight_utc < entries_start_utc:
        return "exits_only", (
            f"Opening 15 min window — exits allowed, new entries blocked "
            f"({entries_start_utc - minutes_since_midnight_utc} min until entries open)"
        )

    # Power hour (3-4 PM EST = 19:00-20:00 UTC) — best liquidity for exits
    if minutes_since_midnight_utc >= 19 * 60:
        return "full", "Power hour — prime exit window"

    return "full", "Normal trading hours"
