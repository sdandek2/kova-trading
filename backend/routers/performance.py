import math
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter
from services import alpaca_service

router = APIRouter(prefix="/api/performance", tags=["performance"])


def _compute_sharpe(returns: list[float], risk_free_rate: float = 0.05) -> float:
    if len(returns) < 2:
        return 0.0
    avg = sum(returns) / len(returns)
    variance = sum((r - avg) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(variance)
    if std == 0:
        return 0.0
    daily_rf = risk_free_rate / 252
    return round((avg - daily_rf) / std * math.sqrt(252), 2)


@router.get("/")
def get_performance():
    try:
        orders = alpaca_service.get_orders(limit=500)
        filled = [o for o in orders if o.status == "filled" and o.filled_avg_price]

        # Win rate: count profitable sell orders
        sells = [o for o in filled if o.side == "sell"]

        if not sells:
            win_rate = 0.0
            avg_win = 0.0
            avg_loss = 0.0
            profit_factor = 0.0
        else:
            wins = 0
            total_gain = 0.0
            total_loss = 0.0

            for order in sells:
                # Find matching buy for same symbol
                matching_buys = [o for o in filled if o.side == "buy" and o.symbol == order.symbol and o.created_at < order.created_at]
                if matching_buys:
                    buy = max(matching_buys, key=lambda x: x.created_at)
                    pnl = (order.filled_avg_price - buy.filled_avg_price) * order.qty
                    if pnl > 0:
                        wins += 1
                        total_gain += pnl
                    else:
                        total_loss += abs(pnl)

            win_rate = round(wins / len(sells) * 100, 1)
            avg_win = round(total_gain / wins, 2) if wins else 0.0
            avg_loss = round(total_loss / (len(sells) - wins), 2) if (len(sells) - wins) > 0 else 0.0
            profit_factor = round(total_gain / total_loss, 2) if total_loss > 0 else 0.0

        # Portfolio history for Sharpe — prefer our own DB (unlimited history),
        # fall back to Alpaca's 1M window if DB is empty (e.g. first day running)
        sharpe = 0.0
        portfolio_return = None
        history = []
        try:
            from services.db import get_daily_summaries
            db_summaries = get_daily_summaries(days=365)
            if len(db_summaries) >= 2:
                # Use our own daily snapshots — no Alpaca retention limit
                daily_returns = []
                for i in range(1, len(db_summaries)):
                    prev = db_summaries[i-1]["portfolio_value"]
                    curr = db_summaries[i]["portfolio_value"]
                    if prev > 0:
                        daily_returns.append((curr - prev) / prev)
                sharpe = _compute_sharpe(daily_returns)
                start_equity = db_summaries[0]["portfolio_value"]
                end_equity = db_summaries[-1]["portfolio_value"]
                if start_equity > 0:
                    portfolio_return = round((end_equity - start_equity) / start_equity * 100, 2)
                history = [{"date": s["date"], "equity": s["portfolio_value"]} for s in db_summaries]
            else:
                # Fall back to Alpaca for the first days before DB has data
                history = alpaca_service.get_portfolio_history(period="1M")
                daily_returns = []
                for i in range(1, len(history)):
                    prev = history[i-1]["equity"]
                    curr = history[i]["equity"]
                    if prev > 0:
                        daily_returns.append((curr - prev) / prev)
                sharpe = _compute_sharpe(daily_returns)
                if history and len(history) >= 2:
                    start_equity = history[0]["equity"]
                    end_equity = history[-1]["equity"]
                    if start_equity > 0:
                        portfolio_return = round((end_equity - start_equity) / start_equity * 100, 2)
        except Exception:
            sharpe = 0.0
            portfolio_return = None

        # SPY comparison: get SPY 1M return
        spy_return = None
        try:
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame
            from alpaca.data.historical import StockHistoricalDataClient
            from config import settings
            data_client = StockHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_secret_key)
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=30)
            bars_req = StockBarsRequest(symbol_or_symbols=["SPY"], timeframe=TimeFrame.Day, start=start, end=end)
            bars = data_client.get_stock_bars(bars_req)
            spy_bars = bars.get("SPY", [])
            if len(spy_bars) >= 2:
                spy_return = round((float(spy_bars[-1].close) - float(spy_bars[0].close)) / float(spy_bars[0].close) * 100, 2)
        except Exception:
            pass

        return {
            "total_trades": len(filled),
            "total_orders": len(orders),
            "filled_orders": len(filled),
            "win_rate": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": profit_factor,
            "sharpe_ratio": sharpe,
            "portfolio_return_1m": portfolio_return,
            "spy_return_1m": spy_return,
            "alpha": round(portfolio_return - spy_return, 2) if portfolio_return is not None and spy_return is not None else None,
        }
    except Exception:
        return {
            "total_trades": 0,
            "total_orders": 0,
            "filled_orders": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_factor": 0.0,
            "sharpe_ratio": 0.0,
            "portfolio_return_1m": None,
            "spy_return_1m": None,
            "alpha": None,
        }


@router.get("/scorecard")
def get_long_short_scorecard():
    """Win rate, avg winner/loser, profit factor, hold time — split by long vs short."""
    from services.db import get_long_short_scorecard
    return get_long_short_scorecard()


@router.get("/blocked-trades")
def get_blocked_trades_report():
    """Opportunity cost report: which block reason suppressed the most profit."""
    from services.db import get_blocked_trades_report
    return get_blocked_trades_report()


@router.get("/var")
def get_portfolio_var():
    """Latest portfolio VaR and gross exposure (updated each cycle)."""
    from services.db import get_portfolio_var as _fn
    return _fn()


@router.get("/by-setup")
def get_performance_by_setup():
    """P&L breakdown by setup type: momentum_breakout / mean_reversion / event_driven / extended_hours."""
    from services.db import get_performance_by_setup as _fn
    return _fn()


@router.get("/ai-baseline")
def get_ai_baseline_comparison():
    """Compare signal-only decisions vs Claude decisions; shows override rate."""
    from services.db import get_ai_baseline_comparison as _fn
    return _fn()


@router.get("/slippage")
def get_slippage_summary():
    """Avg slippage per trade, total cost, best/worst single fill."""
    from services.db import get_slippage_summary as _fn
    return _fn()


@router.get("/by-hour")
def get_performance_by_hour():
    """Win rate and avg P&L by entry hour (ET) for time-of-day analysis."""
    from services.db import get_performance_by_hour as _fn
    return _fn()


@router.get("/near-misses")
def get_near_misses():
    """Near-miss tracker: stocks that scored 35–44 and what they did next."""
    from services.db import get_near_misses_report as _fn
    return _fn()


@router.get("/sprint-review/latest")
def get_sprint_review_latest():
    """Most recent daily sprint review snapshot."""
    from services.db import get_latest_sprint_review as _fn
    return _fn()


@router.get("/sprint-review/weekly")
def get_sprint_review_weekly():
    """Last 7 days of sprint reviews + signal win rates."""
    from services.db import get_sprint_review_history, get_signal_win_rates
    history = get_sprint_review_history(days=7)
    win_rates = get_signal_win_rates(days=30)
    return {"daily_reviews": history, "signal_win_rates": win_rates}


@router.get("/signal-weights")
def get_signal_weights_endpoint():
    """Current signal weights — updated each Sunday by weight adjustment job."""
    from services.db import get_signal_weights, get_signal_win_rates, _get_conn
    weights = get_signal_weights()
    win_rates = {r["signal_name"]: r for r in get_signal_win_rates(days=30)}
    result = []
    conn = _get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT signal_name, current_weight, default_weight,
                           win_rate_30d, sample_count_30d, last_adjusted, adjustment_reason
                    FROM signal_weights ORDER BY signal_name
                """)
                cols = [d[0] for d in cur.description]
                for row in cur.fetchall():
                    d = dict(zip(cols, row))
                    d["last_adjusted"] = d["last_adjusted"].isoformat() if d["last_adjusted"] else None
                    wr = win_rates.get(d["signal_name"], {})
                    d["current_win_rate"] = float(wr.get("win_rate") or 0)
                    d["sample_count"] = int(wr.get("sample_count") or 0)
                    result.append(d)
        except Exception:
            pass
    return result
