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

_RISK_CACHE_KEY = "user_pref:risk_settings"
_RISK_DEFAULTS = {
    "daily_loss_limit_pct": 3.0,   # stop trading if down this % today
    "stop_loss_pct": 0.05,          # 5% trailing stop fallback (Claude overrides per trade)
    "take_profit_pct": 0.15,        # 15% TP fallback (Claude overrides per trade)
    "min_daily_trades": 2,          # trigger afternoon pressure if below this by cutoff hour
    "afternoon_pressure_hour": 14,  # EST hour (24h) — e.g. 14 = 2:00 PM EST
}


def _load_risk_settings() -> dict:
    """Load risk settings from persistent cache, falling back to defaults."""
    from services.db import cache_get
    cached = cache_get(_RISK_CACHE_KEY)
    if isinstance(cached, dict):
        # Merge with defaults so new keys are always present
        return {**_RISK_DEFAULTS, **cached}
    return _RISK_DEFAULTS.copy()


def _save_risk_settings(settings: dict) -> None:
    from services.db import cache_set
    cache_set(_RISK_CACHE_KEY, settings, 365 * 24 * 3600)  # 1 year TTL


_risk_settings = _load_risk_settings()
# No fixed watchlist — universe is built dynamically each cycle from top movers + actives

_is_running = False
_last_analysis_at: Optional[datetime] = None
_next_run_at: Optional[datetime] = None
_latest_analysis: Optional[AIAnalysis] = None
_task: Optional[asyncio.Task] = None
_daily_trade_count: dict = {}  # date → int, tracks trades executed per day


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


async def run_premarket_scan():
    """
    Runs at 9:00-9:30 AM EST before market open.
    Identifies top opportunities from overnight news and pre-market movers.
    Stores results in cache so the first trading cycle uses them immediately.
    """
    try:
        now_utc = datetime.now(timezone.utc)
        if not (13 <= now_utc.hour < 14):
            return

        logger.info("Running pre-market scan (13:00-14:00 UTC)...")

        universe = alpaca_service.get_tradeable_universe()
        news_articles = alpaca_service.get_news(limit=20)
        macro = get_macro_context()

        # Build news headlines list
        news_headlines = [
            f"[{art.get('source', '')}] [{', '.join(art.get('symbols', [])[:3])}] {art['headline']}"
            for art in news_articles[:15]
            if art.get("headline")
        ]

        # Derive sentiment from news articles
        sentiment = {}
        for art in news_articles:
            for sym in art.get("symbols", []):
                if sym in universe:
                    sentiment[sym] = sentiment.get(sym, 0) + 1

        # Top movers from news (symbols with most mentions)
        top_movers = sorted(sentiment.items(), key=lambda x: x[1], reverse=True)[:5]
        top_movers_str = ", ".join([f"{sym}({cnt})" for sym, cnt in top_movers]) or "none identified"

        # Format a simple pre-market context string
        market_regime = macro.get("market_regime", "unknown").upper()
        premarket_context = (
            f"Pre-market scan: regime={market_regime}, "
            f"top news movers={top_movers_str}, "
            f"articles={len(news_articles)}"
        )

        cache_set(
            "premarket_scan",
            {
                "headlines": news_headlines,
                "sentiment": sentiment,
                "macro_regime": macro.get("market_regime"),
                "scanned_at": datetime.now(timezone.utc).isoformat(),
            },
            7200,  # 2 hour TTL
        )

        logger.info(f"Pre-market scan complete: {premarket_context}")
    except Exception as e:
        logger.warning(f"Pre-market scan failed (non-fatal): {e}")


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
        logger.info(f"Universe: {len(universe)} stocks — 100% market-driven (top movers, volume, news, sectors)")
        # Lightweight snapshot for broad scan (Step 1) — price + 5-day change only
        snapshot_light = alpaca_service.get_market_snapshot_light(universe)

        # ── Fetch multi-source news ONCE per cycle ──
        # Used for both sentiment scoring AND passing headlines to the AI prompt
        news_articles = []
        try:
            news_articles = alpaca_service.get_news(symbols=universe[:10], limit=30)
            logger.info(f"Fetched {len(news_articles)} news articles from multi-source feed")
        except Exception as e:
            logger.warning(f"Could not fetch news: {e}")

        # Derive sentiment from news: count articles mentioning each symbol
        sentiment = {}
        for art in news_articles:
            for sym in art.get("symbols", []):
                if sym in universe:
                    sentiment[sym] = sentiment.get(sym, 0) + 1

        # Top headlines for AI context (most recent 15)
        news_headlines = [
            f"[{art.get('source', '')}] [{', '.join(art.get('symbols', [])[:3])}] {art['headline']}"
            for art in news_articles[:15]
            if art.get("headline")
        ]

        macro = get_macro_context()
        sector_info = get_sector_rotation()

        # Sector momentum scores — used to boost/reduce conviction per symbol
        from services.sector_momentum import get_sector_momentum_scores, get_sector_context_for_symbols
        sector_scores = {}
        sector_context = {}
        try:
            sector_scores = get_sector_momentum_scores(lookback_days=3)
            sector_context = get_sector_context_for_symbols(universe[:30], sector_scores)
            leading_sectors = [f"{s}({v:+.1f}%)" for s, v in sorted(sector_scores.items(), key=lambda x: x[1], reverse=True)[:3]]
            logger.info(f"Sector momentum — Leading: {', '.join(leading_sectors)}")
        except Exception as e:
            logger.warning(f"Sector momentum failed (non-fatal): {e}")

        # Recent trade outcomes — fed back to Claude so it learns from past decisions
        from services.db import get_recent_trade_outcomes
        recent_trades = []
        try:
            recent_trades = get_recent_trade_outcomes(limit=10)
        except Exception as e:
            logger.warning(f"Could not fetch trade history (non-fatal): {e}")

        earnings_map = get_upcoming_earnings(universe)

        # Earnings play candidates — small pre-earnings run-up plays
        from services.earnings import get_earnings_play_candidates
        earnings_plays = []
        try:
            earnings_plays = get_earnings_play_candidates(universe, earnings_map)
            if earnings_plays:
                logger.info(f"Earnings play candidates: {[p['symbol'] for p in earnings_plays]}")
        except Exception as e:
            logger.warning(f"Earnings plays failed (non-fatal): {e}")

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

        # ── Time-of-day filter: skip chaotic opening 15 minutes ──
        from services.entry_timing import is_good_trading_window
        window_ok, window_reason = is_good_trading_window()
        if not window_ok:
            logger.info(f"Trading window: {window_reason} — skipping cycle")
            return

        # ── Daily trade floor: configurable via /api/risk/settings ──
        now_utc = datetime.now(timezone.utc)
        trades_today = _daily_trade_count.get(now_utc.date(), 0)
        min_trades = int(_risk_settings.get("min_daily_trades", 2))
        # afternoon_pressure_hour is in EST; UTC offset is +4 (EDT) or +5 (EST)
        # Using +4 (EDT, summer) as default — close enough for this purpose
        pressure_hour_est = int(_risk_settings.get("afternoon_pressure_hour", 14))
        pressure_hour_utc = pressure_hour_est + 4
        afternoon_pressure = (now_utc.hour >= pressure_hour_utc and trades_today < min_trades)
        if afternoon_pressure:
            logger.info(f"Afternoon pressure: only {trades_today}/{min_trades} trades today — lowering thresholds")

        from services.strategy import get_strategy
        strat = get_strategy()
        strategy_key = strat["key"]

        decisions = claude_service.analyze_and_decide(
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
            news_headlines=news_headlines,
            full_data_fetcher=lambda symbols: alpaca_service.get_market_snapshot(symbols),
            sector_context=sector_context,
            recent_trades=recent_trades,
            earnings_plays=earnings_plays,
            afternoon_pressure=afternoon_pressure,
        )

        _last_analysis_at = datetime.now(timezone.utc)

        # Broadcast + log each decision
        for decision in decisions:
            _latest_analysis = AIAnalysis(
                reasoning=decision.reasoning,
                last_action=decision.action,
                symbol=decision.symbol,
                timestamp=_last_analysis_at,
            )
            cache_set("latest_ai_decision", _latest_analysis.model_dump(mode="json"), 86400)
            log_trade_decision({
                "timestamp":       _last_analysis_at,
                "action":          decision.action,
                "symbol":          decision.symbol,
                "quantity":        decision.quantity,
                "reasoning":       decision.reasoning,
                "confidence":      None,
                "market_regime":   macro.get("market_regime"),
                "geo_risk":        geo.get("risk_level"),
                "take_profit_pct": decision.take_profit_pct,
                "stop_loss_pct":   decision.stop_loss_pct,
                "partial_exit":    decision.partial_exit,
            })
            logger.info(f"AI decision: {decision.action} {decision.symbol} x{decision.quantity}")
            await manager.broadcast({"type": "ai_analysis", "data": _latest_analysis.model_dump(mode="json")})

            if decision.action not in ("buy", "sell") or not decision.symbol or not decision.quantity:
                continue

            # ── Entry confirmation (strategy-aware) ──
            deep = alpaca_service.get_market_snapshot([decision.symbol])
            sym_data = deep.get(decision.symbol) or snapshot_light.get(decision.symbol, {})
            closing_prices = sym_data.get("closing_prices", [])
            current_price = sym_data.get("current_price") or 0

            confirmed, confirm_reason = should_confirm_entry(
                symbol=decision.symbol,
                action=decision.action,
                closing_prices=closing_prices,
                current_price=current_price,
                strategy_key=strategy_key,
                positions_count=len(positions),
            )

            if not confirmed:
                logger.info(f"Entry rejected: {confirm_reason}")
                await manager.broadcast({"type": "ai_analysis", "data": {
                    "reasoning": f"Entry not confirmed: {confirm_reason}",
                    "last_action": "waiting", "symbol": decision.symbol,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }})
                continue  # Try next decision, don't abort the whole cycle

            if decision.action == "buy":
                # Volatility-adjusted sizing
                atr = compute_atr(
                    highs=sym_data.get("high_prices", []),
                    lows=sym_data.get("low_prices", []),
                    closes=closing_prices,
                )
                price = sym_data.get("current_price")
                if price and price > 0:
                    vol_qty = volatility_adjusted_quantity(
                        portfolio_value=account.portfolio_value,
                        max_position_pct=strat["max_position_pct"],
                        current_price=price,
                        atr=atr,
                    )
                    if vol_qty != decision.quantity:
                        logger.info(f"Vol-adjust: {decision.symbol} {decision.quantity}→{vol_qty} shares (ATR={atr:.2f})")
                    decision.quantity = vol_qty

                # Scale-in (strategy-aware — aggressive takes full position)
                existing_pos = next((p for p in positions if p.symbol == decision.symbol), None)
                existing_qty = float(existing_pos.qty) if existing_pos else 0
                scaled_qty = get_scale_in_quantity(
                    base_quantity=decision.quantity,
                    confidence="high",
                    existing_position_qty=existing_qty,
                    max_total_qty=decision.quantity,
                    strategy_key=strategy_key,
                )
                if scaled_qty != decision.quantity:
                    logger.info(f"Scale-in: {decision.symbol} buying {scaled_qty}/{decision.quantity} planned")
                decision.quantity = scaled_qty

            order = alpaca_service.submit_market_order(
                symbol=decision.symbol,
                qty=decision.quantity,
                side=decision.action,
                stop_loss_pct=decision.stop_loss_pct or _risk_settings["stop_loss_pct"],
                take_profit_pct=decision.take_profit_pct or _risk_settings["take_profit_pct"],
                partial_exit=decision.partial_exit,
            )
            if order:
                # Track daily trade count
                today = datetime.now(timezone.utc).date()
                _daily_trade_count[today] = _daily_trade_count.get(today, 0) + 1
                logger.info(f"✅ Order executed: {decision.action.upper()} {decision.symbol} x{decision.quantity} | trades today: {_daily_trade_count[today]}")
                await manager.broadcast({"type": "order_filled", "data": order.model_dump(mode="json")})
                updated_positions = alpaca_service.get_positions()
                await manager.broadcast({"type": "position_update", "data": [p.model_dump(mode="json") for p in updated_positions]})

    except Exception as e:
        logger.error(f"Trading cycle error: {e}", exc_info=True)


async def _save_eod_snapshot():
    """
    Save an end-of-day performance snapshot to daily_summary.
    Called once automatically when the market transitions from open → closed.
    """
    try:
        from services.db import save_daily_summary
        from services.strategy import get_strategy
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.historical import StockHistoricalDataClient
        from config import settings as _settings

        account = alpaca_service.get_account()
        today = datetime.now(timezone.utc).date()

        # Count today's AI decisions from trade_log
        from services.db import _get_conn
        conn = _get_conn()
        totals = {"total": 0, "buy": 0, "sell": 0, "hold": 0}
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT action, COUNT(*) FROM trade_log
                        WHERE timestamp::date = %s
                        GROUP BY action
                    """, (today,))
                    for action, cnt in cur.fetchall():
                        totals["total"] += cnt
                        if action in totals:
                            totals[action] += cnt
            except Exception as e:
                logger.warning(f"EOD: could not count today's decisions: {e}")

        # Get SPY close price
        spy_close = None
        try:
            data_client = StockHistoricalDataClient(_settings.alpaca_api_key, _settings.alpaca_secret_key)
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=2)
            bars = data_client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=["SPY"], timeframe=TimeFrame.Day, start=start, end=end
            ))
            spy_bars = bars.get("SPY", [])
            if spy_bars:
                spy_close = float(spy_bars[-1].close)
        except Exception as e:
            logger.warning(f"EOD: could not fetch SPY close: {e}")

        strat = get_strategy()
        save_daily_summary({
            "date":             today,
            "portfolio_value":  account.portfolio_value,
            "cash":             account.cash,
            "day_pl":           account.day_pl,
            "day_pl_pct":       account.day_pl_percent,
            "total_decisions":  totals["total"],
            "buy_decisions":    totals["buy"],
            "sell_decisions":   totals["sell"],
            "hold_decisions":   totals["hold"],
            "strategy":         strat["key"],
            "spy_close":        spy_close,
        })
        logger.info(f"EOD snapshot saved for {today}: portfolio=${account.portfolio_value:,.2f}, day_pl={account.day_pl_percent:.2f}%")
    except Exception as e:
        logger.warning(f"EOD snapshot failed (non-fatal): {e}")


async def _trading_loop():
    global _next_run_at
    from datetime import timedelta
    _cleanup_counter = 0
    _premarket_scanned_date = None  # track which date we last ran the pre-market scan
    _eod_saved_date = None          # track which date we last saved the EOD snapshot
    _market_was_open = False        # detect open→closed transition
    while _is_running:
        # Run pre-market scan once per day at 13:00-14:00 UTC (9-10 AM EST)
        now_utc = datetime.now(timezone.utc)
        today = now_utc.date()
        if 13 <= now_utc.hour < 14 and _premarket_scanned_date != today:
            _premarket_scanned_date = today
            await run_premarket_scan()

        await run_trading_cycle()

        # Detect market close → save EOD snapshot once per day
        market_open_now = alpaca_service.is_market_open()
        if _market_was_open and not market_open_now and _eod_saved_date != today:
            _eod_saved_date = today
            logger.info("Market just closed — saving EOD snapshot.")
            await _save_eod_snapshot()
        _market_was_open = market_open_now

        # Run DB cleanup once every ~144 cycles (~24 hours at 10-min intervals)
        _cleanup_counter += 1
        if _cleanup_counter >= 144:
            _cleanup_counter = 0
            from services.db import cleanup_old_trade_logs, cleanup_expired_cache
            cleanup_old_trade_logs(days=90)
            cleanup_expired_cache()
            logger.info("DB cleanup complete.")

        # Sleep longer when market is closed (nights / weekends)
        # so we don't spin every 5 min for 18 hours doing nothing
        if market_open_now:
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
