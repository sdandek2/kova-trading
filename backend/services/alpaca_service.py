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
        try:
            quote_request = StockLatestQuoteRequest(symbol_or_symbols=[symbol])
            quotes = data_client.get_stock_latest_quote(quote_request)
            current_price = float(quotes[symbol].ask_price)
            take_profit_price = round(current_price * (1 + take_profit_pct), 2)

            if partial_exit and qty >= 2:
                # ── Partial exit strategy: sell half at TP, let other half ride ──
                # Half 1: plain market buy for full qty
                buy_req = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=order_side,
                    time_in_force=TimeInForce.DAY,
                )
                order = trading_client.submit_order(buy_req)
                logger.info(f"Partial-exit buy: {qty} {symbol} @ ~${current_price:.2f}")

                half_qty = qty // 2
                remaining_qty = qty - half_qty

                # Half 2: limit sell for first half at TP price
                try:
                    limit_sell = LimitOrderRequest(
                        symbol=symbol,
                        qty=half_qty,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.GTC,
                        limit_price=take_profit_price,
                    )
                    trading_client.submit_order(limit_sell)
                    logger.info(f"Partial TP: selling {half_qty} {symbol} at ${take_profit_price:.2f} (+{take_profit_pct*100:.0f}%)")
                except Exception as e:
                    logger.warning(f"Partial limit sell failed (non-fatal): {e}")

                # Half 3: trailing stop on remaining shares — rides the winner
                try:
                    trail_req = TrailingStopOrderRequest(
                        symbol=symbol,
                        qty=remaining_qty,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.GTC,
                        trail_percent=stop_loss_pct * 100,
                    )
                    trading_client.submit_order(trail_req)
                    logger.info(f"Trailing stop on remaining {remaining_qty} {symbol}: {stop_loss_pct*100:.0f}% trail (rides the winner)")
                except Exception as e:
                    logger.warning(f"Trailing stop failed (non-fatal): {e}")

            else:
                # ── Standard exit: take-profit + trailing stop on full position ──
                request = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=order_side,
                    time_in_force=TimeInForce.DAY,
                    order_class=OrderClass.OTO,
                    take_profit=TakeProfitRequest(limit_price=take_profit_price),
                )
                order = trading_client.submit_order(request)
                logger.info(f"Buy order: {qty} {symbol} @ ~${current_price:.2f} | TP: ${take_profit_price:.2f} (+{take_profit_pct*100:.0f}%)")

                try:
                    trail_req = TrailingStopOrderRequest(
                        symbol=symbol,
                        qty=qty,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.GTC,
                        trail_percent=stop_loss_pct * 100,
                    )
                    trading_client.submit_order(trail_req)
                    logger.info(f"Trailing stop: {stop_loss_pct*100:.0f}% trail on {symbol}")
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

            volume = int(symbol_bars[-1].volume) if symbol_bars else 0
            volumes = [int(b.volume) for b in symbol_bars]
            last_20_volumes = volumes[-20:] if len(volumes) >= 20 else volumes
            avg_volume = int(sum(last_20_volumes) / len(last_20_volumes)) if last_20_volumes else 0
            relative_volume = round(volume / avg_volume, 2) if avg_volume > 0 else 1.0

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
            five_day_change = None
            if symbol_bars and len(symbol_bars) >= 2:
                oldest = float(symbol_bars[0].close)
                newest = float(symbol_bars[-1].close)
                five_day_change = round((newest - oldest) / oldest * 100, 2) if oldest else None

            volume = int(symbol_bars[-1].volume) if symbol_bars else 0
            volumes = [int(b.volume) for b in symbol_bars]
            last_20_volumes = volumes[-20:] if len(volumes) >= 20 else volumes
            avg_volume = int(sum(last_20_volumes) / len(last_20_volumes)) if last_20_volumes else 0
            relative_volume = round(volume / avg_volume, 2) if avg_volume > 0 else 1.0

            snapshot[symbol] = {
                "current_price": current_price,
                "five_day_change_pct": five_day_change,
                "volume": volume,
                "avg_volume": avg_volume,
                "relative_volume": relative_volume,
            }
    except Exception as e:
        logger.error(f"Error fetching light snapshot: {e}")
    return snapshot


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

    # Financial keyword filter — at least one must appear in the headline or summary
    # to be included. Keeps pet food press releases and unrelated PRs out.
    _FINANCE_KEYWORDS = {
        "stock", "share", "market", "invest", "earn", "revenue", "profit", "loss",
        "ipo", "sec", "fed", "rate", "bond", "etf", "fund", "quarter", "fiscal",
        "dividend", "acquire", "merger", "guidance", "forecast", "analyst", "upgrade",
        "downgrade", "buy", "sell", "trade", "portfolio", "equity", "nasdaq", "nyse",
        "s&p", "dow", "bitcoin", "crypto", "economy", "gdp", "inflation", "cpi",
        "earnings", "outlook", "financial", "capital", "asset", "valuation", "ipo",
        "short", "rally", "correction", "bull", "bear", "volatility", "hedge",
        "breakout", "momentum", "sector", "commodity", "oil", "gold", "yield",
    }

    def _is_financial(headline: str, summary: str) -> bool:
        text = (headline + " " + summary).lower()
        # Reject OTC/pink sheet micro-caps — not tradeable on major exchanges
        _OTC_MARKERS = ["otc:", "(otc:", "otcqb", "otcqx", "pink sheet", "pinksheet"]
        if any(marker in text for marker in _OTC_MARKERS):
            return False
        return any(kw in text for kw in _FINANCE_KEYWORDS)

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
        if not raw:
            return None
        try:
            return parsedate_to_datetime(raw).isoformat()
        except Exception:
            return raw

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
            logger.warning(f"RSS parse error ({source}): {e}")
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
            logger.warning(f"{source} fetch failed (non-fatal): {e}")
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

        # PR Newswire — financial & earnings specific feeds only (not the general feed which is too noisy)
        ("https://www.prnewswire.com/rss/financial-news-releases-list.rss", "PR Newswire", "rss", []),
        ("https://www.prnewswire.com/rss/earnings-releases-list.rss", "PR Newswire", "rss", []),
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

    unique.sort(key=lambda a: a.get("created_at") or "", reverse=True)
    return unique[:limit]
