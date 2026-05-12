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
        # NewsRequest expects symbols as a comma-separated string, not a list
        symbols_str = ",".join(symbols[:50]) if symbols else ""
        news = nc.get_news(NewsRequest(symbols=symbols_str, limit=50))

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


def get_earnings_play_candidates(symbols: list[str], earnings_map: dict[str, str]) -> list[dict]:
    """
    Identify stocks with earnings THIS WEEK that are good candidates for a pre-earnings play.
    Strategy: buy small position 1-2 days before earnings, sell before the report (captures run-up).

    Returns list of dicts: [{"symbol": str, "timing": str, "play_type": "pre_earnings_runup", "suggested_size_pct": float, "reason": str}]
    """
    try:
        candidates = []
        for symbol in symbols:
            if earnings_map.get(symbol) == "this_week":
                candidates.append({
                    "symbol": symbol,
                    "timing": "this_week",
                    "play_type": "pre_earnings_runup",
                    "suggested_size_pct": 0.05,
                    "reason": f"{symbol} has earnings this week — pre-earnings run-up play. Buy now, sell BEFORE the report. Small position only.",
                })
        return candidates
    except Exception as e:
        logger.warning(f"get_earnings_play_candidates failed ({e}), returning empty list.")
        return []


def get_earnings_guidance(symbol: str, timing: str) -> str:
    if timing == "today/tomorrow":
        return f"{symbol} earnings TODAY/TOMORROW — very high risk, gap ±20% possible. Use tiny position or avoid."
    elif timing == "this_week":
        return f"{symbol} earnings THIS WEEK — elevated volatility. Size conservatively."
    return ""
