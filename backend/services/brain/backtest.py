"""
Phase 5 — Backtesting Framework.

Walk-forward simulation of the full strategy stack against Alpaca historical data.
Uses the real brain modules (regime, rs_ranking, signals) on historical slices so
results reflect actual live behaviour, not a separate backtesting model.

Usage:
    from services.brain.backtest import run_backtest, print_report

    result = run_backtest(lookback_days=365)
    print_report(result)

    # Or run from CLI:
    # python -m services.brain.backtest

Thresholds (go-live gate):
    total_return_pct  > 0
    win_rate_pct      > 45
    avg_win_loss_ratio > 2.0
    sharpe_ratio      > 1.5
    max_drawdown_pct  < 15
    profit_factor     > 1.5
"""
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

# Minimum bars needed before we start simulating (for indicators that need history)
_WARMUP_BARS = 210          # 200-day MA needs 200 bars; add 10 buffer

# Trade management defaults
_STOP_LOSS_PCT   = 0.05     # 5% hard stop
_TAKE_PROFIT_PCT = 0.12     # 12% target
_MAX_HOLD_DAYS   = 10       # time stop for swing trades
_POSITION_SIZE_PCT = 0.05   # flat 5% of equity per trade (keeps sim simple)
_MAX_OPEN_POSITIONS = 5     # never hold more than 5 at once

# Go-live thresholds
THRESHOLDS = {
    "total_return_pct":   0.0,
    "win_rate_pct":      45.0,
    "avg_win_loss_ratio": 2.0,
    "sharpe_ratio":       1.5,
    "max_drawdown_pct":  15.0,   # must stay BELOW this
    "profit_factor":      1.5,
}


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class SimTrade:
    symbol: str
    entry_date: date
    entry_price: float
    exit_date: Optional[date] = None
    exit_price: Optional[float] = None
    shares: float = 0.0
    signal_type: str = ""
    regime_at_entry: str = ""
    exit_reason: str = ""          # "take_profit" | "stop_loss" | "time_stop" | "signal_exit" | "eod"
    pl_pct: float = 0.0
    pl_dollar: float = 0.0


@dataclass
class BacktestResult:
    start_date: date
    end_date: date
    universe_size: int
    total_trades: int
    winning_trades: int
    losing_trades: int

    # Core metrics
    total_return_pct: float
    win_rate_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    avg_win_loss_ratio: float
    sharpe_ratio: float
    max_drawdown_pct: float
    profit_factor: float

    # Equity curve
    equity_curve: list[tuple[date, float]] = field(default_factory=list)
    trades: list[SimTrade] = field(default_factory=list)

    # Gate check
    passes_go_live: bool = False
    failures: list[str] = field(default_factory=list)


# ── Historical data fetch ─────────────────────────────────────────────────────

def _fetch_historical_bars(symbols: list[str], lookback_days: int) -> dict[str, list]:
    """
    Fetch daily bars from Alpaca for all symbols.
    Returns {symbol: [Bar, ...]} sorted ascending by date.
    """
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from config import settings

    client = StockHistoricalDataClient(
        settings.alpaca_api_key,
        settings.alpaca_secret_key,
    )

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days + _WARMUP_BARS + 30)  # extra buffer for weekends/holidays

    logger.info("backtest: fetching %d days of bars for %d symbols …", lookback_days, len(symbols))

    try:
        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            feed="iex",
            start=start,
            end=end,
        )
        bars = client.get_stock_bars(req)
        raw = bars.data if hasattr(bars, "data") else dict(bars)
        return {sym: sorted(raw.get(sym, []), key=lambda b: b.timestamp) for sym in symbols}
    except Exception as e:
        logger.error("backtest: bar fetch failed — %s", e)
        return {}


def _fetch_spy_bars(lookback_days: int) -> list:
    """Fetch SPY bars for regime detection."""
    result = _fetch_historical_bars(["SPY"], lookback_days + _WARMUP_BARS + 30)
    return result.get("SPY", [])


# ── Snapshot builder (historical slice) ──────────────────────────────────────

def _build_snapshot(bars_by_symbol: dict[str, list], bar_index: int) -> dict:
    """
    Build a universe_snapshot dict for a single historical day, using all bars
    up to and including bar_index for each symbol.
    Matches the structure that signals.py / regime.py expect.
    """
    snapshot = {}
    for symbol, bars in bars_by_symbol.items():
        if bar_index >= len(bars):
            continue
        history = bars[: bar_index + 1]
        closing_prices = [float(b.close) for b in history]
        volumes = [int(b.volume) for b in history]
        current_price = closing_prices[-1] if closing_prices else None

        avg_vol = sum(volumes[-20:]) / len(volumes[-20:]) if len(volumes) >= 20 else (sum(volumes) / len(volumes) if volumes else 1)
        rel_vol = round(volumes[-1] / avg_vol, 2) if avg_vol > 0 else 1.0

        snapshot[symbol] = {
            "current_price": current_price,
            "closing_prices": closing_prices,
            "high_prices": [float(b.high) for b in history],
            "low_prices": [float(b.low) for b in history],
            "volume": volumes[-1] if volumes else 0,
            "avg_volume": int(avg_vol),
            "relative_volume": rel_vol,
        }
    return snapshot


# ── Strategy evaluation (one day) ────────────────────────────────────────────

def _run_strategy_day(snapshot: dict, spy_prices: list[float], vix_stub: float = 18.0):
    """
    Run regime + rs_ranking + signals for a single historical day.
    Returns (regime_result, rs_map, scored_candidates).
    vix_stub: fixed VIX since we don't backtest VIX (use 18 = "normal").
    """
    from services.brain.regime import detect_regime
    from services.brain.rs_ranking import rank_universe
    from services.brain.signals import score_universe

    try:
        regime_result = detect_regime(spy_prices, vix_stub, snapshot)
    except Exception as e:
        logger.debug("backtest regime error: %s", e)
        return None, {}, []

    try:
        rs_list = rank_universe(snapshot, spy_prices)
        rs_map = {r.symbol: r for r in rs_list}
    except Exception as e:
        logger.debug("backtest rs_ranking error: %s", e)
        rs_map = {}

    try:
        candidates = score_universe(snapshot, regime_result, rs_map, {}, [], top_n=5, min_score=50)
    except Exception as e:
        logger.debug("backtest signals error: %s", e)
        candidates = []

    return regime_result, rs_map, candidates


# ── Trade simulation ──────────────────────────────────────────────────────────

def _try_open(
    candidate,
    regime_result,
    open_positions: dict[str, SimTrade],
    equity: float,
    today: date,
) -> Optional[SimTrade]:
    """Open a new simulated trade if conditions are met."""
    symbol = candidate.symbol
    if symbol in open_positions:
        return None
    if len(open_positions) >= _MAX_OPEN_POSITIONS:
        return None
    if candidate.suggested_action not in ("buy",):
        return None  # backtest only longs for simplicity

    price = candidate.price
    if not price or price <= 0:
        return None

    dollar_amount = equity * _POSITION_SIZE_PCT
    shares = dollar_amount / price

    return SimTrade(
        symbol=symbol,
        entry_date=today,
        entry_price=price,
        shares=shares,
        signal_type=candidate.signal_type,
        regime_at_entry=regime_result.regime if regime_result else "unknown",
    )


def _check_exit(trade: SimTrade, current_price: float, today: date) -> Optional[str]:
    """Return exit reason string if position should be closed, else None."""
    if current_price <= 0:
        return None

    hold_days = (today - trade.entry_date).days
    pct_change = (current_price - trade.entry_price) / trade.entry_price

    if pct_change <= -_STOP_LOSS_PCT:
        return "stop_loss"
    if pct_change >= _TAKE_PROFIT_PCT:
        return "take_profit"
    if hold_days >= _MAX_HOLD_DAYS:
        return "time_stop"
    return None


def _close_trade(trade: SimTrade, exit_price: float, exit_date: date, reason: str) -> SimTrade:
    trade.exit_date = exit_date
    trade.exit_price = exit_price
    trade.exit_reason = reason
    trade.pl_pct = round((exit_price - trade.entry_price) / trade.entry_price * 100, 4)
    trade.pl_dollar = round((exit_price - trade.entry_price) * trade.shares, 4)
    return trade


# ── Metrics calculation ───────────────────────────────────────────────────────

def _compute_metrics(
    closed_trades: list[SimTrade],
    equity_curve: list[tuple[date, float]],
    start_equity: float,
) -> dict:
    if not closed_trades:
        return {k: 0.0 for k in THRESHOLDS}

    wins = [t for t in closed_trades if t.pl_pct > 0]
    losses = [t for t in closed_trades if t.pl_pct <= 0]

    win_rate = len(wins) / len(closed_trades) * 100
    avg_win = sum(t.pl_pct for t in wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(t.pl_pct for t in losses) / len(losses)) if losses else 0.0
    avg_win_loss = round(avg_win / avg_loss, 4) if avg_loss > 0 else float("inf")

    gross_profit = sum(t.pl_dollar for t in wins)
    gross_loss = abs(sum(t.pl_dollar for t in losses))
    profit_factor = round(gross_profit / gross_loss, 4) if gross_loss > 0 else float("inf")

    final_equity = equity_curve[-1][1] if equity_curve else start_equity
    total_return = round((final_equity / start_equity - 1) * 100, 4)

    # Sharpe: annualised, using daily equity returns
    if len(equity_curve) >= 2:
        daily_returns = [
            (equity_curve[i][1] - equity_curve[i - 1][1]) / equity_curve[i - 1][1]
            for i in range(1, len(equity_curve))
        ]
        mean_ret = sum(daily_returns) / len(daily_returns)
        variance = sum((r - mean_ret) ** 2 for r in daily_returns) / len(daily_returns)
        std_ret = math.sqrt(variance) if variance > 0 else 0.0
        sharpe = round((mean_ret / std_ret) * math.sqrt(252), 4) if std_ret > 0 else 0.0
    else:
        sharpe = 0.0

    # Max drawdown: peak-to-trough on equity curve
    peak = start_equity
    max_dd = 0.0
    for _, eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd
    max_dd = round(max_dd, 4)

    return {
        "total_return_pct":    total_return,
        "win_rate_pct":        round(win_rate, 2),
        "avg_win_pct":         round(avg_win, 4),
        "avg_loss_pct":        round(avg_loss, 4),
        "avg_win_loss_ratio":  avg_win_loss,
        "sharpe_ratio":        sharpe,
        "max_drawdown_pct":    max_dd,
        "profit_factor":       profit_factor,
    }


def _check_thresholds(metrics: dict) -> tuple[bool, list[str]]:
    failures = []
    for key, threshold in THRESHOLDS.items():
        value = metrics.get(key, 0.0)
        if key == "max_drawdown_pct":
            if value >= threshold:
                failures.append(f"{key}={value:.2f}% (must be < {threshold}%)")
        else:
            if value < threshold:
                failures.append(f"{key}={value:.2f} (must be >= {threshold})")
    return len(failures) == 0, failures


# ── Main entry point ──────────────────────────────────────────────────────────

def run_backtest(
    lookback_days: int = 365,
    start_equity: float = 100_000.0,
    universe: Optional[list[str]] = None,
) -> BacktestResult:
    """
    Run the full walk-forward backtest.

    Args:
        lookback_days:  How many calendar days of history to test (default 1 year).
        start_equity:   Starting paper equity (default $100k).
        universe:       List of symbols to test. Defaults to a representative universe.

    Returns:
        BacktestResult with all metrics and the closed trade list.
    """
    if universe is None:
        universe = _default_universe()

    # Fetch data
    spy_bars = _fetch_spy_bars(lookback_days)
    all_bars = _fetch_historical_bars(universe, lookback_days)

    if not spy_bars or not all_bars:
        logger.error("backtest: no data returned — aborting")
        return _empty_result(lookback_days)

    # Align all symbols to SPY bar dates for a clean daily loop
    spy_dates = [b.timestamp.date() if hasattr(b.timestamp, "date") else b.timestamp for b in spy_bars]

    # Only simulate bars within the requested lookback (skip warmup)
    sim_start_idx = max(_WARMUP_BARS, len(spy_bars) - lookback_days)
    sim_bars = spy_bars[sim_start_idx:]
    sim_dates = spy_dates[sim_start_idx:]

    if not sim_dates:
        logger.error("backtest: not enough bars after warmup")
        return _empty_result(lookback_days)

    logger.info("backtest: simulating %d trading days (%s → %s)", len(sim_dates), sim_dates[0], sim_dates[-1])

    equity = start_equity
    equity_curve: list[tuple[date, float]] = []
    open_positions: dict[str, SimTrade] = {}
    closed_trades: list[SimTrade] = []

    for sim_i, today in enumerate(sim_dates):
        # Absolute index in full bar arrays
        abs_idx = sim_start_idx + sim_i

        # Build snapshot for today
        spy_prices = [float(b.close) for b in spy_bars[: abs_idx + 1]]
        snapshot = _build_snapshot(all_bars, abs_idx)

        if not snapshot:
            equity_curve.append((today, equity))
            continue

        # Update open positions: check exits
        to_close: list[str] = []
        for sym, trade in open_positions.items():
            sym_data = snapshot.get(sym)
            if not sym_data:
                continue
            current_price = sym_data.get("current_price") or 0.0
            reason = _check_exit(trade, current_price, today)
            if reason:
                _close_trade(trade, current_price, today, reason)
                equity += trade.pl_dollar
                closed_trades.append(trade)
                to_close.append(sym)
                logger.debug("backtest: closed %s %s @ $%.2f (%.2f%%)", sym, reason, current_price, trade.pl_pct)

        for sym in to_close:
            del open_positions[sym]

        # Run strategy to find new candidates
        regime_result, _, candidates = _run_strategy_day(snapshot, spy_prices)

        # Try to open new positions
        for candidate in candidates:
            trade = _try_open(candidate, regime_result, open_positions, equity, today)
            if trade:
                open_positions[trade.symbol] = trade
                logger.debug("backtest: opened %s %s @ $%.2f", trade.symbol, trade.signal_type, trade.entry_price)

        equity_curve.append((today, round(equity, 2)))

    # Close any still-open positions at last price
    for sym, trade in open_positions.items():
        sym_data = snapshot.get(sym, {})
        last_price = sym_data.get("current_price") or trade.entry_price
        _close_trade(trade, last_price, sim_dates[-1], "eod")
        equity += trade.pl_dollar
        closed_trades.append(trade)

    # Calculate metrics
    metrics = _compute_metrics(closed_trades, equity_curve, start_equity)
    passes, failures = _check_thresholds(metrics)

    wins = [t for t in closed_trades if t.pl_pct > 0]
    losses = [t for t in closed_trades if t.pl_pct <= 0]

    result = BacktestResult(
        start_date=sim_dates[0],
        end_date=sim_dates[-1],
        universe_size=len(universe),
        total_trades=len(closed_trades),
        winning_trades=len(wins),
        losing_trades=len(losses),
        total_return_pct=metrics["total_return_pct"],
        win_rate_pct=metrics["win_rate_pct"],
        avg_win_pct=metrics["avg_win_pct"],
        avg_loss_pct=metrics["avg_loss_pct"],
        avg_win_loss_ratio=metrics["avg_win_loss_ratio"],
        sharpe_ratio=metrics["sharpe_ratio"],
        max_drawdown_pct=metrics["max_drawdown_pct"],
        profit_factor=metrics["profit_factor"],
        equity_curve=equity_curve,
        trades=closed_trades,
        passes_go_live=passes,
        failures=failures,
    )

    _log_summary(result)
    return result


# ── Report printer ────────────────────────────────────────────────────────────

def print_report(result: BacktestResult) -> None:
    """Print a human-readable backtest report to stdout."""
    gate = "✅ PASS" if result.passes_go_live else "❌ FAIL"
    print(f"\n{'='*60}")
    print(f"  KOVA BACKTEST REPORT  —  Go-Live Gate: {gate}")
    print(f"{'='*60}")
    print(f"  Period:        {result.start_date} → {result.end_date}")
    print(f"  Universe:      {result.universe_size} symbols")
    print(f"  Total trades:  {result.total_trades}  ({result.winning_trades}W / {result.losing_trades}L)")
    print()

    rows = [
        ("Total return",        f"{result.total_return_pct:+.2f}%",   THRESHOLDS["total_return_pct"],  ">",  "%"),
        ("Win rate",            f"{result.win_rate_pct:.1f}%",         THRESHOLDS["win_rate_pct"],      ">",  "%"),
        ("Avg win / avg loss",  f"{result.avg_win_loss_ratio:.2f}x",   THRESHOLDS["avg_win_loss_ratio"],">",  "x"),
        ("Sharpe ratio",        f"{result.sharpe_ratio:.2f}",          THRESHOLDS["sharpe_ratio"],      ">",  ""),
        ("Max drawdown",        f"{result.max_drawdown_pct:.2f}%",     THRESHOLDS["max_drawdown_pct"],  "<",  "%"),
        ("Profit factor",       f"{result.profit_factor:.2f}",         THRESHOLDS["profit_factor"],     ">",  ""),
    ]

    for label, value, threshold, direction, unit in rows:
        status = "✅" if result.passes_go_live or label not in [f[0] for f in result.failures] else "❌"
        # Re-check per-metric
        numeric_val = float(value.replace("%", "").replace("x", "").replace("+", ""))
        if direction == ">":
            ok = numeric_val >= threshold
        else:
            ok = numeric_val < threshold
        icon = "✅" if ok else "❌"
        print(f"  {icon}  {label:<22} {value:>10}   (threshold: {direction}{threshold}{unit})")

    if result.failures:
        print(f"\n  Failures:")
        for f in result.failures:
            print(f"    • {f}")

    print(f"\n  Avg win:  +{result.avg_win_pct:.2f}%   Avg loss: -{result.avg_loss_pct:.2f}%")
    print(f"{'='*60}\n")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _log_summary(result: BacktestResult) -> None:
    gate = "PASS" if result.passes_go_live else "FAIL"
    logger.info(
        "backtest [%s] %d trades | return=%.1f%% win=%.0f%% sharpe=%.2f drawdown=%.1f%% pf=%.2f",
        gate,
        result.total_trades,
        result.total_return_pct,
        result.win_rate_pct,
        result.sharpe_ratio,
        result.max_drawdown_pct,
        result.profit_factor,
    )
    if result.failures:
        logger.warning("backtest: go-live gate FAILED — %s", "; ".join(result.failures))


def _empty_result(lookback_days: int) -> BacktestResult:
    today = date.today()
    return BacktestResult(
        start_date=today - timedelta(days=lookback_days),
        end_date=today,
        universe_size=0,
        total_trades=0,
        winning_trades=0,
        losing_trades=0,
        total_return_pct=0.0,
        win_rate_pct=0.0,
        avg_win_pct=0.0,
        avg_loss_pct=0.0,
        avg_win_loss_ratio=0.0,
        sharpe_ratio=0.0,
        max_drawdown_pct=0.0,
        profit_factor=0.0,
        passes_go_live=False,
        failures=["No data returned from Alpaca"],
    )


def _default_universe() -> list[str]:
    """Representative universe of liquid large-caps + sector ETFs for backtesting."""
    return [
        # Large-cap tech
        "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA", "AVGO",
        # Financials / industrials
        "JPM", "GS", "BAC", "CAT", "DE",
        # Healthcare / consumer
        "UNH", "LLY", "JNJ", "COST", "HD",
        # Sector ETFs
        "XLK", "XLF", "XLE", "XLV", "XLY", "XLI",
        # Benchmarks / leveraged
        "SPY", "QQQ", "IWM", "TQQQ", "SOXL",
    ]


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    days = int(sys.argv[1]) if len(sys.argv) > 1 else 365
    print(f"Running backtest over {days} days …")
    result = run_backtest(lookback_days=days)
    print_report(result)

    sys.exit(0 if result.passes_go_live else 1)
