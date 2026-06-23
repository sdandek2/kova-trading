import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Ticker-aware MA20 thresholds ───────────────────────────────────────────
_LEVERAGED_ETFS = {
    "TQQQ", "SQQQ", "SOXL", "SOXS", "SPXL", "SPXS", "UPRO", "SPXU",
    "TECL", "TECS", "LABU", "LABD", "FNGU", "FNGS", "CURE", "DFEN",
    "TNA", "TZA", "UDOW", "SDOW", "URTY", "SRTY",
}

_BROAD_ETFS = {
    "SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "TLT", "HYG", "LQD", "IEF",
    "XLF", "XLK", "XLE", "XLV", "XLI", "XLY", "XLB", "XLP", "XLU", "XLRE",
    "SMH", "XBI", "XME", "GDX", "USO", "IAU", "IBIT", "GBTC", "VOO", "VTI",
    "AGG", "SOXX", "ARKK", "ARKW", "ARKG", "ARKF", "ARKE", "SCO", "OIH",
    "EWY", "FXI", "EEM", "EFA", "VWO", "UVXY", "SVXY", "SNDQ", "IUXX",
}

# ── Rejection cooldown ─────────────────────────────────────────────────────
# Prevents the same symbol from being fetched, confirmed, and rejected every
# cycle (e.g. QUCY rejected 4× per hour for the same MA20 extension reason).
# Thread-safe: the dict is shared across executor threads via the lock below.
_REJECTION_COOLDOWN: dict = {}
_REJECTION_COOLDOWN_LOCK = threading.Lock()
_REJECTION_COOLDOWN_MINUTES = 15  # aggressive: shorter cooldown so symbols re-enter sooner

# ── Stop-out cooldown ──────────────────────────────────────────────────────
# After a stop-loss exit, block re-entry for 2 hours so the bot doesn't
# immediately re-buy a falling knife. Stock may recover after 2h — unlike
# a full-day ban, this allows profitable afternoon re-entries.
_STOPOUT_COOLDOWN: dict = {}   # symbol → datetime of stop-out
_STOPOUT_COOLDOWN_LOCK = threading.Lock()
_STOPOUT_COOLDOWN_HOURS = 4.0


def record_stopout(symbol: str) -> None:
    """Call this when a position is exited via stop-loss."""
    with _STOPOUT_COOLDOWN_LOCK:
        _STOPOUT_COOLDOWN[symbol] = datetime.now(timezone.utc)
    logger.info(f"Stop-out recorded for {symbol} — re-entry blocked for {_STOPOUT_COOLDOWN_HOURS:.0f}h")


def is_in_stopout_cooldown(symbol: str) -> tuple[bool, str]:
    """Return (True, reason) if symbol was stopped out within the cooldown window."""
    with _STOPOUT_COOLDOWN_LOCK:
        last = _STOPOUT_COOLDOWN.get(symbol)
        if last is None:
            return False, ""
        hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        if hours < _STOPOUT_COOLDOWN_HOURS:
            remaining = _STOPOUT_COOLDOWN_HOURS - hours
            return True, (
                f"{symbol} was stopped out {hours*60:.0f} min ago — "
                f"re-entry blocked for {remaining*60:.0f} more min"
            )
        del _STOPOUT_COOLDOWN[symbol]
        return False, ""


def _is_in_rejection_cooldown(symbol: str) -> tuple[bool, str]:
    """Return (True, reason) if symbol was recently rejected."""
    with _REJECTION_COOLDOWN_LOCK:
        last = _REJECTION_COOLDOWN.get(symbol)
        if last is None:
            return False, ""
        mins = (datetime.now(timezone.utc) - last).total_seconds() / 60
        if mins < _REJECTION_COOLDOWN_MINUTES:
            return True, (
                f"{symbol} in rejection cooldown — rejected {mins:.0f} min ago, "
                f"retry in {_REJECTION_COOLDOWN_MINUTES - mins:.0f} min"
            )
        del _REJECTION_COOLDOWN[symbol]
        return False, ""


def _record_rejection(symbol: str) -> None:
    with _REJECTION_COOLDOWN_LOCK:
        _REJECTION_COOLDOWN[symbol] = datetime.now(timezone.utc)


def clear_rejection_cooldown(symbol: str) -> None:
    with _REJECTION_COOLDOWN_LOCK:
        _REJECTION_COOLDOWN.pop(symbol, None)


def get_cooldown_symbols() -> list[str]:
    """Return list of symbols currently in rejection cooldown — fed into Step 1 so Claude stops nominating them."""
    with _REJECTION_COOLDOWN_LOCK:
        now = datetime.now(timezone.utc)
        return [
            sym for sym, last in _REJECTION_COOLDOWN.items()
            if (now - last).total_seconds() / 60 < _REJECTION_COOLDOWN_MINUTES
        ]


def _ma20_extension_limit(symbol: str, price: float = 0.0) -> float:
    """
    Return the maximum allowed % extension above MA20 for this symbol.
    - Penny stocks (< $5): 40% — 10% on a $0.45 stock = 4.5 cents, meaningless.
    - Leveraged 3× ETFs: 18%
    - Broad / non-leveraged ETFs: 12%
    - Individual stocks: 10%
    """
    if 0 < price < 5.0:
        return 0.40
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
    relative_volume: float = 1.0,
    macd_histogram: float = None,
) -> tuple[bool, str]:
    """
    Strategy-aware entry confirmation.

    AGGRESSIVE mode (designed for 5-10 trades/day):
    - Only blocks on confirmed panic selling (>5% down + volume collapse)
    - RSI ceiling: 92 for leveraged ETFs (momentum instruments), 85 for all others
    - MA20 extension: ticker-aware (penny=40%, leveraged=18%, broad=12%, stocks=10%)
    - Thin portfolio (<3 positions) lowers bar further, capped at 1.5× normal limit
    - Volume threshold lowered to 0.4x (was 0.7x) — don't miss momentum on quiet days

    BALANCED / CONSERVATIVE mode:
    - Down >2% from yesterday → skip
    - RSI > 75 → skip
    - Price > MA20 * (limit * 0.5) → skip (half aggressive limit, but penny stocks keep full 40%)
    """
    # ── Rejection cooldown: skip immediately if recently rejected ──
    in_cooldown, cooldown_reason = _is_in_rejection_cooldown(symbol)
    if in_cooldown and action == "buy":
        return False, cooldown_reason

    # ── Stop-out cooldown: block re-entry for 2h after a loss exit ──
    if action == "buy":
        in_stopout, stopout_reason = is_in_stopout_cooldown(symbol)
        if in_stopout:
            return False, stopout_reason

    if not closing_prices or len(closing_prices) < 2:
        return True, "Insufficient data for confirmation, proceeding anyway"

    from services.indicators import compute_rsi, compute_moving_averages

    yesterday_close = closing_prices[-2]
    # Explicit None check: `or 50.0` would wrongly replace a real 0.0 RSI with 50.
    _raw_rsi = compute_rsi(closing_prices)
    rsi = _raw_rsi if _raw_rsi is not None else 50.0
    mas = compute_moving_averages(closing_prices)
    ma20 = mas.get("ma20")
    is_aggressive = strategy_key == "aggressive"
    needs_positions = positions_count < 3

    if action == "buy":
        if is_aggressive:
            if current_price < yesterday_close * 0.95 and not needs_positions:
                reason = (
                    f"{symbol} down >5% from yesterday (${yesterday_close:.2f}→${current_price:.2f}) "
                    f"— confirmed weakness, skipping"
                )
                _record_rejection(symbol)
                return False, reason

            # Leveraged ETFs are momentum instruments — high RSI IS the signal.
            # Only block at extreme levels. Regular stocks capped at 85.
            rsi_ceiling = 92 if symbol in _LEVERAGED_ETFS else 85
            if rsi > rsi_ceiling:
                reason = f"{symbol} RSI {rsi:.1f} — above {rsi_ceiling} ceiling, too extended even for aggressive"
                _record_rejection(symbol)
                return False, reason

            # Aggressive mode: skip MA20 extension check entirely.
            # In aggressive mode, momentum IS the strategy. RSI (capped at 85/92),
            # MACD, and volume are the real guards. MA20 extension is an
            # anti-momentum filter — it conflicts with the aggressive thesis.
            # Leveraged ETFs also exempt: their MA20 is distorted by 3x daily rebalancing.
            if ma20 and symbol not in _LEVERAGED_ETFS and not is_aggressive:
                ext_limit = _ma20_extension_limit(symbol, current_price)
                hard_cap  = ext_limit * 1.5
                pct_above = (current_price / ma20 - 1) * 100

                if needs_positions:
                    if current_price > ma20 * (1 + hard_cap):
                        reason = (
                            f"{symbol} is {pct_above:.0f}% above MA20 (${ma20:.2f}) — "
                            f"too extended even with thin portfolio (cap={hard_cap*100:.0f}%)"
                        )
                        _record_rejection(symbol)
                        return False, reason
                else:
                    if current_price > ma20 * (1 + ext_limit):
                        reason = (
                            f"{symbol} is {pct_above:.0f}%+ above MA20 (${ma20:.2f}) — "
                            f"parabolic, not a breakout (limit={ext_limit*100:.0f}%)"
                        )
                        _record_rejection(symbol)
                        return False, reason

            if relative_volume < 0.4 and not needs_positions:
                reason = (
                    f"{symbol} relative volume {relative_volume:.1f}x — too thin even for aggressive"
                )
                _record_rejection(symbol)
                return False, reason

            # Require positive MACD for leveraged ETFs — entering a 3x instrument with
            # negative momentum burns capital fast. For regular stocks, only block on
            # strongly negative MACD to avoid filtering mild consolidation dips.
            if macd_histogram is not None and not needs_positions:
                if symbol in _LEVERAGED_ETFS and macd_histogram <= 0:
                    reason = (
                        f"{symbol} MACD histogram {macd_histogram:.3f} — leveraged ETF requires positive MACD to enter"
                    )
                    _record_rejection(symbol)
                    return False, reason
                elif symbol not in _LEVERAGED_ETFS and macd_histogram < -0.25 and rsi >= 45:
                    reason = (
                        f"{symbol} MACD histogram {macd_histogram:.3f} — momentum strongly negative, waiting for recovery"
                    )
                    _record_rejection(symbol)
                    return False, reason

            return True, (
                f"{symbol} approved [AGGRESSIVE]: RSI={rsi:.1f}, "
                f"vs yesterday {((current_price/yesterday_close)-1)*100:+.1f}%"
            )

        else:
            if current_price < yesterday_close * 0.98:
                reason = (
                    f"{symbol} is down >2% from yesterday's close "
                    f"(${yesterday_close:.2f}→${current_price:.2f}) — waiting for stabilization"
                )
                _record_rejection(symbol)
                return False, reason
            if rsi > 75:
                reason = f"{symbol} RSI is {rsi:.1f} — overbought, waiting for pullback"
                _record_rejection(symbol)
                return False, reason
            if ma20:
                raw_limit = _ma20_extension_limit(symbol, current_price)
                # Bug fix: penny stocks keep full 40% limit even in conservative mode.
                # Halving 40% → 20% is still meaningless on a $0.45 stock.
                ext_limit = raw_limit if (0 < current_price < 5.0) else raw_limit * 0.5
                pct_above = (current_price / ma20 - 1) * 100
                if current_price > ma20 * (1 + ext_limit):
                    reason = (
                        f"{symbol} is {pct_above:.0f}%+ above MA20 (${ma20:.2f}) — avoid chasing"
                    )
                    _record_rejection(symbol)
                    return False, reason
            return True, f"{symbol} passes entry confirmation: RSI={rsi:.1f}, price vs MA20 ok"

    elif action == "sell":
        # No RSI floor on sells. An oversold RSI during a sell signal means the
        # stock is in a panic-selling cascade — exactly when you want to exit, not hold.
        # Blocking sells here traps capital in deteriorating positions.
        return True, f"{symbol} sell confirmed: RSI={rsi:.1f}"

    elif action == "short":
        # Short requires genuine overbought conditions — not just any RSI.
        # RSI 65 minimum for aggressive (stock must be overbought before we short).
        # RSI 45 was too low — shorting a stock already pulling back risks a bounce.
        rsi_floor = 65 if is_aggressive else 55
        if rsi < rsi_floor:
            return False, (
                f"{symbol} RSI {rsi:.1f} — below short floor ({rsi_floor}), not overbought enough to short"
            )
        if current_price < yesterday_close * 0.95:
            return False, f"{symbol} already down >5% today — late short entry, skipping"
        # MACD must be turning negative — don't short into still-positive momentum
        from services.indicators import compute_macd as _cm
        _macd_s = _cm(closing_prices)
        _hist_s = _macd_s.get("histogram", 0) or 0
        if _hist_s > 0.05:
            return False, (
                f"{symbol} MACD histogram {_hist_s:.3f} — momentum still positive, too early to short"
            )
        return True, f"{symbol} short confirmed: RSI={rsi:.1f}, MACD hist={_hist_s:.3f}"

    return True, "No confirmation needed for hold"


def get_scale_in_quantity(
    base_quantity: int,
    confidence: str,
    existing_position_qty: float = 0,
    max_total_qty: int = 0,
    strategy_key: str = "balanced",
) -> int:
    if existing_position_qty > 0:
        remaining = max(0, max_total_qty - int(existing_position_qty))
        return max(1, min(remaining, base_quantity))
    if strategy_key == "aggressive":
        return max(1, base_quantity)
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
    atr_stop_price: float = None,
) -> tuple[bool, float, str]:
    """
    Partial profit-taking and loss-cutting for LONG positions.

    Exit logic (RSI-aware):
    - RSI > 75 AND profit > 5%  → trim 33% (momentum tiring, overbought)
    - RSI > 80 AND profit > 8%  → trim 50% (significantly overbought with good gains)
    - Profit > 15%              → sell 50%, let rest ride with trailing stop
    - Trailing stop             → asset-aware (7% leveraged ETFs, 5% individual stocks)
    - Stop loss                 → down 6% → full exit
    """
    is_aggressive = strategy_key == "aggressive"

    # ── Asset-aware trailing stop ──────────────────────────────────────────────
    # Leveraged ETFs (SOXL, TQQQ etc.) need wider trail — they swing 6-9% intraday.
    # A 5% trail fires on normal noise for 3x instruments.
    _is_leveraged = symbol in _LEVERAGED_ETFS
    _effective_trail = 0.07 if _is_leveraged else trail_pct  # 7% lev ETFs, caller-set for others

    if (high_watermark is not None and current_price is not None
            and high_watermark > 0 and current_price > 0
            and position_unrealized_pl_percent > 2.0):
        drop_from_peak = (high_watermark - current_price) / high_watermark
        if drop_from_peak >= _effective_trail:
            return True, 1.0, (
                f"{symbol} trailing stop hit: dropped {drop_from_peak*100:.1f}% from peak "
                f"${high_watermark:.2f} → ${current_price:.2f} "
                f"(trail={_effective_trail*100:.0f}%, still up {position_unrealized_pl_percent:.1f}% from entry) — locking in gains"
            )

    # ── RSI-based exits: recognize when upward momentum is exhausting ──────────
    # Aggressive mode: no RSI trim before +15% — let winners run
    if rsi is not None and not (is_aggressive and position_unrealized_pl_percent < 15.0):
        if rsi > 80 and position_unrealized_pl_percent >= 8.0:
            return True, 0.50, (
                f"{symbol} RSI {rsi:.1f} — significantly overbought with {position_unrealized_pl_percent:.1f}% gain — trimming 50%"
            )
        elif rsi > 75 and position_unrealized_pl_percent >= 5.0:
            return True, 0.33, (
                f"{symbol} RSI {rsi:.1f} — overbought with {position_unrealized_pl_percent:.1f}% gain — trimming 33%"
            )

    # ── Profit target ──────────────────────────────────────────────────────────
    # Aggressive: first trim at +20% (not +15%) — let momentum run further
    _profit_trigger = 20.0 if is_aggressive else 15.0
    if position_unrealized_pl_percent >= _profit_trigger:
        return True, 0.50, (
            f"{symbol} up {position_unrealized_pl_percent:.1f}% — taking 50% profits, letting 50% ride with trailing stop"
        )

    # ── Stop loss ──────────────────────────────────────────────────────────────
    # ATR stop: entry_price - (1.5× ATR for stocks, 2× for leveraged ETFs).
    # Gives each stock room proportional to its own volatility — a 4% ATR stock
    # won't be shaken out by normal noise the way a flat 3% stop would.
    # Falls back to flat % stop when ATR data is unavailable.
    if atr_stop_price is not None and current_price is not None and current_price > 0:
        if current_price <= atr_stop_price:
            return True, 1.0, (
                f"{symbol} hit ATR stop at ${atr_stop_price:.2f} (current ${current_price:.2f}) — cutting losses"
                + (", redeploying cash" if is_aggressive else "")
            )
    else:
        _stop_threshold = -5.0 if _is_leveraged else -3.0
        if position_unrealized_pl_percent <= _stop_threshold:
            return True, 1.0, (
                f"{symbol} down {position_unrealized_pl_percent:.1f}% — cutting losses"
                + (", redeploying cash" if is_aggressive else "")
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
    atr_stop_price: float = None,
) -> tuple[bool, float, str]:
    """
    Partial profit-taking and loss-cutting for SHORT positions.

    Exit logic (RSI-aware):
    - RSI < 30               → cover 100% regardless of profit (deeply oversold, reversal imminent)
    - RSI < 40 AND profit > 3% → cover 50% (momentum exhausting, take half off)
    - Profit > 10%           → cover 50%, let rest ride with trailing stop
    - Trailing stop          → asset-aware (8% leveraged ETFs, 5% individual stocks)
    - Stop loss              → price rises 3.5% from entry → full cover
    """
    # ── Asset-aware trailing stop ──────────────────────────────────────────────
    # Leveraged ETFs shorted need wider trail — SOXL can spike 6-7% on a single
    # news event even while trending down. 5% trail fires on normal intraday noise.
    _is_leveraged = symbol in _LEVERAGED_ETFS
    _effective_trail = 0.08 if _is_leveraged else trail_pct  # 8% lev ETFs, 5% individual stocks

    if (low_watermark is not None and current_price is not None
            and low_watermark > 0 and current_price > 0
            and position_unrealized_pl_percent > 2.0):
        rise_from_low = (current_price - low_watermark) / low_watermark
        if rise_from_low >= _effective_trail:
            return True, 1.0, (
                f"{symbol} SHORT trailing stop: price rose {rise_from_low*100:.1f}% from low "
                f"${low_watermark:.2f} → ${current_price:.2f} "
                f"(trail={_effective_trail*100:.0f}%, still up {position_unrealized_pl_percent:.1f}% from entry) — locking in short gains"
            )

    # ── RSI-based exits: recognize when downward momentum is exhausting ────────
    if rsi is not None:
        if rsi < 30:
            return True, 1.0, (
                f"{symbol} RSI {rsi:.1f} — deeply oversold, reversal imminent — covering 100%"
            )
        elif rsi < 40 and position_unrealized_pl_percent >= 3.0:
            return True, 0.50, (
                f"{symbol} RSI {rsi:.1f} — momentum exhausting with {position_unrealized_pl_percent:.1f}% gain — covering 50%"
            )

    # ── Profit target: staggered covers to let downtrends run ─────────────────
    if position_unrealized_pl_percent >= 20.0:
        return True, 0.25, (
            f"{symbol} SHORT up {position_unrealized_pl_percent:.1f}% — covering 25% (2nd trim), letting 50% ride"
        )
    if position_unrealized_pl_percent >= 10.0:
        return True, 0.25, (
            f"{symbol} SHORT up {position_unrealized_pl_percent:.1f}% — covering 25% (1st trim), letting 75% ride"
        )

    # ── Stop loss: price rising against short ─────────────────────────────────
    # ATR stop for shorts: entry_price + (2× ATR). Tighter than longs (2× vs 1.5×)
    # because short squeezes are fast and violent — need to cut early.
    if atr_stop_price is not None and current_price is not None and current_price > 0:
        if current_price >= atr_stop_price:
            return True, 1.0, (
                f"{symbol} SHORT hit ATR stop at ${atr_stop_price:.2f} (current ${current_price:.2f}) — covering"
            )
    elif position_unrealized_pl_percent <= -3.5:
        return True, 1.0, (
            f"{symbol} SHORT down {abs(position_unrealized_pl_percent):.1f}% (price rose against short) — covering to stop loss"
        )

    return False, 0.0, ""


def is_good_trading_window() -> tuple[str, str]:
    """
    Time-of-day filter using proper ET timezone (handles EDT/EST DST automatically).
    Returns (mode, reason) where mode is:
      "full"        — normal entries + exits
      "exits_only"  — only exits/stop-losses, no new entries (9:30–9:35 opening window)
      "premarket"   — extended hours, news-triggered limit orders only (8:30–9:30)
      "afterhours"  — extended hours, earnings plays only (4:00–6:00 PM)
      "closed"      — no trading at all
    """
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        from datetime import timedelta
        now_et = datetime.now(timezone.utc) - timedelta(hours=4)

    total_minutes = now_et.hour * 60 + now_et.minute

    premarket_start  = 8 * 60 + 30   # 8:30 AM ET
    market_open_et   = 9 * 60 + 30   # 9:30 AM ET
    entries_start_et = 9 * 60 + 35   # 9:35 AM ET  (was 9:45 — skip only worst 5 min)
    market_close_et  = 16 * 60        # 4:00 PM ET
    afterhours_end   = 18 * 60        # 6:00 PM ET

    if total_minutes < premarket_start:
        return "closed", "Outside all trading windows"
    if total_minutes < market_open_et:
        return "premarket", f"Pre-market (8:30–9:30 ET) — news-triggered limit orders only"
    if total_minutes < entries_start_et:
        return "exits_only", (
            f"Opening window — exits allowed, new entries blocked "
            f"({entries_start_et - total_minutes} min until entries open)"
        )
    if total_minutes >= afterhours_end:
        return "closed", "Outside all trading windows"
    if total_minutes >= market_close_et:
        return "afterhours", "After-hours (4:00–6:00 PM ET) — earnings plays only"
    if total_minutes >= 15 * 60:
        return "full", "Power hour — prime exit window"

    return "full", "Normal trading hours"
