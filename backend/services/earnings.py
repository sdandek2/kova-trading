import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def get_upcoming_earnings(symbols: list[str]) -> dict[str, str]:
    """
    Returns {symbol: timing} for stocks with upcoming earnings detected via news.
    Uses Alpaca news as proxy — looks for earnings keywords in recent articles.
    """
    from config import settings
    from alpaca.data.requests import NewsRequest
    from alpaca.data.historical.news import NewsClient

    earnings_map = {}
    try:
        nc = NewsClient(settings.alpaca_api_key, settings.alpaca_secret_key)
        news = nc.get_news(NewsRequest(symbols=symbols, limit=100))

        earnings_keywords = [
            "earnings", "eps", "quarterly results", "q1", "q2", "q3", "q4",
            "beat", "miss", "guidance", "revenue beat", "profit report",
            "earnings call", "after the bell", "before the bell",
        ]

        for article in news.news:
            headline_lower = article.headline.lower()
            if any(kw in headline_lower for kw in earnings_keywords):
                for sym in (article.symbols or []):
                    if sym in symbols and sym not in earnings_map:
                        if article.created_at:
                            age_hours = (datetime.now(timezone.utc) - article.created_at).total_seconds() / 3600
                            if age_hours < 24:
                                earnings_map[sym] = "today/tomorrow"
                            elif age_hours < 72:
                                earnings_map[sym] = "this_week"
    except Exception as e:
        logger.warning(f"Could not fetch earnings data: {e}")

    return earnings_map


def get_earnings_guidance(symbol: str, timing: str) -> str:
    if timing == "today/tomorrow":
        return f"{symbol} earnings TODAY/TOMORROW — very high risk, gap ±20% possible. Use tiny position or avoid."
    elif timing == "this_week":
        return f"{symbol} earnings THIS WEEK — elevated volatility. Size conservatively."
    return ""
