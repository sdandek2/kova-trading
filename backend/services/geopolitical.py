import logging
from datetime import datetime, timezone, timedelta
from collections import Counter

from config import settings

logger = logging.getLogger(__name__)

# Keyword categories for geopolitical/macro risk scoring
RISK_THEMES = {
    "trade_war": [
        "tariff", "tariffs", "trade war", "trade deal", "import duty", "export ban",
        "sanctions", "embargo", "trade deficit", "protectionism", "WTO",
    ],
    "geopolitical_conflict": [
        "war", "invasion", "military", "missile", "airstrike", "conflict", "ceasefire",
        "NATO", "Ukraine", "Russia", "China", "Taiwan", "Middle East", "Iran", "North Korea",
        "troops", "troops deployed", "escalation",
    ],
    "fed_policy": [
        "Federal Reserve", "Fed", "interest rate", "rate hike", "rate cut", "FOMC",
        "Jerome Powell", "inflation", "CPI", "PCE", "quantitative tightening", "QT",
        "balance sheet", "monetary policy", "basis points",
    ],
    "recession_risk": [
        "recession", "GDP", "unemployment", "layoffs", "job cuts", "bankruptcy",
        "default", "debt ceiling", "credit crunch", "economic slowdown", "contraction",
        "stagflation", "yield curve", "inverted",
    ],
    "energy_commodities": [
        "oil", "crude", "OPEC", "natural gas", "energy crisis", "commodity",
        "gold", "inflation hedge", "supply chain", "shortage",
    ],
    "tech_regulation": [
        "antitrust", "regulation", "SEC", "investigation", "fine", "ban", "AI regulation",
        "chip ban", "semiconductor", "export control", "Big Tech",
    ],
    "earnings_macro": [
        "earnings beat", "earnings miss", "guidance", "revenue", "outlook",
        "downgrade", "upgrade", "analyst", "price target",
    ],
}

# Risk weights: how much each theme affects the overall risk score
THEME_WEIGHTS = {
    "geopolitical_conflict": 25,
    "trade_war": 20,
    "fed_policy": 20,
    "recession_risk": 15,
    "energy_commodities": 10,
    "tech_regulation": 5,
    "earnings_macro": 5,
}


def _score_article(text: str) -> dict[str, int]:
    """Return hit counts per theme for a single article."""
    text_lower = text.lower()
    hits = {}
    for theme, keywords in RISK_THEMES.items():
        count = sum(1 for kw in keywords if kw.lower() in text_lower)
        if count:
            hits[theme] = count
    return hits


def get_geopolitical_context() -> dict:
    """
    Pulls latest 200 news articles, scores them for geopolitical/macro themes,
    and returns a structured risk assessment with forward-looking guidance.
    """
    from alpaca.data.requests import NewsRequest
    from alpaca.data.historical.news import NewsClient

    result = {
        "risk_score": 0,          # 0-100: overall geopolitical risk level
        "risk_level": "low",      # low / moderate / elevated / high / extreme
        "dominant_themes": [],    # top 3 active themes right now
        "theme_scores": {},       # score per theme
        "key_headlines": [],      # top 5 most relevant headlines
        "forward_guidance": "",   # what this means for trading
        "market_impact": {
            "sectors_at_risk": [],
            "safe_havens": [],
            "opportunities": [],
        },
        "scenarios": {
            "bull_case": "",
            "base_case": "",
            "bear_case": "",
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        nc = NewsClient(settings.alpaca_api_key, settings.alpaca_secret_key)
        news = nc.get_news(NewsRequest(limit=50))
        articles = news.news
    except Exception as e:
        logger.warning(f"Could not fetch news for geopolitical analysis: {e}")
        return result

    theme_totals: Counter = Counter()
    headline_scores: list[tuple[float, str, str]] = []  # (score, headline, url)

    for article in articles:
        text = f"{article.headline or ''} {article.summary or ''}"
        hits = _score_article(text)
        if hits:
            for theme, count in hits.items():
                theme_totals[theme] += count
            total_hits = sum(hits.values())
            headline_scores.append((total_hits, article.headline or "", article.url or ""))

    if not theme_totals:
        result["forward_guidance"] = "No significant geopolitical signals detected. Market appears quiet."
        result["scenarios"]["base_case"] = "Normal market conditions. Trade on technical signals."
        return result

    # Compute weighted risk score (0-100)
    raw_score = 0
    for theme, count in theme_totals.items():
        weight = THEME_WEIGHTS.get(theme, 5)
        # Normalize: 10+ mentions = full weight for that theme
        theme_contribution = min(count / 10.0, 1.0) * weight
        raw_score += theme_contribution
        result["theme_scores"][theme] = count

    result["risk_score"] = min(100, int(raw_score))

    if result["risk_score"] >= 70:
        result["risk_level"] = "extreme"
    elif result["risk_score"] >= 50:
        result["risk_level"] = "high"
    elif result["risk_score"] >= 30:
        result["risk_level"] = "elevated"
    elif result["risk_score"] >= 15:
        result["risk_level"] = "moderate"
    else:
        result["risk_level"] = "low"

    # Top themes by mention count
    result["dominant_themes"] = [t for t, _ in theme_totals.most_common(3)]

    # Top 5 most relevant headlines
    headline_scores.sort(reverse=True)
    result["key_headlines"] = [
        {"headline": h, "url": u} for _, h, u in headline_scores[:5]
    ]

    # Forward guidance and market impact based on dominant themes
    result["forward_guidance"], result["market_impact"], result["scenarios"] = _generate_guidance(
        result["dominant_themes"], result["risk_level"], result["theme_scores"]
    )

    logger.info(
        f"Geopolitical risk: {result['risk_level'].upper()} (score={result['risk_score']}) | "
        f"Themes: {result['dominant_themes']}"
    )
    return result


def _generate_guidance(themes: list[str], risk_level: str, scores: dict) -> tuple[str, dict, dict]:
    """Generate trading guidance, market impact, and scenarios from active themes."""

    guidance_parts = []
    at_risk = []
    safe_havens = ["GLD", "TLT", "VXX", "UVXY"]
    opportunities = []
    bull_case = ""
    bear_case = ""
    base_case = ""

    if "geopolitical_conflict" in themes:
        guidance_parts.append("Active geopolitical conflict detected — defense stocks may surge, energy prices volatile.")
        at_risk.extend(["airlines", "tourism", "emerging markets"])
        opportunities.extend(["LMT", "RTX", "NOC", "GLD", "USO"])
        bear_case = "Escalating conflict triggers risk-off: sell equities, rotate to GLD/bonds/inverse ETFs."
        bull_case = "Ceasefire or diplomatic progress → sharp relief rally in risk assets."

    if "trade_war" in themes:
        guidance_parts.append("Trade war signals active — multinationals with China exposure at risk.")
        at_risk.extend(["AAPL", "NVDA", "semiconductors"])
        opportunities.extend(["domestic manufacturers", "SOXX-short", "SQQQ"])
        bear_case = "New tariffs announced → tech/semis sell off hard. Rotate to domestic plays."
        bull_case = "Trade deal progress → tech/semis gap up. Buy SOXX, QQQ on dip."

    if "fed_policy" in themes:
        guidance_parts.append("Fed policy in focus — rate decisions dominate short-term direction.")
        if scores.get("fed_policy", 0) > 15:
            at_risk.extend(["growth stocks", "real estate", "utilities"])
            opportunities.extend(["financials (XLF)", "short-duration bonds"])
        bear_case = "Hawkish Fed surprises markets with rate hike or delayed cuts → growth stocks crater."
        bull_case = "Dovish pivot signals → rate-sensitive growth stocks rally. Buy QQQ, ARKK."

    if "recession_risk" in themes:
        guidance_parts.append("Recession signals building — defensive rotation likely.")
        at_risk.extend(["consumer discretionary (XLY)", "financials (XLF)"])
        safe_havens.extend(["XLP", "XLU", "XLV"])
        opportunities.extend(["SQQQ", "SPXS", "inverse ETFs"])
        bear_case = "Recession confirmed → broad market down 20-30%. Heavy inverse ETF exposure."
        bull_case = "Soft landing achieved → relief rally. Buy broad market ETFs on dip."

    if "energy_commodities" in themes:
        guidance_parts.append("Energy/commodity volatility detected — inflation risk elevated.")
        opportunities.extend(["XLE", "USO", "GLD", "SLV"])
        bear_case = "Energy spike drives inflation higher → Fed stays hawkish → stagflation."
        bull_case = "Energy stabilizes → inflation cools → rate cuts back on table → risk-on."

    if not guidance_parts:
        guidance_parts.append("No dominant macro themes. Focus on individual stock technicals.")
        bull_case = "Technical momentum drives individual stock gains."
        bear_case = "Profit-taking after recent rally pressures indexes."

    if not base_case:
        base_case = f"Risk level is {risk_level}. Trade with reduced size on volatile stocks. Favor liquid large-caps and sector ETFs."

    if risk_level in ("high", "extreme"):
        guidance_parts.append("RISK ELEVATED: Reduce all position sizes by 40-50%. Prefer inverse ETFs and cash.")
    elif risk_level == "elevated":
        guidance_parts.append("RISK MODERATE-HIGH: Trade smaller. Use tighter stops. Avoid leveraged longs.")

    market_impact = {
        "sectors_at_risk": list(set(at_risk))[:5],
        "safe_havens": list(set(safe_havens))[:5],
        "opportunities": list(set(opportunities))[:6],
    }

    scenarios = {
        "bull_case": bull_case or "Market catalysts resolve positively — broad rally.",
        "base_case": base_case,
        "bear_case": bear_case or "Risk-off sentiment spreads — defensive rotation accelerates.",
    }

    return " ".join(guidance_parts), market_impact, scenarios


def get_trend_forecast(macro_context: dict, geo_context: dict) -> str:
    """
    Combine macro + geopolitical data into a concise 1-5 day forward trend forecast.
    Used as additional context in Claude's trading prompts.
    """
    lines = []

    regime = macro_context.get("market_regime", "neutral")
    spy_trend = macro_context.get("spy_trend", "neutral")
    vix = macro_context.get("vix_level", "normal")
    geo_risk = geo_context.get("risk_level", "low")
    geo_themes = geo_context.get("dominant_themes", [])
    geo_score = geo_context.get("risk_score", 0)

    lines.append(f"## Forward Market Outlook (1-5 Day Forecast)")
    lines.append(f"- Macro regime: {regime.upper()} | SPY: {spy_trend} | Fear: {vix}")
    lines.append(f"- Geopolitical risk: {geo_risk.upper()} (score {geo_score}/100)")

    if geo_themes:
        lines.append(f"- Active macro themes: {', '.join(geo_themes)}")

    # Combine signals into an overall outlook
    if regime == "bull" and geo_risk in ("low", "moderate"):
        lines.append("- OUTLOOK: BULLISH. Technicals + geopolitics both supportive. Favor momentum longs.")
    elif regime == "bear" or geo_risk in ("high", "extreme"):
        lines.append("- OUTLOOK: BEARISH. Macro headwinds + geopolitical stress. Favor inverse ETFs or cash.")
    elif vix == "extreme_fear" or geo_risk == "elevated":
        lines.append("- OUTLOOK: CAUTIOUS. Elevated risk environment. Trade smaller, tighter stops.")
    else:
        lines.append("- OUTLOOK: NEUTRAL. No strong directional bias. Follow individual stock signals.")

    scenarios = geo_context.get("scenarios", {})
    if scenarios.get("bull_case"):
        lines.append(f"- Bull case: {scenarios['bull_case']}")
    if scenarios.get("bear_case"):
        lines.append(f"- Bear case: {scenarios['bear_case']}")

    # Safe havens + opportunities
    impact = geo_context.get("market_impact", {})
    if impact.get("opportunities"):
        lines.append(f"- Geopolitical opportunities: {', '.join(impact['opportunities'][:4])}")
    if impact.get("safe_havens"):
        lines.append(f"- Safe havens: {', '.join(impact['safe_havens'][:4])}")

    return "\n".join(lines)
