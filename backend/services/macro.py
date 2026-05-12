import logging
from datetime import datetime, timezone, timedelta
from config import settings

logger = logging.getLogger(__name__)


def get_macro_context() -> dict:
    """
    Returns macro market signals:
    - SPY trend (market direction via MA20/MA50)
    - VIX proxy via UVXY ETF
    - Market regime: bull / bear / volatile / neutral
    """
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    client = StockHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_secret_key)

    context = {
        "spy_trend": "neutral",
        "vix_level": "normal",
        "market_regime": "neutral",
        "guidance": "Market conditions are neutral. Follow individual stock signals.",
    }

    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=90)

        bars = client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=["SPY", "UVXY"],
                timeframe=TimeFrame.Day,
            feed="iex",
                start=start,
                end=end,
            )
        )

        # Unpack BarSet — alpaca-py 0.20+ returns a BarSet object, not a dict
        bars_dict = bars.data if hasattr(bars, "data") else dict(bars)

        # SPY trend
        spy_bars = bars_dict.get("SPY", [])
        if len(spy_bars) >= 20:
            closes = [float(b.close) for b in spy_bars]
            ma20 = sum(closes[-20:]) / 20
            ma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else ma20
            price = closes[-1]

            if price > ma20 > ma50:
                context["spy_trend"] = "strong_uptrend"
            elif price > ma20:
                context["spy_trend"] = "uptrend"
            elif price < ma20 < ma50:
                context["spy_trend"] = "strong_downtrend"
            elif price < ma20:
                context["spy_trend"] = "downtrend"

        # VIX proxy via UVXY
        uvxy_bars = bars_dict.get("UVXY", [])
        if len(uvxy_bars) >= 10:
            u_closes = [float(b.close) for b in uvxy_bars]
            u_ma10 = sum(u_closes[-10:]) / 10
            u_price = u_closes[-1]

            if u_price > u_ma10 * 1.3:
                context["vix_level"] = "extreme_fear"
            elif u_price > u_ma10 * 1.1:
                context["vix_level"] = "elevated"
            elif u_price < u_ma10 * 0.9:
                context["vix_level"] = "low_fear"

        # Determine regime + guidance
        trend = context["spy_trend"]
        vix = context["vix_level"]

        if trend in ("strong_uptrend", "uptrend") and vix in ("low_fear", "normal"):
            context["market_regime"] = "bull"
            context["guidance"] = "Market is bullish with low fear. Favor long positions and momentum plays. Be aggressive."
        elif trend in ("strong_downtrend", "downtrend") or vix == "extreme_fear":
            context["market_regime"] = "bear"
            context["guidance"] = "Market is bearish or fear is extreme. Prefer inverse ETFs (SOXS, SQQQ, SPXS) or stay in cash."
        elif vix == "elevated":
            context["market_regime"] = "volatile"
            context["guidance"] = "Volatility is elevated. Reduce position sizes by 50%. Avoid high-beta stocks."

    except Exception as e:
        logger.warning(f"Could not compute macro context: {e}")

    return context


def get_sector_rotation() -> str:
    """Identify leading and lagging sectors over the last 5 days."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    sector_etfs = {
        "Tech": "XLK", "Finance": "XLF", "Health": "XLV",
        "Energy": "XLE", "Consumer": "XLY", "Semis": "SOXX",
        "Industrial": "XLI", "Utilities": "XLU",
    }

    client = StockHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_secret_key)

    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=7)
        bars = client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=list(sector_etfs.values()),
                timeframe=TimeFrame.Day,
            feed="iex",
                start=start,
                end=end,
            )
        )

        bars_dict = bars.data if hasattr(bars, "data") else dict(bars)
        performance = {}
        for sector, etf in sector_etfs.items():
            etf_bars = bars_dict.get(etf, [])
            if len(etf_bars) >= 2:
                change = (float(etf_bars[-1].close) - float(etf_bars[0].close)) / float(etf_bars[0].close) * 100
                performance[sector] = round(change, 2)

        if not performance:
            return ""

        sorted_sectors = sorted(performance.items(), key=lambda x: x[1], reverse=True)
        leaders = [f"{s}({v:+.1f}%)" for s, v in sorted_sectors[:2]]
        laggards = [f"{s}({v:+.1f}%)" for s, v in sorted_sectors[-2:]]
        return f"Leading: {', '.join(leaders)} | Lagging: {', '.join(laggards)}"

    except Exception as e:
        logger.warning(f"Sector rotation failed: {e}")
        return ""
