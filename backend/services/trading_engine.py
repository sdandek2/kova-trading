import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from models.trade import TradingStatus, AIAnalysis
from services import alpaca_service, claude_service
from services.db import cache_get, cache_set, log_trade_decision
from services.indicators import compute_atr, compute_rsi, volatility_adjusted_quantity
from services.entry_timing import should_confirm_entry, get_scale_in_quantity, should_scale_out
from services.earnings import get_upcoming_earnings
from services.macro import get_macro_context, get_sector_rotation
from services.geopolitical import get_geopolitical_context, get_trend_forecast
from websocket.manager import manager

logger = logging.getLogger(__name__)

TRADING_INTERVAL_SECONDS = 600  # 10 minutes

_risk_settings = {
    "daily_loss_limit_pct": 2.0,   # stop trading if down 2% today
    "stop_loss_pct": 0.03,          # 3% stop loss on each trade
    "take_profit_pct": 0.05,        # 5% take profit on each trade
}
# No fixed watchlist — universe is built dynamically each cycle from top movers + actives

_is_running = False
_last_analysis_at: Optional[datetime] = None
_next_run_at: Optional[datetime] = None
_latest_analysis: Optional[AIAnalysis] = None
_task: Optional[asyncio.Task] = None


def get_status() -> TradingStatus:
    next_run_in = None
    if _next_run_at:
        delta = (_next_run_at - datetime.now(timezone.utc)).total_seconds()
        next_run_in = max(0, int(delta))
    return TradingStatus(
        is_running=_is_running,
        last_analysis_at=_last_analysis_at,
        next_run_in_seconds=next_run_in,
    )


def get_latest_analysis() -> Optional[AIAnalysis]:
    global _latest_analysis
    if _latest_analysis is not None:
        return _latest_analysis
    # Attempt to restore from the persistent cache after a restart
    try:
        cached = cache_get("latest_ai_decision")
        if cached:
            _latest_analysis = AIAnalysis(**cached)
            logger.info("Restored latest AI analysis from Postgres cache.")
            return _latest_analysis
    except Exception as e:
        logger.warning(f"Could not restore latest_ai_decision from cache: {e}")
    return None


async def run_trading_cycle():
    global _last_analysis_at, _next_run_at, _latest_analysis

    logger.info("Running trading cycle...")

    try:
        if not alpaca_service.is_market_open():
            logger.info("Market is closed. Skipping cycle.")
            return

        # Circuit breaker: stop if down more than daily loss limit
        account = alpaca_service.get_account()
        if account.day_pl_percent < -_risk_settings["daily_loss_limit_pct"]:
            logger.warning(f"Daily loss limit hit ({account.day_pl_percent:.2f}%). Stopping trading.")
            stop()
            await manager.broadcast({
                "type": "circuit_breaker",
                "data": {"reason": f"Daily loss limit of {_risk_settings['daily_loss_limit_pct']}% hit", "day_pl_percent": account.day_pl_percent}
            })
            return

        positions = alpaca_service.get_positions()
        universe = alpaca_service.get_tradeable_universe()
        # Always include user's watchlist stocks in the scan
        from routers.watchlist import load_watchlist
        user_watchlist = load_watchlist()
        universe = list(dict.fromkeys(user_watchlist + universe))  # watchlist first, deduplicated
        logger.info(f"Universe: {len(universe)} stocks (including {len(user_watchlist)} from watchlist)")
        # Lightweight snapshot for broad scan (Step 1) — price + 5-day change only
        snapshot_light = alpaca_service.get_market_snapshot_light(universe)
        sentiment = alpaca_service.get_sentiment_context(universe)
        macro = get_macro_context()
        sector_info = get_sector_rotation()
        earnings_map = get_upcoming_earnings(universe)
        geo = get_geopolitical_context()
        trend_forecast = get_trend_forecast(macro, geo)
        logger.info(f"Macro: {macro['market_regime']} | VIX: {macro['vix_level']} | SPY: {macro['spy_trend']}")
        logger.info(f"Geopolitical risk: {geo['risk_level'].upper()} (score={geo['risk_score']}) | Themes: {geo['dominant_themes']}")

        # ── Scale-out: review existing positions for partial profit-taking / loss cuts ──
        for position in positions:
            sym_data = snapshot_light.get(position.symbol, {})
            closing_prices = sym_data.get("closing_prices", [])
            rsi = compute_rsi(closing_prices) if closing_prices else 50.0

            scale_out, fraction, reason = should_scale_out(
                position_unrealized_pl_percent=position.unrealized_pl_percent,
                rsi=rsi,
                symbol=position.symbol,
            )

            if scale_out:
                sell_qty = max(1, int(float(position.qty) * fraction))
                logger.info(f"Scale-out triggered: {reason} — selling {sell_qty} shares")
                order = alpaca_service.submit_market_order(
                    symbol=position.symbol,
                    qty=sell_qty,
                    side="sell",
                )
                if order:
                    await manager.broadcast({"type": "order_filled", "data": order.model_dump(mode="json")})
                    await manager.broadcast({
                        "type": "ai_analysis",
                        "data": {
                            "reasoning": reason,
                            "last_action": "sell",
                            "symbol": position.symbol,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        },
                    })

        decision = claude_service.analyze_and_decide(
            market_snapshot=snapshot_light,
            positions=positions,
            account_cash=account.cash,
            portfolio_value=account.portfolio_value,
            sentiment=sentiment,
            macro=macro,
            sector_info=sector_info,
            earnings_map=earnings_map,
            geo_context=geo,
            trend_forecast=trend_forecast,
            full_data_fetcher=lambda symbols: alpaca_service.get_market_snapshot(symbols),
        )

        _last_analysis_at = datetime.now(timezone.utc)
        _latest_analysis = AIAnalysis(
            reasoning=decision.reasoning,
            last_action=decision.action,
            symbol=decision.symbol,
            timestamp=_last_analysis_at,
        )

        # Persist so the analysis survives Railway restarts
        cache_set("latest_ai_decision", _latest_analysis.model_dump(mode="json"), 86400)

        # Log the AI decision for future performance analysis
        log_trade_decision({
            "timestamp":     _last_analysis_at,
            "action":        decision.action,
            "symbol":        decision.symbol,
            "quantity":      decision.quantity,
            "reasoning":     decision.reasoning,
            "confidence":    getattr(decision, "confidence", None),
            "market_regime": macro.get("market_regime"),
            "geo_risk":      geo.get("risk_level"),
        })

        logger.info(f"AI decision: {decision.action} {decision.symbol} x{decision.quantity}")
        logger.info(f"Reasoning: {decision.reasoning}")

        await manager.broadcast(
            {
                "type": "ai_analysis",
                "data": _latest_analysis.model_dump(mode="json"),
            }
        )

        if decision.action in ("buy", "sell") and decision.symbol and decision.quantity:
            # ── Entry confirmation: verify price action supports the signal ──
            # Fetch deep data for the chosen symbol if not already in light snapshot
            deep = alpaca_service.get_market_snapshot([decision.symbol])
            sym_data = deep.get(decision.symbol) or snapshot_light.get(decision.symbol, {})
            closing_prices = sym_data.get("closing_prices", [])
            current_price = sym_data.get("current_price") or 0

            confirmed, confirm_reason = should_confirm_entry(
                symbol=decision.symbol,
                action=decision.action,
                closing_prices=closing_prices,
                current_price=current_price,
            )

            if not confirmed:
                logger.info(f"Entry rejected by confirmation: {confirm_reason}")
                _latest_analysis = AIAnalysis(
                    reasoning=(
                        f"Signal detected but entry not confirmed: {confirm_reason}. "
                        f"Original analysis: {decision.reasoning}"
                    ),
                    last_action="waiting",
                    symbol=decision.symbol,
                    timestamp=datetime.now(timezone.utc),
                )
                await manager.broadcast({"type": "ai_analysis", "data": _latest_analysis.model_dump(mode="json")})
                return  # Skip trade execution, wait for next cycle

            if decision.action == "buy":
                # Override quantity with volatility-adjusted sizing
                atr = compute_atr(
                    highs=sym_data.get("high_prices", []),
                    lows=sym_data.get("low_prices", []),
                    closes=closing_prices,
                )
                from services.strategy import get_strategy
                strat = get_strategy()
                price = sym_data.get("current_price") or 1
                vol_qty = volatility_adjusted_quantity(
                    portfolio_value=account.portfolio_value,
                    max_position_pct=strat["max_position_pct"],
                    current_price=price,
                    atr=atr,
                )
                # Use volatility-adjusted quantity, log the difference
                if vol_qty != decision.quantity:
                    logger.info(f"Volatility adjustment: Claude suggested {decision.quantity} shares, using {vol_qty} (ATR={atr:.2f})")
                decision.quantity = vol_qty

                # ── Scale-in: enter gradually rather than all at once ──
                existing_pos = next((p for p in positions if p.symbol == decision.symbol), None)
                existing_qty = float(existing_pos.qty) if existing_pos else 0

                scaled_qty = get_scale_in_quantity(
                    base_quantity=decision.quantity,
                    confidence="high",
                    existing_position_qty=existing_qty,
                    max_total_qty=decision.quantity,
                )
                if scaled_qty != decision.quantity:
                    logger.info(
                        f"Scale-in: buying {scaled_qty} of {decision.quantity} planned shares of {decision.symbol}"
                    )
                    decision.quantity = scaled_qty

            order = alpaca_service.submit_market_order(
                symbol=decision.symbol,
                qty=decision.quantity,
                side=decision.action,
                stop_loss_pct=_risk_settings["stop_loss_pct"],
                take_profit_pct=_risk_settings["take_profit_pct"],
            )
            if order:
                await manager.broadcast(
                    {
                        "type": "order_filled",
                        "data": order.model_dump(mode="json"),
                    }
                )
                updated_positions = alpaca_service.get_positions()
                await manager.broadcast(
                    {
                        "type": "position_update",
                        "data": [p.model_dump(mode="json") for p in updated_positions],
                    }
                )

    except Exception as e:
        logger.error(f"Trading cycle error: {e}", exc_info=True)


async def _trading_loop():
    global _next_run_at
    from datetime import timedelta
    while _is_running:
        await run_trading_cycle()

        # Sleep longer when market is closed (nights / weekends)
        # so we don't spin every 5 min for 18 hours doing nothing
        if alpaca_service.is_market_open():
            sleep_seconds = TRADING_INTERVAL_SECONDS
        else:
            sleep_seconds = 900  # check every 15 min when closed
            logger.info("Market closed — checking again in 15 minutes")

        _next_run_at = datetime.now(timezone.utc) + timedelta(seconds=sleep_seconds)
        await asyncio.sleep(sleep_seconds)


def start():
    global _is_running, _task
    if _is_running:
        return
    _is_running = True
    _task = asyncio.create_task(_trading_loop())
    logger.info("Trading engine started.")


def stop():
    global _is_running, _task
    _is_running = False
    if _task:
        _task.cancel()
        _task = None
    logger.info("Trading engine stopped.")
