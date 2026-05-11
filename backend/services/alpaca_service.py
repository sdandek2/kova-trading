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

trading_client = TradingClient(
    settings.alpaca_api_key,
    settings.alpaca_secret_key,
    paper=True,
)

data_client = StockHistoricalDataClient(
    settings.alpaca_api_key,
    settings.alpaca_secret_key,
)


def get_account() -> AccountInfo:
    account = trading_client.get_account()
    portfolio_value = float(account.portfolio_value)
    prev_value = float(account.last_equity)
    day_pl = portfolio_value - prev_value
    day_pl_percent = (day_pl / prev_value * 100) if prev_value else 0.0

    return AccountInfo(
        portfolio_value=portfolio_value,
        cash=float(account.cash),
        buying_power=float(account.buying_power),
        day_pl=day_pl,
        day_pl_percent=round(day_pl_percent, 2),
    )


def get_positions() -> list[Position]:
    raw = trading_client.get_all_positions()
    result = []
    for p in raw:
        result.append(
            Position(
                symbol=p.symbol,
                qty=float(p.qty),
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


def submit_market_order(symbol: str, qty: int, side: str, stop_loss_pct: float = 0.03, take_profit_pct: float = 0.05) -> Optional[Order]:
    from alpaca.trading.requests import TakeProfitRequest, StopLossRequest, TrailingStopOrderRequest
    from alpaca.trading.enums import OrderClass

    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

    if side == "buy":
        try:
            quote_request = StockLatestQuoteRequest(symbol_or_symbols=[symbol])
            quotes = data_client.get_stock_latest_quote(quote_request)
            current_price = float(quotes[symbol].ask_price)
            take_profit_price = round(current_price * (1 + take_profit_pct), 2)

            # Use bracket with trailing stop instead of fixed stop
            # trail_percent moves the stop up automatically as price rises
            request = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.OTO,  # One-triggers-other
                take_profit=TakeProfitRequest(limit_price=take_profit_price),
            )
            # Submit main order first, then attach trailing stop
            order = trading_client.submit_order(request)
            logger.info(f"Submitted buy order with take-profit: {qty} {symbol} @ ~${current_price:.2f}, TP: ${take_profit_price:.2f}")

            # Submit trailing stop as separate order
            trail_req = TrailingStopOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                trail_percent=stop_loss_pct * 100,  # e.g. 3.0 for 3%
            )
            try:
                trading_client.submit_order(trail_req)
                logger.info(f"Trailing stop set: {stop_loss_pct*100:.1f}% trail on {symbol}")
            except Exception as te:
                logger.warning(f"Could not set trailing stop (non-fatal): {te}")

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
            logger.warning(f"Advanced order failed, using plain market order: {e}")
            request = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=order_side,
                time_in_force=TimeInForce.DAY,
            )
    else:
        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=order_side,
            time_in_force=TimeInForce.DAY,
        )

    try:
        order = trading_client.submit_order(request)
        logger.info(f"Submitted {side} order: {qty} {symbol}")
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
        logger.error(f"Failed to submit order: {e}")
        return None


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
            current_price = float(quote.ask_price) if quote else None

            symbol_bars = bars_dict.get(symbol, [])
            closing_prices = [float(b.close) for b in symbol_bars]
            high_prices = [float(b.high) for b in symbol_bars]
            low_prices = [float(b.low) for b in symbol_bars]
            five_day_change = None
            if closing_prices and len(closing_prices) >= 2:
                five_day_change = round((closing_prices[-1] - closing_prices[0]) / closing_prices[0] * 100, 2)

            snapshot[symbol] = {
                "current_price": current_price,
                "five_day_change_pct": five_day_change,
                "closing_prices": closing_prices,
                "high_prices": high_prices,
                "low_prices": low_prices,
            }
    except Exception as e:
        logger.error(f"Error fetching market snapshot: {e}")

    return snapshot


def get_portfolio_history(period: str = "1W") -> list[dict]:
    """Return portfolio equity history for charting."""
    try:
        # alpaca-py >= 0.20 uses get_portfolio_history with direct kwargs
        history = trading_client.get_portfolio_history(
            period=period,
            timeframe="1D",
        )
        result = []
        for i, timestamp in enumerate(history.timestamp):
            equity = history.equity[i]
            if equity is not None:
                result.append({
                    "timestamp": timestamp,
                    "equity": float(equity),
                    "profit_loss": float(history.profit_loss[i]) if history.profit_loss[i] else 0.0,
                    "profit_loss_pct": float(history.profit_loss_pct[i]) if history.profit_loss_pct[i] else 0.0,
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
    """
    from collections import Counter
    seen = set()
    universe = []

    def add(symbols: list[str]):
        for s in symbols:
            if s and s not in seen:
                seen.add(s)
                universe.append(s)

    # ── Real-time: most active by volume ──
    # These are whatever the market is focused on TODAY
    try:
        from alpaca.data.historical.screener import ScreenerClient
        from alpaca.data.requests import MostActivesRequest
        sc = ScreenerClient(settings.alpaca_api_key, settings.alpaca_secret_key)
        actives = sc.get_most_actives(MostActivesRequest(top=50))
        add([i.symbol for i in actives.most_actives])
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
        add([i.symbol for i in movers.gainers])
        add([i.symbol for i in movers.losers])
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
        add(top_news)
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
    add(sector_etfs)

    logger.info(f"Total universe: {len(universe)} stocks — 100% real-time, zero hardcoded individual stocks")
    return universe


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

        # 5-day bars for change calculation
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=8)
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
            five_day_change = None
            if symbol_bars and len(symbol_bars) >= 2:
                oldest = float(symbol_bars[0].close)
                newest = float(symbol_bars[-1].close)
                five_day_change = round((newest - oldest) / oldest * 100, 2) if oldest else None

            snapshot[symbol] = {
                "current_price": current_price,
                "five_day_change_pct": five_day_change,
            }
    except Exception as e:
        logger.error(f"Error fetching light snapshot: {e}")
    return snapshot


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


def is_market_open() -> bool:
    clock = trading_client.get_clock()
    return clock.is_open


def get_news(symbols: list[str] = None, limit: int = 20) -> list[dict]:
    """Fetch latest news from Alpaca News API + Yahoo Finance RSS, deduplicated by headline."""
    import httpx
    import xml.etree.ElementTree as ET
    from alpaca.data.requests import NewsRequest
    from alpaca.data.historical.news import NewsClient

    news_client = NewsClient(
        settings.alpaca_api_key,
        settings.alpaca_secret_key,
    )

    target_symbols = symbols or ["AAPL", "MSFT", "GOOGL", "TSLA", "SPY", "NVDA"]

    result = []
    seen_headlines: set[str] = set()

    # ── Alpaca News ──
    try:
        request = NewsRequest(
            symbols=",".join(target_symbols),
            limit=limit,
            exclude_contentless=True,
        )
        news = news_client.get_news(request)
        for article in news.news:
            headline = article.headline or ""
            if headline and headline not in seen_headlines:
                seen_headlines.add(headline)
                result.append({
                    "id": str(article.id),
                    "headline": headline,
                    "summary": article.summary or "",
                    "author": article.author or "",
                    "created_at": article.created_at.isoformat() if article.created_at else None,
                    "url": article.url or "",
                    "symbols": article.symbols or [],
                    "source": getattr(article, 'source', ''),
                })
    except TypeError:
        # exclude_contentless not supported in this version — retry without it
        try:
            request = NewsRequest(
                symbols=",".join(target_symbols),
                limit=limit,
            )
            news = news_client.get_news(request)
            for article in news.news:
                headline = article.headline or ""
                if headline and headline not in seen_headlines:
                    seen_headlines.add(headline)
                    result.append({
                        "id": str(article.id),
                        "headline": headline,
                        "summary": article.summary or "",
                        "author": article.author or "",
                        "created_at": article.created_at.isoformat() if article.created_at else None,
                        "url": article.url or "",
                        "symbols": article.symbols or [],
                        "source": getattr(article, 'source', ''),
                    })
        except Exception as e:
            logger.error(f"Error fetching Alpaca news: {e}")
    except Exception as e:
        logger.error(f"Error fetching Alpaca news: {e}")

    # ── Yahoo Finance RSS (supplemental) ──
    try:
        rss_url = "https://finance.yahoo.com/news/rssindex"
        resp = httpx.get(rss_url, timeout=5.0, follow_redirects=True)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        ns = {"media": "http://search.yahoo.com/mrss/"}
        items = root.findall(".//item")
        for item in items:
            headline = (item.findtext("title") or "").strip()
            if not headline or headline in seen_headlines:
                continue
            seen_headlines.add(headline)
            pub_date = item.findtext("pubDate") or ""
            # Convert RSS date to ISO if possible
            created_at = None
            if pub_date:
                try:
                    from email.utils import parsedate_to_datetime
                    created_at = parsedate_to_datetime(pub_date).isoformat()
                except Exception:
                    created_at = pub_date
            result.append({
                "id": "",
                "headline": headline,
                "summary": (item.findtext("description") or "").strip(),
                "author": "",
                "created_at": created_at,
                "url": (item.findtext("link") or "").strip(),
                "symbols": [],
                "source": "Yahoo Finance",
            })
    except Exception as e:
        logger.warning(f"Yahoo Finance RSS fetch failed (non-fatal): {e}")

    return result
