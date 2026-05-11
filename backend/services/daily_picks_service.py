import json
import logging
from datetime import datetime, timezone, date


import anthropic

from config import settings
from services import alpaca_service
from services.db import cache_get, cache_set
from services.indicators import compute_all
from services.macro import get_macro_context, get_sector_rotation
from services.geopolitical import get_geopolitical_context, get_trend_forecast

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

# Cache key is date-scoped so it refreshes automatically each new trading day
def _picks_cache_key() -> str:
    return f"daily_picks:{date.today().isoformat()}"


def get_daily_picks(force_refresh: bool = False) -> dict:
    """
    Generate high-conviction growth picks once per trading day.
    Returns two lists:
      - short_term: stocks likely to move significantly in 1-2 weeks
      - long_term: stocks with strong 1-3 month growth thesis
    Cached until the next calendar day (key is date-scoped).
    """
    if not force_refresh:
        cached = cache_get(_picks_cache_key())
        if cached:
            logger.info("Daily picks cache hit")
            return cached

    logger.info("Generating fresh daily picks...")

    result = {
        "date": date.today().isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market_regime": "unknown",
        "geo_risk_level": "unknown",
        "short_term": [],   # 1-2 week high-conviction plays
        "long_term": [],    # 1-3 month growth compounders
        "avoid_today": [],  # stocks / sectors to avoid right now
        "summary": "",
        "error": None,
    }

    try:
        # ── Step 1: Gather all market intelligence ──
        universe = alpaca_service.get_tradeable_universe()
        snapshot = alpaca_service.get_market_snapshot_light(universe)
        macro = get_macro_context()
        sector_info = get_sector_rotation()
        geo = get_geopolitical_context()
        trend = get_trend_forecast(macro, geo)

        result["market_regime"] = macro.get("market_regime", "unknown")
        result["geo_risk_level"] = geo.get("risk_level", "unknown")

        # ── Step 2: Fetch recent news for catalysts ──
        news_headlines = []
        try:
            from alpaca.data.requests import NewsRequest
            from alpaca.data.historical.news import NewsClient
            nc = NewsClient(settings.alpaca_api_key, settings.alpaca_secret_key)
            news_resp = nc.get_news(NewsRequest(limit=50))
            news_headlines = [
                f"  • [{', '.join(a.symbols[:3])}] {a.headline}"
                for a in news_resp.news[:20]
                if a.headline
            ]
        except Exception as e:
            logger.warning(f"Could not fetch news for daily picks: {e}")

        # ── Step 3: Build universe summary with momentum data ──
        # Sort by absolute 5-day change to surface the biggest movers
        valid = {
            s: d for s, d in snapshot.items()
            if d.get("current_price") and d["current_price"] > 0
        }
        sorted_by_move = sorted(
            valid.items(),
            key=lambda x: abs(x[1].get("five_day_change_pct") or 0),
            reverse=True,
        )

        # Top 80 by momentum for the prompt
        top_movers_lines = []
        for sym, data in sorted_by_move[:80]:
            price = data.get("current_price", 0)
            chg = data.get("five_day_change_pct")
            chg_str = f"{chg:+.1f}%" if chg is not None else "N/A"
            penny = " [PENNY]" if price < 5 else ""
            top_movers_lines.append(f"  {sym}{penny}: ${price:.2f}, 5d: {chg_str}")

        universe_text = "\n".join(top_movers_lines)
        news_text = "\n".join(news_headlines) or "  No news available"

        # ── Step 4: Claude deep analysis ──
        prompt = f"""You are a top-tier equity research analyst. Your job today is to identify the HIGHEST CONVICTION growth opportunities across the entire market.

Today is {date.today().strftime('%A, %B %d, %Y')}.

## Current Market Environment
{trend}

### Macro Details
- Regime: {macro.get('market_regime', 'neutral').upper()} | SPY: {macro.get('spy_trend')} | Fear: {macro.get('vix_level')}
- Guidance: {macro.get('guidance', '')}
- Sector rotation: {sector_info}

### Geopolitical Context
- Risk: {geo.get('risk_level', 'low').upper()} (score {geo.get('risk_score', 0)}/100)
- Active themes: {', '.join(geo.get('dominant_themes', [])) or 'none'}
- Geopolitical opportunities: {', '.join(geo.get('market_impact', {}).get('opportunities', [])) or 'none'}
- Bull scenario: {geo.get('scenarios', {}).get('bull_case', '')}
- Bear scenario: {geo.get('scenarios', {}).get('bear_case', '')}

## Breaking News & Catalysts (last 24 hours)
{news_text}

## Market Universe — Top Movers (sorted by 5-day momentum)
{universe_text}

---

Your task: Identify the BEST growth opportunities for TWO time horizons.

**SHORT-TERM picks (1-2 weeks):**
Focus on: technical breakouts, momentum continuation, upcoming catalysts (earnings, FDA, product launch), news-driven sentiment plays, short squeeze candidates, sector rotation beneficiaries. These should be high-velocity moves.

**LONG-TERM picks (1-3 months):**
Focus on: structural tailwinds, macro/geopolitical beneficiaries, strong sector leadership, stocks building a base before a big move, undervalued relative to peers. These are higher-conviction, lower-risk compounders.

**AVOID list:**
Stocks/sectors that look dangerous right now — overextended, facing headwinds, earnings risk, geopolitical exposure.

For each pick, be SPECIFIC and BOLD. Don't give generic answers. State exactly why this stock, why now, what drives it, and what could go wrong.

Respond ONLY in JSON:
{{
  "summary": "2-3 sentence overall market assessment and what kind of day/week it will be for traders",
  "short_term": [
    {{
      "symbol": "XXXX",
      "type": "breakout|momentum|catalyst|sentiment|squeeze|rotation",
      "current_price_approx": 123.45,
      "upside_pct": 15,
      "target_price": 142.00,
      "time_horizon": "1-2 weeks",
      "confidence": "high|medium",
      "thesis": "2-3 sentences: specific reason this stock moves NOW, what catalyst drives it, why the timing is right",
      "entry_zone": "buy below $X or on dip to $Y",
      "invalidation": "what would make this thesis wrong (e.g. closes below $X, macro shift)",
      "key_risk": "single biggest risk to this trade"
    }}
  ],
  "long_term": [
    {{
      "symbol": "XXXX",
      "type": "compounder|sector_leader|macro_play|turnaround|geopolitical",
      "current_price_approx": 123.45,
      "upside_pct": 35,
      "target_price": 166.00,
      "time_horizon": "1-3 months",
      "confidence": "high|medium",
      "thesis": "2-3 sentences: structural reason this stock grows, macro/geo tailwind, competitive position",
      "entry_zone": "ideal entry price or range",
      "invalidation": "what would make this thesis wrong",
      "key_risk": "single biggest risk"
    }}
  ],
  "avoid_today": [
    {{
      "symbol_or_sector": "XXXX or 'Airlines sector'",
      "reason": "one sentence why to avoid"
    }}
  ]
}}

Provide 5 short-term picks and 5 long-term picks. Be specific, actionable, and conviction-driven."""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2500,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw.strip())

        result["short_term"] = data.get("short_term", [])
        result["long_term"] = data.get("long_term", [])
        result["avoid_today"] = data.get("avoid_today", [])
        result["summary"] = data.get("summary", "")

        logger.info(
            f"Daily picks: {len(result['short_term'])} short-term, "
            f"{len(result['long_term'])} long-term | "
            f"Regime: {result['market_regime']} | Geo: {result['geo_risk_level']}"
        )
        logger.info(f"Short-term picks: {[p['symbol'] for p in result['short_term']]}")
        logger.info(f"Long-term picks: {[p['symbol'] for p in result['long_term']]}")

    except Exception as e:
        logger.error(f"Daily picks generation failed: {e}", exc_info=True)
        result["error"] = str(e)

    # Cache until end of day (seconds remaining in the day)
    now = datetime.now(timezone.utc)
    seconds_until_midnight = int(
        (datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
         + __import__('datetime').timedelta(days=1) - now).total_seconds()
    )
    cache_set(_picks_cache_key(), result, max(seconds_until_midnight, 3600))
    return result
