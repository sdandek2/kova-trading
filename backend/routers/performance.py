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


@router.get("/report")
def get_performance_report(days: int = 30):
    from services.db import get_trade_metrics_report
    return get_trade_metrics_report(days=days)


@router.get("/diagnostics")
def get_performance_diagnostics(days: int = 30, min_trades: int = 3):
    from services.db import get_trade_diagnostics_report
    return get_trade_diagnostics_report(days=days, min_trades=min_trades)


@router.get("/replay")
def get_performance_replay(
    days: int = 180,
    min_prior_trades: int = 3,
    negative_cutoff: float = -0.35,
    positive_boost_cutoff: float = 0.35,
):
    from services.replay_service import run_trade_replay
    return run_trade_replay(
        days=days,
        min_prior_trades=min_prior_trades,
        negative_cutoff=negative_cutoff,
        positive_boost_cutoff=positive_boost_cutoff,
    )
