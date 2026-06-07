"""
Phase 2 Backtest — 2020-2024 walk-forward using yfinance.

Zero AI calls — runs the full signal pipeline (regime + RS + signals) but skips
the Claude approve/reject step. Every candidate above min_score is treated as approved.

Usage (from backend/ directory):
    pip install yfinance
    python backtest_phase2.py

Output:
    Year-by-year table + ablation test table (each exit rule toggled off).
"""
import math
import sys
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

warnings.filterwarnings("ignore")

# ── Universe ──────────────────────────────────────────────────────────────────

UNIVERSE = [
    # Large-cap tech
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA", "AVGO",
    # AI stocks
    "PLTR",
    # Chips / Memory — includes stress tests (INTC collapse, SMCI boom+bust)
    "AMD", "MU", "TSM", "ARM", "QCOM", "INTC", "WDC", "SMCI",
    # Financials / industrials
    "JPM", "GS", "BAC", "CAT", "DE",
    # Healthcare / consumer
    "UNH", "LLY", "JNJ", "COST", "HD",
    # Sector ETFs
    "XLK", "XLF", "XLE", "XLV", "XLY", "XLI",
    # Benchmarks + leveraged
    "SPY", "QQQ", "IWM", "TQQQ", "SOXL",
]

# ── Exit rule flags (all True = full strategy) ────────────────────────────────

@dataclass
class ExitRules:
    use_time_stop: bool = True        # stale exit: close after MAX_HOLD_DAYS
    use_macd_decay: bool = True       # close if MACD histogram turns negative post-entry
    use_scale_out: bool = True        # partial exit at SCALE_OUT_PCT profit
    use_cooldown: bool = True         # skip symbol for COOLDOWN_DAYS after a stop-out
    use_circuit_breaker: bool = True  # halt new entries if daily drawdown > CB_LIMIT
    # negative test flags
    random_entries: bool = False      # ignore signals, enter randomly
    long_only: bool = False           # skip all short candidates
    no_regime_filter: bool = False    # ignore regime, take all signals regardless

FULL_RULES = ExitRules()

ABLATION_CONFIGS = [
    # ── Exit rule ablations ───────────────────────────────────────────────────
    ("Full strategy (baseline)",          ExitRules()),
    ("No stale exit (time stop off)",     ExitRules(use_time_stop=False)),
    ("No MACD decay exit",                ExitRules(use_macd_decay=False)),
    ("No scale-out",                      ExitRules(use_scale_out=False)),
    ("No cooldown after stop-out",        ExitRules(use_cooldown=False)),
    ("No circuit breaker",                ExitRules(use_circuit_breaker=False)),
    # ── Negative / baseline tests ─────────────────────────────────────────────
    ("NEGATIVE: random entries",          ExitRules(random_entries=True)),
    ("NEGATIVE: long-only (no shorts)",   ExitRules(long_only=True)),
    ("NEGATIVE: no regime filter",        ExitRules(no_regime_filter=True)),
]

# ── Trade parameters ──────────────────────────────────────────────────────────

STOP_LOSS_PCT    = 0.05
TAKE_PROFIT_PCT  = 0.12
SCALE_OUT_PCT    = 0.07   # partial exit: sell half at 7%
MAX_HOLD_DAYS    = 10
POSITION_SIZE_PCT = 0.05
MAX_OPEN         = 5
COOLDOWN_DAYS    = 3
CB_DAILY_LIMIT   = 0.03   # 3% daily equity drop triggers circuit breaker
WARMUP_BARS      = 210
MIN_SCORE        = 50

# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class SimTrade:
    symbol: str
    entry_date: date
    entry_price: float
    shares: float
    signal_type: str
    regime_at_entry: str
    direction: str = "long"        # "long" | "short"
    scaled_out: bool = False       # True after partial exit taken
    exit_date: Optional[date] = None
    exit_price: Optional[float] = None
    exit_reason: str = ""
    pl_pct: float = 0.0
    pl_dollar: float = 0.0


@dataclass
class YearResult:
    year: int
    trades: int
    win_pct: float
    avg_win: float
    avg_loss: float
    sharpe: float
    max_dd: float
    total_return: float


# ── yfinance data fetch ───────────────────────────────────────────────────────

def fetch_data(symbols: list[str], start: str, end: str) -> dict[str, dict]:
    """Returns {symbol: {dates, opens, highs, lows, closes, volumes}}."""
    try:
        import yfinance as yf
    except ImportError:
        print("ERROR: yfinance not installed. Run: pip install yfinance")
        sys.exit(1)

    import pandas as pd
    import time

    result = {}
    batch_size = 5  # small batches to avoid rate limiting

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i: i + batch_size]
        print(f"  Fetching {batch} …", end="", flush=True)

        raw = None
        for attempt in range(4):
            if attempt > 0:
                wait = 20 * attempt
                print(f" (rate limited, waiting {wait}s) …", end="", flush=True)
                time.sleep(wait)
            try:
                raw = yf.download(batch, start=start, end=end, auto_adjust=True, progress=False)
                if raw is not None and len(raw) > 0:
                    break
            except Exception:
                pass

        if raw is None or len(raw) == 0:
            print(" FAILED (skipping)")
            continue

        print(" done")

        for sym in batch:
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    close_field = "Adj Close" if "Adj Close" in raw.columns.get_level_values(0) else "Close"
                    closes  = raw[close_field][sym].dropna()
                    opens   = raw["Open"][sym].reindex(closes.index).fillna(closes)
                    highs   = raw["High"][sym].reindex(closes.index).fillna(closes)
                    lows    = raw["Low"][sym].reindex(closes.index).fillna(closes)
                    volumes = raw["Volume"][sym].reindex(closes.index).fillna(0)
                else:
                    close_field = "Adj Close" if "Adj Close" in raw.columns else "Close"
                    closes  = raw[close_field].dropna()
                    opens   = raw["Open"].reindex(closes.index).fillna(closes)
                    highs   = raw["High"].reindex(closes.index).fillna(closes)
                    lows    = raw["Low"].reindex(closes.index).fillna(closes)
                    volumes = raw["Volume"].reindex(closes.index).fillna(0)

                if closes.empty:
                    continue

                result[sym] = {
                    "dates":   [d.date() for d in closes.index],
                    "opens":   opens.tolist(),
                    "highs":   highs.tolist(),
                    "lows":    lows.tolist(),
                    "closes":  closes.tolist(),
                    "volumes": [int(v) for v in volumes.tolist()],
                }
            except Exception:
                pass

        time.sleep(3)  # small pause between batches

    return result


# ── Brain module imports ──────────────────────────────────────────────────────

def _import_brain():
    """Try to import brain modules. Returns (detect_regime, rank_universe, score_universe) or None."""
    try:
        from services.brain.regime import detect_regime
        from services.brain.rs_ranking import rank_universe
        from services.brain.signals import score_universe
        return detect_regime, rank_universe, score_universe
    except Exception as e:
        print(f"  WARNING: could not import brain modules ({e}) — using built-in simplified scoring")
        return None


# ── Simplified built-in signal scoring (fallback) ────────────────────────────

def _simple_macd(prices: list[float]) -> float:
    """EMA12 - EMA26 histogram value."""
    def ema(p, n):
        k = 2 / (n + 1)
        e = p[0]
        for x in p[1:]:
            e = x * k + e * (1 - k)
        return e
    if len(prices) < 26:
        return 0.0
    return ema(prices[-12:], 12) - ema(prices[-26:], 26)


def _simple_rsi(prices: list[float], period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d for d in deltas[-period:] if d > 0]
    losses = [-d for d in deltas[-period:] if d < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _simple_score(sym: str, closes: list[float], vols: list[float],
                  spy_closes: list[float], regime: str) -> Optional[dict]:
    """Simplified signal scoring when brain modules aren't available."""
    if len(closes) < 50 or closes[-1] <= 0:
        return None

    score = 0

    # MACD
    macd = _simple_macd(closes)
    if macd > 0:
        score += 10
    else:
        score -= 15

    # RSI
    rsi = _simple_rsi(closes)
    if rsi < 35:
        score += 15
    elif rsi > 75:
        score -= 10

    # Price vs MA20
    ma20 = sum(closes[-20:]) / 20
    if closes[-1] > ma20:
        score += 10
    else:
        score -= 5

    # Relative volume
    avg_vol = sum(vols[-20:]) / 20 if len(vols) >= 20 else (sum(vols) / len(vols) if vols else 1)
    rel_vol = vols[-1] / avg_vol if avg_vol > 0 else 1.0
    if rel_vol >= 2.0:
        score += 15
    elif rel_vol >= 1.5:
        score += 8
    elif rel_vol < 0.8:
        score -= 10

    # RS vs SPY (3-month)
    if len(closes) >= 63 and len(spy_closes) >= 63:
        sym_ret = closes[-1] / closes[-63] - 1
        spy_ret = spy_closes[-1] / spy_closes[-63] - 1
        if sym_ret > spy_ret + 0.05:
            score += 20
        elif sym_ret > spy_ret:
            score += 10
        else:
            score -= 10

    # Regime alignment — invert score for bear regime to find short candidates
    if regime == "bear":
        # Flip: strong stocks (high score) become short candidates
        short_score = -score + 30  # formerly high-scoring = weak relative = good short
        if short_score >= MIN_SCORE:
            return {
                "symbol": sym,
                "score": short_score,
                "signal_type": "short_candidate",
                "suggested_action": "short",
                "price": closes[-1],
                "macd_hist": macd,
                "rsi": rsi,
            }
        return None
    elif regime == "bull":
        score += 15

    if score < MIN_SCORE:
        return None

    return {
        "symbol": sym,
        "score": score,
        "signal_type": "momentum" if macd > 0 else "reversal",
        "suggested_action": "buy",
        "price": closes[-1],
        "macd_hist": macd,
        "rsi": rsi,
    }


# ── Snapshot builder ──────────────────────────────────────────────────────────

def _build_snapshot(all_data: dict[str, dict], idx: int) -> dict:
    snap = {}
    for sym, d in all_data.items():
        if idx >= len(d["closes"]):
            continue
        closes = d["closes"][: idx + 1]
        highs  = d["highs"][: idx + 1]
        lows   = d["lows"][: idx + 1]
        vols   = d["volumes"][: idx + 1]
        if not closes:
            continue
        avg_vol = sum(vols[-20:]) / min(20, len(vols))
        snap[sym] = {
            "current_price":  closes[-1],
            "closing_prices": closes,
            "high_prices":    highs,
            "low_prices":     lows,
            "volume":         vols[-1],
            "avg_volume":     int(avg_vol),
            "relative_volume": round(vols[-1] / avg_vol, 2) if avg_vol > 0 else 1.0,
        }
    return snap


# ── Simple regime detection (fallback) ───────────────────────────────────────

def _simple_regime(spy_closes: list[float], vix: float = 18.0) -> str:
    if len(spy_closes) < 50:
        return "chop"
    ma50 = sum(spy_closes[-50:]) / 50
    ma20 = sum(spy_closes[-20:]) / 20
    price = spy_closes[-1]
    if price > ma50 and ma20 > ma50:
        return "bull" if vix < 25 else "chop"
    elif price < ma50 and ma20 < ma50:
        return "bear"
    return "chop"


# ── Exit check ────────────────────────────────────────────────────────────────

def _check_exit(
    trade: SimTrade,
    current_price: float,
    today: date,
    current_macd: float,
    rules: ExitRules,
) -> Optional[str]:
    if current_price <= 0:
        return None

    hold = (today - trade.entry_date).days
    # For shorts, profit when price falls
    if trade.direction == "short":
        pct = (trade.entry_price - current_price) / trade.entry_price
    else:
        pct = (current_price - trade.entry_price) / trade.entry_price

    if pct <= -STOP_LOSS_PCT:
        return "stop_loss"

    if pct >= TAKE_PROFIT_PCT:
        return "take_profit"

    if rules.use_time_stop and hold >= MAX_HOLD_DAYS:
        return "time_stop"

    # MACD decay: for longs exit when MACD turns negative; for shorts exit when turns positive
    if rules.use_macd_decay and hold >= 2:
        if trade.direction == "long" and current_macd < 0:
            return "macd_decay"
        if trade.direction == "short" and current_macd > 0:
            return "macd_decay"

    return None


def _close(trade: SimTrade, price: float, today: date, reason: str) -> None:
    trade.exit_date = today
    trade.exit_price = price
    trade.exit_reason = reason
    if trade.direction == "short":
        trade.pl_pct = round((trade.entry_price - price) / trade.entry_price * 100, 4)
        trade.pl_dollar = round((trade.entry_price - price) * trade.shares, 4)
    else:
        trade.pl_pct = round((price - trade.entry_price) / trade.entry_price * 100, 4)
        trade.pl_dollar = round((price - trade.entry_price) * trade.shares, 4)


# ── Metrics ───────────────────────────────────────────────────────────────────

def _metrics(trades: list[SimTrade], equity_curve: list[float], start_eq: float) -> dict:
    if not trades:
        return dict(trades=0, win_pct=0, avg_win=0, avg_loss=0, sharpe=0, max_dd=0, total_return=0)

    wins   = [t for t in trades if t.pl_pct > 0]
    losses = [t for t in trades if t.pl_pct <= 0]
    win_pct   = len(wins) / len(trades) * 100
    avg_win   = sum(t.pl_pct for t in wins) / len(wins) if wins else 0
    avg_loss  = abs(sum(t.pl_pct for t in losses) / len(losses)) if losses else 0

    final = equity_curve[-1] if equity_curve else start_eq
    total_return = (final / start_eq - 1) * 100

    if len(equity_curve) >= 2:
        daily_rets = [(equity_curve[i] - equity_curve[i-1]) / equity_curve[i-1]
                      for i in range(1, len(equity_curve))]
        mu  = sum(daily_rets) / len(daily_rets)
        var = sum((r - mu) ** 2 for r in daily_rets) / len(daily_rets)
        std = math.sqrt(var) if var > 0 else 0
        sharpe = round((mu / std) * math.sqrt(252), 2) if std > 0 else 0
    else:
        sharpe = 0

    peak = start_eq
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    return dict(
        trades=len(trades),
        win_pct=round(win_pct, 1),
        avg_win=round(avg_win, 2),
        avg_loss=round(avg_loss, 2),
        sharpe=sharpe,
        max_dd=round(max_dd, 1),
        total_return=round(total_return, 1),
    )


# ── Core simulation for one year ──────────────────────────────────────────────

def run_year(year: int, all_data: dict[str, dict], brain, rules: ExitRules,
             start_equity: float = 100_000.0) -> dict:

    detect_regime_fn, rank_universe_fn, score_universe_fn = brain if brain else (None, None, None)

    spy_data = all_data.get("SPY", {})
    spy_dates = spy_data.get("dates", [])

    # Find date indices for the year
    year_indices = [i for i, d in enumerate(spy_dates) if d.year == year]
    if len(year_indices) < WARMUP_BARS:
        # Need warmup before year start — find first index of year in full array
        first_year_idx = year_indices[0] if year_indices else None
        if first_year_idx is None or first_year_idx < WARMUP_BARS:
            return dict(trades=0, win_pct=0, avg_win=0, avg_loss=0, sharpe=0, max_dd=0, total_return=0)

    sim_indices = year_indices
    equity = start_equity
    equity_curve: list[float] = []
    open_positions: dict[str, SimTrade] = {}
    closed_trades: list[SimTrade] = []
    cooldown_tracker: dict[str, date] = {}  # symbol → earliest re-entry date
    prev_day_equity = equity

    for abs_idx in sim_indices:
        today = spy_dates[abs_idx]
        spy_closes = spy_data["closes"][: abs_idx + 1]
        snapshot = _build_snapshot(all_data, abs_idx)
        if not snapshot:
            equity_curve.append(equity)
            continue

        # ── Regime ────────────────────────────────────────────────────────────
        vix_stub = 18.0
        if detect_regime_fn:
            try:
                regime_result = detect_regime_fn(spy_closes, vix_stub, snapshot)
                regime_str = regime_result.regime
            except Exception:
                regime_str = _simple_regime(spy_closes)
                regime_result = None
        else:
            regime_str = _simple_regime(spy_closes)
            regime_result = None

        # ── Circuit breaker ────────────────────────────────────────────────────
        daily_dd = (prev_day_equity - equity) / prev_day_equity if prev_day_equity > 0 else 0
        circuit_broken = rules.use_circuit_breaker and daily_dd >= CB_DAILY_LIMIT

        # ── Update open positions ──────────────────────────────────────────────
        to_close: list[str] = []
        for sym, trade in open_positions.items():
            sym_data = snapshot.get(sym)
            if not sym_data:
                continue
            price = sym_data["current_price"]
            sym_closes = sym_data["closing_prices"]
            macd = _simple_macd(sym_closes) if rules.use_macd_decay else 0.0

            # Scale-out: close half at SCALE_OUT_PCT profit if not already done
            if rules.use_scale_out and not trade.scaled_out:
                if trade.direction == "short":
                    pct = (trade.entry_price - price) / trade.entry_price
                else:
                    pct = (price - trade.entry_price) / trade.entry_price
                if pct >= SCALE_OUT_PCT:
                    half_shares = trade.shares / 2
                    if trade.direction == "short":
                        pl = (trade.entry_price - price) * half_shares
                    else:
                        pl = (price - trade.entry_price) * half_shares
                    equity += pl
                    trade.shares -= half_shares
                    trade.scaled_out = True

            reason = _check_exit(trade, price, today, macd, rules)
            if reason:
                _close(trade, price, today, reason)
                equity += trade.pl_dollar
                closed_trades.append(trade)
                to_close.append(sym)
                if reason == "stop_loss" and rules.use_cooldown:
                    from datetime import timedelta
                    cooldown_tracker[sym] = today + __import__("datetime").timedelta(days=COOLDOWN_DAYS)

        for sym in to_close:
            del open_positions[sym]

        # ── Signal scoring ─────────────────────────────────────────────────────
        if not circuit_broken:
            if rules.random_entries:
                # Negative test: pick up to MAX_OPEN random symbols, ignore all signals
                import random
                available = [s for s in snapshot if s not in open_positions and s != "SPY"]
                random.shuffle(available)
                candidates = [
                    {"symbol": s, "score": 50, "signal_type": "random",
                     "suggested_action": "buy", "price": snapshot[s]["current_price"],
                     "macd_hist": 0.0, "rsi": 50.0}
                    for s in available[:MAX_OPEN]
                ]
            else:
                # Use regime-aware scoring or no-regime variant
                effective_regime_str = regime_str if not rules.no_regime_filter else "chop"
                effective_regime_result = regime_result if not rules.no_regime_filter else None

                if score_universe_fn and effective_regime_result:
                    try:
                        rs_list = rank_universe_fn(snapshot, spy_closes)
                        rs_map  = {r.symbol: r for r in rs_list}
                        candidates = score_universe_fn(snapshot, effective_regime_result, rs_map, {}, [], top_n=5, min_score=MIN_SCORE)
                        candidates = [c for c in candidates if c.suggested_action in ("buy", "short")]
                    except Exception:
                        candidates = _simple_candidates(snapshot, spy_closes, effective_regime_str)
                else:
                    candidates = _simple_candidates(snapshot, spy_closes, effective_regime_str)

            # ── Open new positions ─────────────────────────────────────────────
            for c in candidates:
                sym = c["symbol"] if isinstance(c, dict) else c.symbol
                suggested = c.get("suggested_action", "buy") if isinstance(c, dict) else c.suggested_action
                if suggested not in ("buy", "short"):
                    continue
                if rules.long_only and suggested == "short":
                    continue
                if sym in open_positions or len(open_positions) >= MAX_OPEN:
                    continue
                if rules.use_cooldown and sym in cooldown_tracker and today < cooldown_tracker[sym]:
                    continue
                price = c["price"] if isinstance(c, dict) else c.price
                if not price or price <= 0:
                    continue
                dollars = equity * POSITION_SIZE_PCT
                shares  = dollars / price
                direction = "short" if suggested == "short" else "long"
                open_positions[sym] = SimTrade(
                    symbol=sym,
                    entry_date=today,
                    entry_price=price,
                    shares=shares,
                    signal_type=c["signal_type"] if isinstance(c, dict) else c.signal_type,
                    regime_at_entry=regime_str,
                    direction=direction,
                )

        prev_day_equity = equity
        equity_curve.append(round(equity, 2))

    # Force-close remaining at last price
    last_snap = _build_snapshot(all_data, sim_indices[-1]) if sim_indices else {}
    for sym, trade in open_positions.items():
        sym_data = last_snap.get(sym, {})
        price = sym_data.get("current_price") or trade.entry_price
        _close(trade, price, spy_dates[sim_indices[-1]], "eod")
        equity += trade.pl_dollar
        closed_trades.append(trade)

    return _metrics(closed_trades, equity_curve, start_equity)


def _simple_candidates(snapshot: dict, spy_closes: list[float], regime: str) -> list[dict]:
    """Simplified signal scoring for when brain modules aren't importable."""
    scored = []
    for sym, d in snapshot.items():
        if sym == "SPY":
            continue
        closes = d.get("closing_prices", [])
        vols   = d.get("volume", 0)
        vol_list = [vols] * len(closes)  # approximate
        result = _simple_score(sym, closes, vol_list, spy_closes, regime)
        if result:
            scored.append(result)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:5]


# ── Printer ───────────────────────────────────────────────────────────────────

def print_year_table(results: list[tuple[int, dict]]) -> None:
    print(f"\n{'='*80}")
    print("  KOVA BACKTEST — 2020-2026  (yfinance, no AI calls)")
    print(f"{'='*80}")
    print(f"  {'Year':<6} {'Trades':>7} {'Win%':>7} {'AvgWin':>8} {'AvgLoss':>9} {'Sharpe':>8} {'MaxDD':>8} {'Return':>8}")
    print(f"  {'-'*74}")
    for year, m in results:
        label = "← bear" if year == 2022 else ""
        print(f"  {year:<6} {m['trades']:>7} {m['win_pct']:>6.1f}%"
              f" {m['avg_win']:>+7.2f}%  {-m['avg_loss']:>+7.2f}%"
              f"  {m['sharpe']:>6.2f}  {m['max_dd']:>6.1f}%  {m['total_return']:>+6.1f}%  {label}")
    print(f"{'='*80}\n")


def print_ablation_table(results: list[tuple[str, dict]]) -> None:
    print(f"\n{'='*90}")
    print("  ABLATION + NEGATIVE TESTS — 2020-2026 combined")
    print(f"{'='*90}")
    print(f"  {'Config':<40} {'Trades':>7} {'Win%':>7} {'Sharpe':>8} {'MaxDD':>8} {'Return':>8}")
    print(f"  {'-'*86}")
    for i, (label, m) in enumerate(results):
        if i == 6:  # separator before negative tests
            print(f"  {'-'*86}")
        print(f"  {label:<40} {m['trades']:>7} {m['win_pct']:>6.1f}%"
              f"  {m['sharpe']:>6.2f}  {m['max_dd']:>6.1f}%  {m['total_return']:>+6.1f}%")
    print(f"{'='*90}\n")


# ── Parallel ablation worker ──────────────────────────────────────────────────

def _run_ablation_config(args) -> tuple[str, dict]:
    """Worker function for parallel ablation runs — must be top-level for pickling."""
    label, rules, all_data, brain, years = args
    start_eq = 100_000.0
    equity = start_eq
    all_equity = []
    for year in years:
        m = run_year(year, all_data, brain, rules, start_equity=equity)
        all_equity.append(m)
        equity *= (1 + m["total_return"] / 100)

    total_trades = sum(m["trades"] for m in all_equity)
    years_with_trades = [m for m in all_equity if m["trades"] > 0]
    avg_win_pct  = sum(m["avg_win"] for m in years_with_trades) / max(1, len(years_with_trades))
    avg_loss_pct = sum(m["avg_loss"] for m in years_with_trades) / max(1, len(years_with_trades))
    avg_sharpe   = sum(m["sharpe"] for m in all_equity) / len(all_equity)
    max_dd       = max(m["max_dd"] for m in all_equity)
    total_return = round((equity / start_eq - 1) * 100, 1)
    win_pct      = (sum(m["win_pct"] * m["trades"] for m in years_with_trades) / max(1, total_trades))
    combined = dict(trades=total_trades, win_pct=round(win_pct, 1), avg_win=round(avg_win_pct, 2),
                    avg_loss=round(avg_loss_pct, 2), sharpe=round(avg_sharpe, 2),
                    max_dd=round(max_dd, 1), total_return=total_return)
    return label, combined


# ── Fast ablation universe (diversified 23-symbol subset) ─────────────────────

FAST_UNIVERSE = [
    # Benchmarks
    "SPY", "QQQ", "IWM",
    # Tech / AI
    "NVDA", "MSFT", "GOOGL", "PLTR",
    # Chips / Memory (stress test)
    "AMD", "MU", "INTC",
    # Financials
    "JPM", "GS",
    # Healthcare / Biotech
    "LLY", "UNH", "MRNA",
    # Consumer
    "COST",
    # Sector ETFs
    "XLK", "XLE", "XLV", "XBI",
    # Leveraged
    "TQQQ", "SOXL",
]


# ── Year worker for parallel year runs ───────────────────────────────────────

def _run_year_parallel(args) -> tuple[int, dict]:
    """Worker for parallel year runs — each year starts fresh at $100k."""
    year, all_data, brain, rules = args
    return year, run_year(year, all_data, brain, rules, start_equity=100_000.0)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse, logging, json, os, hashlib, multiprocessing
    logging.basicConfig(level=logging.WARNING)

    parser = argparse.ArgumentParser(description="Kova Phase 2 Backtest")
    parser.add_argument("--parallel", action="store_true",
                        help="Run years in parallel (each starts fresh at $100k — faster)")
    parser.add_argument("--fast", action="store_true",
                        help="Use smaller 23-symbol ablation universe for quicker ablation runs")
    parser.add_argument("--sequential", action="store_true",
                        help="Run years sequentially with compounding equity (default)")
    args = parser.parse_args()

    use_parallel = args.parallel and not args.sequential
    use_fast     = args.fast

    years = [2020, 2021, 2022, 2023, 2024, 2025, 2026]

    mode_str = []
    if use_parallel: mode_str.append("parallel years (fresh $100k each)")
    else:            mode_str.append("sequential years (compounding)")
    if use_fast:     mode_str.append(f"fast ablation ({len(FAST_UNIVERSE)} symbols)")
    else:            mode_str.append(f"full ablation ({len(UNIVERSE)} symbols)")
    print(f"\nMode: {' | '.join(mode_str)}")
    print("Usage: python3 backtest_phase2.py [--parallel] [--fast] [--sequential]\n")

    # Fetch data once — need warmup before 2020 so start from 2019-01-01
    # ── Load / fetch data ─────────────────────────────────────────────────────
    cache_file = os.path.join(os.path.dirname(__file__), "backtest_cache.json")
    universe_hash = hashlib.md5(",".join(sorted(UNIVERSE)).encode()).hexdigest()[:8]

    cache_valid = False
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            raw_cache = json.load(f)
        if raw_cache.get("__universe_hash__") == universe_hash:
            cache_valid = True
        else:
            print(f"Universe changed — ignoring stale cache, re-fetching …")

    if cache_valid:
        print(f"Loading cached OHLCV data ({cache_file}) …")
        all_data = {}
        for sym, d in raw_cache.items():
            if sym.startswith("__"):
                continue
            all_data[sym] = {k: v for k, v in d.items()}
            all_data[sym]["dates"] = [date.fromisoformat(x) for x in d["dates"]]
        print(f"  {len(all_data)} symbols loaded  |  simulation logic runs fresh every time")
    else:
        print("Fetching data …")
        all_data = fetch_data(UNIVERSE, start="2019-01-01", end="2026-06-01")
        if all_data:
            cache_out = {"__universe_hash__": universe_hash}
            for sym, d in all_data.items():
                cache_out[sym] = {k: v for k, v in d.items()}
                cache_out[sym]["dates"] = [x.isoformat() for x in d["dates"]]
            with open(cache_file, "w") as f:
                json.dump(cache_out, f)
            print(f"  Cached raw OHLCV to {cache_file} (delete to force re-fetch)")

    if not all_data:
        print("ERROR: no data fetched — Yahoo Finance may be rate limiting, wait a few minutes and retry")
        sys.exit(1)
    print(f"  Got data for {len(all_data)} symbols")

    brain = _import_brain()
    n_cores = multiprocessing.cpu_count()

    # ── Year-by-year (full strategy) ──────────────────────────────────────────
    if use_parallel:
        print(f"Running year-by-year backtest (parallel, {min(len(years), n_cores)} cores, fresh $100k/year) …")
        year_args = [(year, all_data, brain, FULL_RULES) for year in years]
        with multiprocessing.Pool(processes=min(len(years), n_cores)) as pool:
            raw_year = pool.map(_run_year_parallel, year_args)
        year_results = sorted(raw_year, key=lambda x: x[0])
        for year, m in year_results:
            print(f"  {year} … {m['trades']} trades, Sharpe {m['sharpe']:.2f}")
    else:
        print("Running year-by-year backtest (sequential, compounding equity) …")
        year_results = []
        equity = 100_000.0
        for year in years:
            print(f"  {year} …", end="", flush=True)
            m = run_year(year, all_data, brain, FULL_RULES, start_equity=equity)
            year_results.append((year, m))
            equity *= (1 + m["total_return"] / 100)
            print(f" {m['trades']} trades, Sharpe {m['sharpe']:.2f}")

    print_year_table(year_results)

    # ── Ablation + negative tests ─────────────────────────────────────────────
    if use_fast:
        ablation_data = {s: d for s, d in all_data.items() if s in FAST_UNIVERSE}
        print(f"Running ablation tests (fast universe: {len(ablation_data)} symbols) …")
    else:
        ablation_data = all_data
        print(f"Running ablation tests (full universe: {len(ablation_data)} symbols) …")

    n_ablation_cores = min(len(ABLATION_CONFIGS), n_cores)
    print(f"  {len(ABLATION_CONFIGS)} configs in parallel across {n_ablation_cores} cores …")

    worker_args = [(label, rules, ablation_data, brain, years) for label, rules in ABLATION_CONFIGS]
    order = {label: i for i, (label, _) in enumerate(ABLATION_CONFIGS)}
    with multiprocessing.Pool(processes=n_ablation_cores) as pool:
        raw_results = pool.map(_run_ablation_config, worker_args)

    ablation_results = sorted(raw_results, key=lambda x: order[x[0]])
    for label, m in ablation_results:
        print(f"  {label:<40} return {m['total_return']:+.1f}%  Sharpe {m['sharpe']:.2f}")

    print_ablation_table(ablation_results)

    print("Phase 2 backtest complete. Review the tables above.")
    print("Key check: 2022 should survive with Sharpe > 0 and MaxDD < 25%.\n")

    # ── Save results + print diff vs last run ─────────────────────────────────
    _save_and_diff(year_results, ablation_results)


def _save_and_diff(year_results: list, ablation_results: list) -> None:
    import json, os, subprocess
    from datetime import datetime

    history_file = os.path.join(os.path.dirname(__file__), "backtest_history.json")

    # Get current git commit hash (best-effort)
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(__file__),
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        commit = "unknown"

    # Build this run's record
    baseline = next((m for label, m in ablation_results if "baseline" in label), {})
    current = {
        "timestamp": datetime.now().isoformat(),
        "commit": commit,
        "years": {str(year): m for year, m in year_results},
        "baseline": baseline,
        "ablations": {label: m for label, m in ablation_results},
    }

    # Load history
    history = []
    if os.path.exists(history_file):
        with open(history_file) as f:
            history = json.load(f)

    # ── Diff vs last run ──────────────────────────────────────────────────────
    if history:
        prev = history[-1]
        print(f"\n{'='*70}")
        print(f"  DIFF vs last run  ({prev['timestamp'][:16]}  commit {prev['commit']})")
        print(f"{'='*70}")

        # Year-by-year diff
        print(f"  {'Year':<6} {'Sharpe':>12} {'Return':>14} {'MaxDD':>12}")
        print(f"  {'-'*50}")
        for year, m in year_results:
            prev_y = prev["years"].get(str(year), {})
            if not prev_y:
                continue
            sharpe_diff = m["sharpe"] - prev_y["sharpe"]
            ret_diff    = m["total_return"] - prev_y["total_return"]
            dd_diff     = m["max_dd"] - prev_y["max_dd"]
            sharpe_icon = "↑" if sharpe_diff > 0.05 else ("↓" if sharpe_diff < -0.05 else "→")
            ret_icon    = "↑" if ret_diff > 0.5 else ("↓" if ret_diff < -0.5 else "→")
            dd_icon     = "↓" if dd_diff < -0.5 else ("↑" if dd_diff > 0.5 else "→")  # lower DD is better
            print(f"  {year:<6} {sharpe_icon} {m['sharpe']:>5.2f} ({sharpe_diff:>+.2f})"
                  f"  {ret_icon} {m['total_return']:>+5.1f}% ({ret_diff:>+.1f}%)"
                  f"  {dd_icon} {m['max_dd']:>4.1f}% ({dd_diff:>+.1f}%)")

        # Baseline summary diff
        pb = prev.get("baseline", {})
        if pb and baseline:
            print(f"\n  Overall baseline:")
            for key in ["sharpe", "total_return", "win_pct", "max_dd"]:
                diff = baseline.get(key, 0) - pb.get(key, 0)
                icon = "↑" if diff > 0 else ("↓" if diff < 0 else "→")
                if key == "max_dd":
                    icon = "↓" if diff < 0 else ("↑" if diff > 0 else "→")
                print(f"    {key:<16} {icon}  {baseline.get(key):>+.2f}  (was {pb.get(key):>+.2f}, Δ {diff:>+.2f})")
        print(f"{'='*70}\n")
    else:
        print("\n  (No previous run to diff against — this is the baseline.)\n")

    # Append and save
    history.append(current)
    with open(history_file, "w") as f:
        json.dump(history, f, indent=2)
    print(f"  Results saved to {history_file}  (run #{len(history)} total)\n")


if __name__ == "__main__":
    main()
