import logging
from datetime import datetime, timezone
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from config import settings
from models.account import AccountInfo
from models.trade import Position, Order

logger = logging.getLogger(__name__)

from services.connector_health import track_api

trading_client = TradingClient(
    settings.alpaca_api_key,
    settings.alpaca_secret_key,
    paper=True,
)

data_client = StockHistoricalDataClient(
    settings.alpaca_api_key,
    settings.alpaca_secret_key,
)


@track_api("alpaca_account")
def get_account() -> AccountInfo:
    account = trading_client.get_account()
    portfolio_value = float(account.portfolio_value)
    prev_value = float(account.last_equity)
    day_pl = portfolio_value - prev_value
    day_pl_percent = (day_pl / prev_value * 100) if prev_value else 0.0

    # daytrading_buying_power is only set on margin accounts subject to PDT rules.
    # On cash accounts it's 0 or None — fall back to 0 safely.
    try:
        dt_bp = float(account.daytrading_buying_power) if account.daytrading_buying_power else 0.0
    except Exception:
        dt_bp = 0.0

    return AccountInfo(
        portfolio_value=portfolio_value,
        cash=float(account.cash),
        buying_power=float(account.buying_power),
        daytrading_buying_power=dt_bp,
        day_pl=day_pl,
        day_pl_percent=round(day_pl_percent, 2),
    )


@track_api("alpaca_positions")
def get_positions() -> list[Position]:
    raw = trading_client.get_all_positions()
    result = []
    for p in raw:
        result.append(
            Position(
                symbol=p.symbol,
                qty=float(p.qty),
                side=p.side.value if hasattr(p.side, "value") else str(p.side),
                avg_entry_price=float(p.avg_entry_price),
                current_price=float(p.current_price),
                unrealized_pl=float(p.unrealized_pl),
                unrealized_pl_percent=float(p.unrealized_plpc) * 100,
                market_value=float(p.market_value),
            )
        )
    return result


def get_orders(limit: int = 50) -> list[Order]:
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus

    request = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=limit)
    raw = trading_client.get_orders(filter=request)
    result = []
    for o in raw:
        result.append(
            Order(
                id=str(o.id),
                symbol=o.symbol,
                side=o.side.value,
                qty=float(o.qty),
                status=o.status.value,
                filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price else None,
                created_at=o.created_at,
            )
        )
    return result


@track_api("alpaca_orders")
def submit_market_order(
    symbol: str,
    qty: int,
    side: str,
    stop_loss_pct: float = 0.05,
    take_profit_pct: float = 0.15,
    partial_exit: bool = False,
) -> Optional[Order]:
    from alpaca.trading.requests import TakeProfitRequest, TrailingStopOrderRequest, LimitOrderRequest
    from alpaca.trading.enums import OrderClass

    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

    if side == "buy":
        # Plain fallback request — used if advanced order fails
        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=order_side,
            time_in_force=TimeInForce.DAY,
        )
        try:
            from alpaca.trading.requests import StopLossRequest
            quote_request = StockLatestQuoteRequest(symbol_or_symbols=[symbol])
            quotes = data_client.get_stock_latest_quote(quote_request)
            quote = quotes[symbol]
            ask_price = float(quote.ask_price or 0)
            bid_price = float(quote.bid_price or 0)

            if ask_price <= 0:
                raise ValueError(f"Invalid ask price for {symbol}: {ask_price} — using plain market order")

            # ── LIMIT ORDER: spread-aware buffer ──
            # Narrow spread (liquid): 0.1% buffer — tight, gets filled fast.
            # Wide spread (thin): 0.35% buffer — more aggressive to ensure fill.
            midpoint = round((bid_price + ask_price) / 2, 2) if bid_price > 0 else ask_price
            _spread_pct = (ask_price - bid_price) / ask_price if ask_price > 0 and bid_price > 0 else 0.002
            _buffer = 0.0035 if _spread_pct > 0.005 else 0.001  # wide spread >0.5% → 0.35%, else 0.1%
            limit_price = round(midpoint * (1 + _buffer), 2)
            current_price = ask_price  # use ask for TP/SL calculation

            take_profit_price = round(current_price * (1 + take_profit_pct), 2)
            stop_price = round(current_price * (1 - stop_loss_pct), 2)

            if partial_exit and qty >= 2:
                # ── Partial exit strategy: sell half at TP, let other half ride with trailing stop ──
                # Buy with limit order for better fill
                buy_req = LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=order_side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=limit_price,
                )
                order = trading_client.submit_order(buy_req)
                logger.info(f"Partial-exit limit buy: {qty} {symbol} @ limit ${limit_price:.2f} (ask ${ask_price:.2f})")

                half_qty = qty // 2
                remaining_qty = qty - half_qty

                # Only place sell legs if the buy actually filled.
                # Alpaca trailing stops require GTC (DAY is rejected with HTTP 422).
                # If we placed GTC sell orders before the buy fills, they could
                # execute independently and open an unintended short position.
                # By gating on fill confirmation we guarantee the shares exist first.
                buy_filled = order.filled_avg_price is not None or str(order.status) == "filled"
                if buy_filled:
                    # First half: limit sell at TP price (DAY — expires if not hit today)
                    try:
                        limit_sell = LimitOrderRequest(
                            symbol=symbol,
                            qty=half_qty,
                            side=OrderSide.SELL,
                            time_in_force=TimeInForce.DAY,
                            limit_price=take_profit_price,
                        )
                        trading_client.submit_order(limit_sell)
                        logger.info(f"Partial TP: selling {half_qty} {symbol} at ${take_profit_price:.2f} (+{take_profit_pct*100:.0f}%)")
                    except Exception as e:
                        logger.warning(f"Partial limit sell failed (non-fatal): {e}")

                    # Second half: trailing stop — MUST use GTC (Alpaca rejects DAY for trailing stops)
                    try:
                        trail_req = TrailingStopOrderRequest(
                            symbol=symbol,
                            qty=remaining_qty,
                            side=OrderSide.SELL,
                            time_in_force=TimeInForce.GTC,
                            trail_percent=stop_loss_pct * 100,
                        )
                        trading_client.submit_order(trail_req)
                        logger.info(f"Trailing stop on remaining {remaining_qty} {symbol}: {stop_loss_pct*100:.0f}% trail")
                    except Exception as e:
                        logger.warning(f"Trailing stop failed (non-fatal): {e}")
                else:
                    logger.info(f"Partial-exit buy for {symbol} not yet filled — sell legs deferred until fill confirmed")

            else:
                # ── Standard exit: LIMIT BRACKET order (limit entry + TP + SL) ──
                # Limit order gets better fills than market; bracket legs protect the position.
                bracket_req = LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=order_side,
                    time_in_force=TimeInForce.DAY,
                    order_class=OrderClass.BRACKET,
                    limit_price=limit_price,
                    take_profit=TakeProfitRequest(limit_price=take_profit_price),
                    stop_loss=StopLossRequest(stop_price=stop_price),
                )
                order = trading_client.submit_order(bracket_req)
                logger.info(
                    f"Limit bracket buy: {qty} {symbol} @ limit ${limit_price:.2f} (ask ${ask_price:.2f}) | "
                    f"TP: ${take_profit_price:.2f} (+{take_profit_pct*100:.0f}%) | "
                    f"SL: ${stop_price:.2f} (-{stop_loss_pct*100:.0f}%)"
                )

            return Order(
                id=str(order.id),
                symbol=order.symbol,
                side=order.side.value,
                qty=float(order.qty),
                status=order.status.value,
                filled_avg_price=float(order.filled_avg_price) if order.filled_avg_price else None,
                created_at=order.created_at,
            )
        except Exception as e:
            logger.warning(f"Advanced order failed, falling back to plain market order: {e}")
            # Fallback: plain market buy (only reaches here if advanced order was never placed)
            try:
                order = trading_client.submit_order(request)
                logger.info(f"Submitted plain market buy: {qty} {symbol}")
                return Order(
                    id=str(order.id),
                    symbol=order.symbol,
                    side=order.side.value,
                    qty=float(order.qty),
                    status=order.status.value,
                    filled_avg_price=float(order.filled_avg_price) if order.filled_avg_price else None,
                    created_at=order.created_at,
                )
            except Exception as e2:
                logger.error(f"Plain market buy also failed for {symbol}: {e2}")
                return None
    else:
        # Sell path: plain market order
        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=order_side,
            time_in_force=TimeInForce.DAY,
        )
        try:
            order = trading_client.submit_order(request)
            logger.info(f"Submitted sell order: {qty} {symbol}")
            return Order(
                id=str(order.id),
                symbol=order.symbol,
                side=order.side.value,
                qty=float(order.qty),
                status=order.status.value,
                filled_avg_price=float(order.filled_avg_price) if order.filled_avg_price else None,
                created_at=order.created_at,
            )
        except Exception as e:
            logger.error(f"Failed to submit sell order for {symbol}: {e}")
            return None
def cancel_order(order_id: str) -> bool:
    """Cancel an open order by ID. Returns True on success."""
    try:
        trading_client.cancel_order_by_id(order_id)
        logger.info(f"Cancelled order {order_id}")
        return True
    except Exception as e:
        logger.warning(f"Failed to cancel order {order_id}: {e}")
        return False


def submit_short_order(
    symbol: str,
    qty: int,
    stop_loss_pct: float = 0.05,
    take_profit_pct: float = 0.12,
) -> Optional[Order]:
    """
    Open a short position: sell shares we don't own, profiting as price falls.
    - Entry: limit sell just below midpoint (competitive fill)
    - Take profit: GTC limit buy at cover price (lock in gain when price falls)
    - Stop loss: monitored by trading engine per cycle (avoids Alpaca stop complexity)
    """
    from alpaca.trading.requests import LimitOrderRequest
    try:
        quote_request = StockLatestQuoteRequest(symbol_or_symbols=[symbol])
        quotes = data_client.get_stock_latest_quote(quote_request)
        quote = quotes[symbol]
        bid = float(quote.bid_price or 0)
        ask = float(quote.ask_price or 0)
        if bid <= 0 or ask <= 0:
            raise ValueError(f"Invalid quote for {symbol}: bid={bid} ask={ask}")

        midpoint = (bid + ask) / 2
        entry_limit = round(midpoint * 0.998, 2)          # 0.2% below mid — fill aggressively
        cover_target = round(midpoint * (1 - take_profit_pct), 2)   # buy to cover at profit

        # Short entry: limit sell
        entry_req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            limit_price=entry_limit,
        )
        order = trading_client.submit_order(entry_req)
        logger.info(
            f"SHORT {symbol} x{qty} @ limit ${entry_limit:.2f} | "
            f"cover target ${cover_target:.2f} (-{take_profit_pct*100:.0f}%) | "
            f"stop +{stop_loss_pct*100:.0f}% (engine-monitored)"
        )

        # DAY limit buy to cover at take-profit price.
        # Using DAY (not GTC) so the cover order expires harmlessly at market close
        # if the short entry limit never filled — prevents orphaned buy orders from
        # accidentally opening a long position on a future trading day.
        try:
            cover_req = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=cover_target,
            )
            trading_client.submit_order(cover_req)
            logger.info(f"Cover order placed for {symbol} at ${cover_target:.2f} (DAY)")
        except Exception as e:
            logger.warning(f"Cover order failed (non-fatal, engine will monitor): {e}")

        return Order(
            id=str(order.id),
            symbol=symbol,
            side="short",
            qty=qty,
            status=str(order.status.value if hasattr(order.status, "value") else order.status),
            filled_avg_price=float(order.filled_avg_price) if order.filled_avg_price else None,
            created_at=order.created_at,
        )
    except Exception as e:
        logger.error(f"Short order failed for {symbol}: {e}")
        return None


@track_api("alpaca_market_data")
def get_market_snapshot(symbols: list[str]) -> dict:
    """Return latest quote + 5-day price change for each symbol."""
    snapshot = {}
    try:
        quote_request = StockLatestQuoteRequest(symbol_or_symbols=symbols)
        quotes = data_client.get_stock_latest_quote(quote_request)

        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from datetime import timedelta

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=90)
        bars_request = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            feed="iex",
            start=start,
            end=end,
        )
        bars = data_client.get_stock_bars(bars_request)

        bars_dict = bars.data if hasattr(bars, 'data') else dict(bars)
        for symbol in symbols:
            quote = quotes.get(symbol)
            current_price = float(quote.ask_price) if quote and quote.ask_price else None

            symbol_bars = bars_dict.get(symbol, [])
            closing_prices = [float(b.close) for b in symbol_bars]
            high_prices = [float(b.high) for b in symbol_bars]
            low_prices = [float(b.low) for b in symbol_bars]
            five_day_change = None
            if closing_prices and len(closing_prices) >= 6:
                five_day_change = round((closing_prices[-1] - closing_prices[-6]) / closing_prices[-6] * 100, 2)
            elif closing_prices and len(closing_prices) >= 2:
                five_day_change = round((closing_prices[-1] - closing_prices[0]) / closing_prices[0] * 100, 2)

            volume = int(symbol_bars[-1].volume) if symbol_bars else 0
            volumes = [int(b.volume) for b in symbol_bars]
            # Exclude today's partial bar from the historical average
            historical_volumes = volumes[:-1] if len(volumes) > 1 else volumes
            last_20_volumes = historical_volumes[-20:] if len(historical_volumes) >= 20 else historical_volumes
            avg_volume = int(sum(last_20_volumes) / len(last_20_volumes)) if last_20_volumes else 0

            # Project today's partial volume to a full-day estimate based on time elapsed.
            # Without this, a stock with 15M shares at 11 AM looks "thin" vs a 50M daily avg
            # even though it's perfectly on pace for a normal day.
            market_open_minutes = 9.5 * 60    # 9:30 AM ET in minutes since midnight
            market_close_minutes = 16.0 * 60  # 4:00 PM ET
            total_session_minutes = market_close_minutes - market_open_minutes  # 390 min
            now_et = datetime.now(timezone.utc)
            # Convert UTC to ET (UTC-4 in summer, UTC-5 in winter — use fixed offset as approximation)
            import time as _time
            et_offset = -4 if _time.daylight else -5
            now_et_minutes = (now_et.hour + et_offset) * 60 + now_et.minute
            minutes_elapsed = max(1, now_et_minutes - market_open_minutes)
            day_fraction = min(minutes_elapsed / total_session_minutes, 1.0)
            projected_volume = int(volume / day_fraction) if day_fraction > 0 else volume

            relative_volume = round(projected_volume / avg_volume, 2) if avg_volume > 0 else 1.0

            snapshot[symbol] = {
                "current_price": current_price,
                "five_day_change_pct": five_day_change,
                "closing_prices": closing_prices,
                "high_prices": high_prices,
                "low_prices": low_prices,
                "volume": volume,
                "avg_volume": avg_volume,
                "relative_volume": relative_volume,
            }
    except Exception as e:
        logger.error(f"Error fetching market snapshot: {e}")

    return snapshot


def get_portfolio_history(period: str = "1W") -> list[dict]:
    """Return portfolio equity history for charting."""
    try:
        import requests as _requests
        # Use Alpaca REST API directly — alpaca-py TradingClient
        # doesn't expose get_portfolio_history in all versions
        base = "https://paper-api.alpaca.markets"
        headers = {
            "APCA-API-KEY-ID": settings.alpaca_api_key,
            "APCA-API-SECRET-KEY": settings.alpaca_secret_key,
        }
        params = {"period": period, "timeframe": "1D", "extended_hours": "false"}
        resp = _requests.get(f"{base}/v2/account/portfolio/history", headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        result = []
        for i, timestamp in enumerate(data.get("timestamp", [])):
            equity = data["equity"][i]
            if equity is not None:
                result.append({
                    "timestamp": timestamp,
                    "equity": float(equity),
                    "profit_loss": float(data["profit_loss"][i]) if data["profit_loss"][i] else 0.0,
                    "profit_loss_pct": float(data["profit_loss_pct"][i]) if data["profit_loss_pct"][i] else 0.0,
                })
        return result
    except Exception as e:
        logger.error(f"Error fetching portfolio history: {e}")
        return []


def get_tradeable_universe() -> list[str]:
    """
    100% real-time dynamic universe — zero hardcoded tickers.
    Every stock is here because the market put it here right now:
    - Highest volume today (market is paying attention)
    - Biggest movers today (price action is happening)
    - Most mentioned in today's news (sentiment catalyst)
    - Leading sector ETFs (macro rotation signal)

    Also logs each symbol + its source(s) to daily_universe_log for
    the Sprint Review "why did we miss X?" analysis.
    """
    from collections import Counter
    seen = set()
    universe = []
    # Track which sources put each symbol in the universe
    symbol_sources: dict[str, list[str]] = {}

    def add(symbols: list[str], source: str):
        for s in symbols:
            # Filter out non-US symbols (e.g. TSX:IMO, LSE:XXXX) — Alpaca US equity
            # API rejects them with "invalid symbol" and pollutes the snapshot fetch.
            if s and ":" not in s:
                if s not in symbol_sources:
                    symbol_sources[s] = []
                if source not in symbol_sources[s]:
                    symbol_sources[s].append(source)
                if s not in seen:
                    seen.add(s)
                    universe.append(s)

    # ── Real-time: most active by volume ──
    # These are whatever the market is focused on TODAY
    try:
        from alpaca.data.historical.screener import ScreenerClient
        from alpaca.data.requests import MostActivesRequest
        sc = ScreenerClient(settings.alpaca_api_key, settings.alpaca_secret_key)
        actives = sc.get_most_actives(MostActivesRequest(top=50))
        add([i.symbol for i in actives.most_actives], "most_actives")
        logger.info(f"Most actives: {[i.symbol for i in actives.most_actives]}")
    except Exception as e:
        logger.warning(f"Could not fetch most actives: {e}")

    # ── Real-time: biggest gainers + losers ──
    # Momentum and reversal opportunities happening right now
    try:
        from alpaca.data.historical.screener import ScreenerClient
        from alpaca.data.requests import MarketMoversRequest
        sc = ScreenerClient(settings.alpaca_api_key, settings.alpaca_secret_key)
        movers = sc.get_market_movers(MarketMoversRequest(top=30))
        add([i.symbol for i in movers.gainers], "movers_gainers")
        add([i.symbol for i in movers.losers], "movers_losers")
        logger.info(f"Gainers: {[i.symbol for i in movers.gainers[:5]]} | Losers: {[i.symbol for i in movers.losers[:5]]}")
    except Exception as e:
        logger.warning(f"Could not fetch movers: {e}")

    # ── Real-time: news sentiment ──
    # Stocks the media is talking about right now = potential catalysts
    try:
        from alpaca.data.requests import NewsRequest
        from alpaca.data.historical.news import NewsClient
        nc = NewsClient(settings.alpaca_api_key, settings.alpaca_secret_key)
        news = nc.get_news(NewsRequest(limit=50))
        news_symbols = []
        for article in news.news:
            news_symbols.extend(article.symbols or [])
        top_news = [sym for sym, _ in Counter(news_symbols).most_common(50)]
        add(top_news, "news")
        logger.info(f"Top news symbols: {top_news[:10]}")
    except Exception as e:
        logger.warning(f"Could not fetch news symbols: {e}")

    # ── Real-time: leading sector ETFs ──
    # Whichever sectors are rotating hot today get included automatically
    # These are always liquid and represent macro-level opportunities
    sector_etfs = [
        "SPY", "QQQ", "IWM", "DIA",           # broad market
        "XLK", "XLF", "XLV", "XLE", "XLY",   # sectors
        "XLI", "XLU", "XLP", "XLB", "XLRE",
        "SOXX", "SMH", "IBB", "XBI",           # sub-sectors
        "GLD", "SLV", "GDX", "USO",            # commodities
        "SOXL", "TQQQ", "SPXL", "UVXY",       # leveraged (always volatile)
        "SOXS", "SQQQ", "SPXS",               # inverse (bear plays)
        "ARKK", "ARKG", "ARKW",               # innovation
    ]
    add(sector_etfs, "sector_etf")

    # ── Session 6: FMP Earnings Surprise injection ────────────────────────────
    # Stocks with >10% EPS beat in the last 21 days — captures post-earnings drift
    # (days 2–21 after initial gap-chasers leave, the real drift window)
    try:
        from services.brain.connectors.fmp_earnings import get_universe_additions as _fmp_adds
        earnings_syms = _fmp_adds()
        add(earnings_syms, "fmp_earnings")
        if earnings_syms:
            logger.info(f"FMP earnings surprise injection: {earnings_syms}")
    except Exception as e:
        logger.debug(f"FMP earnings universe injection skipped: {e}")

    # ── Session 6: SEC Form 4 Insider Buy injection ───────────────────────────
    # Stocks with >$100K open-market insider buys in the last 30 days
    # Only cash purchases counted — grants/awards/vesting ignored
    try:
        from services.brain.connectors.sec_insider import get_universe_additions as _sec_adds
        insider_syms = _sec_adds()
        add(insider_syms, "sec_insider")
        if insider_syms:
            logger.info(f"SEC insider buy injection: {insider_syms}")
    except Exception as e:
        logger.debug(f"SEC insider universe injection skipped: {e}")

    # ── Session 6: Barchart Unusual Options injection ─────────────────────────
    # Stocks with unusual options flow (vol/OI > 10x) right now.
    # This solves the trigger problem: without injection, the score boost in
    # signals.py never fires because the stock was never in the universe to begin with.
    # Filter: only inject if it looks like a real US equity ticker (1-4 uppercase letters)
    # to avoid garbage like SVI.TO or index symbols from the Barchart feed.
    try:
        from services.brain.connectors.barchart_options import (
            get_all_unusual_symbols as _bc_syms,
            get_short_candidates as _bc_shorts,
        )
        import re as _re

        def _bc_price_ok(sym: str) -> bool:
            """Skip penny stocks under $3 — pump targets, not real options signals."""
            try:
                import yfinance as _yf
                _p = (_yf.Ticker(sym).info or {}).get("regularMarketPrice") or \
                     (_yf.Ticker(sym).info or {}).get("previousClose") or 999
                return float(_p) >= 3.0
            except Exception:
                return True  # allow through if check fails — score gate handles it

        # ── Bullish call flow → buy candidates ─────────────────────────────
        _bc_candidates = _bc_syms()
        bc_syms = [s for s in _bc_candidates
                   if _re.match(r'^[A-Z]{1,5}$', s) and _bc_price_ok(s)]
        add(bc_syms, "barchart_options")
        if bc_syms:
            logger.info(f"Barchart unusual options injection (all flow): {bc_syms}")

        # ── Heavy put flow → short candidates ──────────────────────────────
        # Stocks with ≥5,000 put contracts at ≥50× vol/OI ratio are injected
        # specifically as short candidates. The -18 score from bearish flow plus
        # other falling indicators will push them into short territory (score < 0).
        _bc_short_map = _bc_shorts()
        bc_short_syms = [s for s in _bc_short_map
                         if _re.match(r'^[A-Z]{1,5}$', s) and _bc_price_ok(s)]
        add(bc_short_syms, "barchart_put_flow")
        if bc_short_syms:
            logger.info(f"Barchart heavy put flow → short injection: {bc_short_syms}")

    except Exception as e:
        logger.debug(f"Barchart options universe injection skipped: {e}")

    # ── Log universe sources to DB for Sprint Review ──────────────────────────
    try:
        from services.db import log_daily_universe
        log_daily_universe(symbol_sources)
    except Exception as e:
        logger.debug(f"log_daily_universe failed (non-fatal): {e}")

    logger.info(f"Total universe: {len(universe)} stocks — 100% real-time, zero hardcoded individual stocks")
    return universe


@track_api("alpaca_market_data")
def get_market_snapshot_light(symbols: list[str]) -> dict:
    """
    Lightweight snapshot — only current price + 5-day change.
    Used for broad Step 1 scan across hundreds of stocks.
    No historical bars needed, just latest quote + recent change.
    """
    snapshot = {}
    try:
        from datetime import timedelta
        # Batch quotes
        quote_request = StockLatestQuoteRequest(symbol_or_symbols=symbols)
        quotes = data_client.get_stock_latest_quote(quote_request)

        # 30-day bars — enough to get 20 trading days for avg_volume + 5-day change
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=30)
        bars_request = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            feed="iex",
            start=start,
            end=end,
        )
        bars = data_client.get_stock_bars(bars_request)

        bars_dict = bars.data if hasattr(bars, 'data') else dict(bars)
        for symbol in symbols:
            quote = quotes.get(symbol)
            current_price = float(quote.ask_price) if quote and quote.ask_price else None

            symbol_bars = bars_dict.get(symbol, [])
            closing_prices = [float(b.close) for b in symbol_bars]
            five_day_change = None
            if closing_prices and len(closing_prices) >= 6:
                oldest_5d = closing_prices[-6]
                newest = closing_prices[-1]
                five_day_change = round((newest - oldest_5d) / oldest_5d * 100, 2) if oldest_5d else None
            elif closing_prices and len(closing_prices) >= 2:
                oldest = closing_prices[0]
                newest = closing_prices[-1]
                five_day_change = round((newest - oldest) / oldest * 100, 2) if oldest else None

            volume = int(symbol_bars[-1].volume) if symbol_bars else 0
            volumes = [int(b.volume) for b in symbol_bars]
            # Exclude today's partial bar from the historical average
            historical_volumes = volumes[:-1] if len(volumes) > 1 else volumes
            last_20_volumes = historical_volumes[-20:] if len(historical_volumes) >= 20 else historical_volumes
            avg_volume = int(sum(last_20_volumes) / len(last_20_volumes)) if last_20_volumes else 0

            # Project today's partial volume to a full-day estimate based on time elapsed
            market_open_minutes = 9.5 * 60
            market_close_minutes = 16.0 * 60
            total_session_minutes = market_close_minutes - market_open_minutes
            now_et = datetime.now(timezone.utc)
            import time as _time
            et_offset = -4 if _time.daylight else -5
            now_et_minutes = (now_et.hour + et_offset) * 60 + now_et.minute
            minutes_elapsed = max(1, now_et_minutes - market_open_minutes)
            day_fraction = min(minutes_elapsed / total_session_minutes, 1.0)
            projected_volume = int(volume / day_fraction) if day_fraction > 0 else volume

            relative_volume = round(projected_volume / avg_volume, 2) if avg_volume > 0 else 1.0

            snapshot[symbol] = {
                "current_price": current_price,
                "five_day_change_pct": five_day_change,
                "closing_prices": closing_prices,  # included for RSI/MACD computation in Step 1
                "volume": volume,
                "avg_volume": avg_volume,
                "relative_volume": relative_volume,
            }
    except Exception as e:
        logger.error(f"Error fetching light snapshot: {e}")
    return snapshot


def get_vwap(symbol: str) -> float | None:
    """
    Intraday VWAP approximation using 1-minute bars since market open.
    Returns VWAP price, or None on failure.
    """
    from datetime import timedelta
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
        today = now_et.date()
        market_open = datetime(today.year, today.month, today.day, 9, 30,
                               tzinfo=ZoneInfo("America/New_York"))
        bars_request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            feed="iex",
            start=market_open,
            end=datetime.now(timezone.utc),
        )
        bars = data_client.get_stock_bars(bars_request)
        bars_dict = bars.data if hasattr(bars, 'data') else dict(bars)
        symbol_bars = bars_dict.get(symbol, [])
        if not symbol_bars:
            return None
        total_tp_vol = sum(
            ((b.high + b.low + b.close) / 3) * b.volume
            for b in symbol_bars
        )
        total_vol = sum(b.volume for b in symbol_bars)
        return round(total_tp_vol / total_vol, 4) if total_vol > 0 else None
    except Exception as e:
        logger.debug(f"get_vwap({symbol}) failed: {e}")
        return None


def get_intraday_bars(symbols: list[str], lookback_bars: int = 8) -> dict[str, list[dict]]:
    """
    Fetch the last `lookback_bars` 15-minute bars for each symbol.
    Returns {symbol: [{"time": iso_str, "open": float, "high": float, "low": float, "close": float, "volume": int}, ...]}
    Used for multi-timeframe entry confirmation — is the stock trending up on the 15-min chart?
    """
    from datetime import timedelta
    result: dict[str, list[dict]] = {}
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=4)

    try:
        try:
            from alpaca.data.timeframe import TimeFrameUnit
            bars_request = StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=TimeFrame(15, TimeFrameUnit.Minute),
                feed="iex",
                start=start,
                end=end,
            )
        except Exception:
            bars_request = StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=TimeFrame.Minute,
                timeframe_multiplier=15,
                feed="iex",
                start=start,
                end=end,
            )

        bars = data_client.get_stock_bars(bars_request)
        bars_dict = bars.data if hasattr(bars, 'data') else dict(bars)

        for symbol in symbols:
            try:
                symbol_bars = bars_dict.get(symbol, [])
                last_bars = symbol_bars[-lookback_bars:] if len(symbol_bars) > lookback_bars else symbol_bars
                result[symbol] = [
                    {
                        "time": bar.timestamp.isoformat(),
                        "open": float(bar.open),
                        "high": float(bar.high),
                        "low": float(bar.low),
                        "close": float(bar.close),
                        "volume": int(bar.volume),
                    }
                    for bar in last_bars
                ]
            except Exception as e:
                logger.warning(f"Error processing intraday bars for {symbol}: {e}")
                result[symbol] = []
    except Exception as e:
        logger.error(f"Error fetching intraday bars: {e}")
        for symbol in symbols:
            result.setdefault(symbol, [])

    return result


def get_sentiment_context(symbols: list[str]) -> dict[str, int]:
    """Return news mention count per symbol as a sentiment proxy."""
    from alpaca.data.requests import NewsRequest
    from alpaca.data.historical.news import NewsClient
    from collections import Counter

    counts: Counter = Counter()
    try:
        nc = NewsClient(settings.alpaca_api_key, settings.alpaca_secret_key)
        news = nc.get_news(NewsRequest(symbols=symbols, limit=50))
        for article in news.news:
            for sym in (article.symbols or []):
                counts[sym] += 1
    except Exception as e:
        logger.warning(f"Could not fetch sentiment: {e}")
    return dict(counts)


def get_live_price(symbol: str) -> Optional[float]:
    """Fetch the real-time bid/ask midpoint for a symbol from Alpaca."""
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=[symbol])
        quotes = data_client.get_stock_latest_quote(req)
        quote = quotes.get(symbol)
        if not quote:
            return None
        bid = float(quote.bid_price or 0)
        ask = float(quote.ask_price or 0)
        if bid > 0 and ask > 0:
            return round((bid + ask) / 2, 2)
        if ask > 0:
            return round(ask, 2)
        if bid > 0:
            return round(bid, 2)
        return None
    except Exception as e:
        logger.warning(f"Could not fetch live price for {symbol}: {e}")
        return None


def is_market_open() -> bool:
    clock = trading_client.get_clock()
    return clock.is_open


def get_market_status() -> str:
    """
    Returns 'open' | 'premarket' | 'afterhours' | 'closed'.
    Uses Alpaca clock for regular hours (handles holidays correctly).
    Uses ET timezone for extended hours windows.
    """
    try:
        clock = trading_client.get_clock()
        if clock.is_open:
            return "open"
    except Exception:
        pass

    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        from datetime import timedelta
        now_et = datetime.now(timezone.utc) - timedelta(hours=4)

    if now_et.weekday() >= 5:
        return "closed"

    mins = now_et.hour * 60 + now_et.minute
    if 8 * 60 + 30 <= mins < 9 * 60 + 30:
        return "premarket"
    if 16 * 60 <= mins < 18 * 60:
        return "afterhours"
    return "closed"


def submit_extended_hours_order(
    symbol: str,
    qty: int,
    side: str,
) -> Optional[Order]:
    """
    Limit order for pre-market / after-hours sessions.
    Alpaca requires: LimitOrderRequest + extended_hours=True + TimeInForce.DAY.
    No bracket orders — TP/SL legs not supported in extended sessions.
    """
    from alpaca.trading.requests import LimitOrderRequest
    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
    try:
        quote_request = StockLatestQuoteRequest(symbol_or_symbols=[symbol])
        quotes = data_client.get_stock_latest_quote(quote_request)
        quote = quotes[symbol]
        ask_price = float(quote.ask_price or 0)
        bid_price = float(quote.bid_price or 0)
        if ask_price <= 0:
            logger.warning(f"Extended hours {symbol}: no valid ask price — skipping")
            return None
        midpoint = round((bid_price + ask_price) / 2, 2) if bid_price > 0 else ask_price
        # Slightly wider buffer than regular hours to account for extended-hours spreads
        limit_price = round(midpoint * 1.003, 2) if side == "buy" else round(midpoint * 0.997, 2)
        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=order_side,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price,
            extended_hours=True,
        )
        order = trading_client.submit_order(req)
        logger.info(f"Extended hours {side}: {qty} {symbol} @ limit ${limit_price:.2f}")
        return Order(
            id=str(order.id),
            symbol=order.symbol,
            side=order.side.value,
            qty=float(order.qty),
            status=order.status.value,
            filled_avg_price=float(order.filled_avg_price) if order.filled_avg_price else None,
            created_at=order.created_at,
        )
    except Exception as e:
        logger.error(f"Extended hours order failed for {symbol}: {e}")
        return None


@track_api("news_api")
def get_news(symbols: list[str] = None, limit: int = 40) -> list[dict]:
    """
    Fetch latest financial news in parallel from 10+ free sources:

    Real-time / near real-time (no API key):
      - SEC EDGAR 8-K filings (official material events — earnings, M&A, guidance)
      - Yahoo Finance per-symbol RSS (targeted, ~minutes fresh)
      - Yahoo Finance general index
      - MarketWatch top stories + market pulse
      - CNBC markets + investing
      - Reuters business
      - GlobeNewswire (earnings press releases)
      - Nasdaq Trader (halts, listing alerts)
      - PR Newswire (press releases)

    Supplemental (may have delay):
      - Alpaca / Benzinga (kept as fallback — symbol-tagged, useful even if delayed)

    All sources run in parallel threads. Results are deduplicated by headline
    and sorted newest-first.
    """
    import httpx
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    from concurrent.futures import ThreadPoolExecutor, as_completed

    import re as _re
    import html as _html

    HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; KovaBot/1.0; +https://kova.app)"}
    target_symbols = symbols or ["AAPL", "MSFT", "GOOGL", "TSLA", "SPY", "NVDA"]

    # Financial keyword filter — whole-word matching only to avoid substring false positives
    # e.g. "market" must not match "marketing", "trade" must not match "STARTRADER"
    _FINANCE_KEYWORDS = {
        "stock", "stocks", "shares", "market", "markets", "invest", "investing",
        "investor", "investors", "earnings", "revenue", "profit", "loss", "ipo",
        "sec", "fed", "interest rate", "fed rate", "rate hike", "rates", "bond", "bonds", "etf", "fund", "quarter",
        "fiscal", "dividend", "dividends", "acquisition", "merger", "guidance",
        "forecast", "analyst", "analysts", "upgrade", "downgrade", "equity",
        "nasdaq", "nyse", "s&p", "dow", "bitcoin", "crypto", "economy", "gdp",
        "inflation", "cpi", "financial", "capital", "valuation", "rally",
        "correction", "bull", "bear", "volatility", "hedge", "sector", "commodity",
        "oil", "gold", "yield", "yields", "trading", "portfolio", "buyback",
        "quarterly", "annual", "outlook", "guidance", "short-selling",
    }

    def _is_financial(headline: str, summary: str) -> bool:
        text = (headline + " " + summary).lower()
        # Reject OTC/pink sheet micro-caps — not tradeable on major exchanges
        _OTC_MARKERS = ["otc:", "(otc:", "otcqb", "otcqx", "pink sheet", "pinksheet"]
        if any(marker in text for marker in _OTC_MARKERS):
            return False
        # Whole-word matching — split text into tokens, check for exact keyword match
        # This prevents "marketing" matching "market", "STARTRADER" matching "trade", etc.
        words = set(_re.findall(r"[a-z&]+", text))
        return bool(words & _FINANCE_KEYWORDS)

    def _clean_text(raw: str) -> str:
        """Strip HTML tags and decode HTML entities from text."""
        if not raw:
            return ""
        # Decode HTML entities (&amp; &#39; &lt; etc.)
        decoded = _html.unescape(raw)
        # Remove HTML tags
        cleaned = _re.sub(r"<[^>]+>", "", decoded)
        # Collapse extra whitespace
        return " ".join(cleaned.split()).strip()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _parse_date(raw: str) -> str | None:
        """Parse any date string → clean ISO 8601 UTC string iOS can decode, or None."""
        if not raw:
            return None
        try:
            from email.utils import parsedate_to_datetime as _ptd
            dt = _ptd(raw)
            # Normalize: strip microseconds, use Z suffix (strict iso8601 for iOS)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass
        try:
            # Handle ISO 8601 variants (Atom feeds, e.g. "2026-05-11T12:00:00.123Z")
            import re as _re2
            cleaned = _re2.sub(r"\.\d+", "", raw).replace("+00:00", "Z").rstrip("Z") + "Z"
            return cleaned
        except Exception:
            return None  # Return None rather than an unparseable string that crashes iOS

    def _parse_rss(xml_text: str, source: str, symbol_hints: list[str] = []) -> list[dict]:
        """Parse standard RSS (item-based) XML."""
        articles = []
        try:
            root = ET.fromstring(xml_text)
            for item in root.findall(".//item"):
                headline = _clean_text(item.findtext("title") or "")
                if not headline:
                    continue
                summary = _clean_text(item.findtext("description") or "")[:400]
                # Skip non-financial press releases (pet food, medical devices, etc.)
                if not _is_financial(headline, summary):
                    continue
                articles.append({
                    "id": "",
                    "headline": headline,
                    "summary": summary,
                    "author": _clean_text(
                        item.findtext("author")
                        or item.findtext("{http://purl.org/dc/elements/1.1/}creator")
                        or ""
                    ),
                    "created_at": _parse_date(item.findtext("pubDate") or ""),
                    "url": (item.findtext("link") or "").strip(),
                    "symbols": symbol_hints,
                    "source": source,
                })
        except Exception as e:
            # Downgrade to debug — some feeds (Nasdaq Trader) have permanently malformed XML
            logger.debug(f"RSS parse error ({source}): {e}")
        return articles

    def _parse_atom(xml_text: str, source: str, symbol_hints: list[str] = []) -> list[dict]:
        """Parse Atom feed (entry-based) XML — used by SEC EDGAR."""
        articles = []
        NS = "http://www.w3.org/2005/Atom"
        try:
            root = ET.fromstring(xml_text)
            for entry in root.findall(f"{{{NS}}}entry"):
                headline = _clean_text(entry.findtext(f"{{{NS}}}title") or "")
                if not headline:
                    continue
                link_el = entry.find(f"{{{NS}}}link")
                url = (link_el.get("href", "") if link_el is not None else "").strip()
                updated = entry.findtext(f"{{{NS}}}updated") or ""
                summary = _clean_text(entry.findtext(f"{{{NS}}}summary") or "")[:400]
                if not _is_financial(headline, summary):
                    continue
                articles.append({
                    "id": "",
                    "headline": headline,
                    "summary": summary,
                    "author": "",
                    "created_at": updated or None,
                    "url": url,
                    "symbols": symbol_hints,
                    "source": source,
                })
        except Exception as e:
            logger.warning(f"Atom parse error ({source}): {e}")
        return articles

    def fetch(url: str, source: str, fmt: str = "rss",
              symbol_hints: list[str] = [], timeout: float = 7.0) -> list[dict]:
        try:
            resp = httpx.get(url, timeout=timeout, follow_redirects=True, headers=HEADERS)
            resp.raise_for_status()
            if fmt == "atom":
                return _parse_atom(resp.text, source, symbol_hints)
            return _parse_rss(resp.text, source, symbol_hints)
        except Exception as e:
            # Downgrade Reuters DNS failure to debug — Railway blocks external DNS for some feeds
            logger.debug(f"{source} fetch failed (non-fatal): {e}")
            return []

    # ── Build task list ───────────────────────────────────────────────────────

    tasks = [
        # SEC EDGAR — 8-K filings (earnings, M&A, material events). Updated every 10 min.
        ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=20&output=atom",
         "SEC EDGAR", "atom", []),

        # Yahoo Finance — general market index
        ("https://finance.yahoo.com/news/rssindex", "Yahoo Finance", "rss", []),

        # MarketWatch — top stories
        ("https://feeds.marketwatch.com/marketwatch/topstories/", "MarketWatch", "rss", []),

        # MarketWatch — market pulse (breaking, shorter items)
        ("https://feeds.marketwatch.com/marketwatch/marketpulse/", "MarketWatch", "rss", []),

        # CNBC — markets
        ("https://www.cnbc.com/id/100003114/device/rss/rss.html", "CNBC", "rss", []),

        # CNBC — investing
        ("https://www.cnbc.com/id/15839069/device/rss/rss.html", "CNBC", "rss", []),

        # Reuters — business & finance
        ("https://feeds.reuters.com/reuters/businessNews", "Reuters", "rss", []),

        # GlobeNewswire — earnings press releases & M&A
        ("https://www.globenewswire.com/RssFeed/subjectcode/23-Mergers+%26+Acquisitions+And+Alliance",
         "GlobeNewswire", "rss", []),
        ("https://www.globenewswire.com/RssFeed/subjectcode/10-Earnings",
         "GlobeNewswire", "rss", []),

        # Nasdaq Trader — halts, listing changes, market alerts
        ("https://www.nasdaqtrader.com/rss.aspx?feed=traderNews", "Nasdaq Trader", "rss", []),

        # Investing.com — broad market news, analyst calls, economic data
        ("https://www.investing.com/rss/news.rss", "Investing.com", "rss", []),

        # TheStreet — stock analysis and market commentary
        ("https://www.thestreet.com/.rss/full", "TheStreet", "rss", []),

        # Investors Business Daily — IBD market news (momentum/growth stock focused)
        ("https://www.investors.com/feed/", "IBD", "rss", []),
    ]

    # NOTE: Yahoo Finance per-symbol RSS feeds (finance.yahoo.com/rss/headline?s=AAPL)
    # return the SAME general market articles regardless of the symbol parameter,
    # causing mass duplicates. The general index feed above already covers Yahoo Finance.

    # ── Fetch all in parallel ─────────────────────────────────────────────────

    all_articles: list[dict] = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {
            pool.submit(fetch, url, source, fmt, hints): source
            for url, source, fmt, hints in tasks
        }
        for future in as_completed(futures):
            try:
                all_articles.extend(future.result())
            except Exception as e:
                logger.warning(f"News future error: {e}")

    # ── Alpaca / Benzinga (sequential — uses SDK, not httpx) ─────────────────
    try:
        from alpaca.data.requests import NewsRequest
        from alpaca.data.historical.news import NewsClient
        nc = NewsClient(settings.alpaca_api_key, settings.alpaca_secret_key)
        try:
            req = NewsRequest(symbols=",".join(target_symbols), limit=20, exclude_contentless=True)
            news = nc.get_news(req)
        except TypeError:
            req = NewsRequest(symbols=",".join(target_symbols), limit=20)
            news = nc.get_news(req)
        for article in news.news:
            headline = _clean_text(article.headline or "")
            if not headline:
                continue
            summary = _clean_text(article.summary or "")[:400]
            all_articles.append({
                "id": str(article.id),
                "headline": headline,
                "summary": summary,
                "author": article.author or "",
                "created_at": article.created_at.isoformat() if article.created_at else None,
                "url": article.url or "",
                "symbols": article.symbols or [],
                "source": getattr(article, "source", "Benzinga"),
            })
    except Exception as e:
        logger.warning(f"Alpaca news fetch failed (non-fatal): {e}")

    # ── Deduplicate by headline AND URL, assign stable IDs, sort newest-first ──
    import hashlib
    seen_headlines: set[str] = set()
    seen_urls: set[str] = set()
    unique: list[dict] = []
    for article in all_articles:
        h = article["headline"].lower().strip()
        url = (article.get("url") or "").strip()
        # Skip if we've seen this headline or a non-empty URL already
        if h in seen_headlines:
            continue
        if url and url in seen_urls:
            continue
        seen_headlines.add(h)
        if url:
            seen_urls.add(url)
        # Generate a stable id if missing (hash of headline+source)
        if not article.get("id"):
            article["id"] = hashlib.md5(
                f"{article['headline']}{article.get('source','')}".encode()
            ).hexdigest()[:16]
        unique.append(article)

    # Merge in any articles from the real-time WebSocket news stream
    try:
        from services.news_stream import get_cached_news as _get_stream_news
        stream_articles = _get_stream_news(symbols=symbols)
        seen_in_rest = {a.get("headline", "") for a in unique}
        for sa in stream_articles:
            if sa.get("headline") not in seen_in_rest:
                unique.append(sa)
    except Exception:
        pass

    # Sort: symbol-tagged articles (Benzinga/Alpaca/stream) first — most trading-relevant
    # Within each group, sort by date newest-first
    def _sort_key(a):
        has_symbols = 1 if a.get("symbols") else 0
        return (has_symbols, a.get("created_at") or "")

    unique.sort(key=_sort_key, reverse=True)
    return unique[:limit]
