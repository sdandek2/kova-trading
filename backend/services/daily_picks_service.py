import json
import logging
from datetime import datetime, timezone, date, timedelta


from config import settings
from services import alpaca_service
from services.db import cache_get, cache_set
from services.indicators import compute_all
from services.macro import get_macro_context, get_sector_rotation
from services.geopolitical import get_geopolitical_context, get_trend_forecast

from services.ai_client import ask_ai_pro

logger = logging.getLogger(__name__)

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
        "short_term": [],     # 1-2 week high-conviction long plays
        "long_term": [],      # 1-3 month growth compounders (long)
        "bearish_plays": [],  # short-sell candidates + inverse ETFs for bear conditions
        "avoid_today": [],    # stocks / sectors to avoid right now
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

        # ── Step 2: Fetch recent news from ALL sources for catalysts ──
        news_headlines = []
        try:
            news_articles = alpaca_service.get_news(limit=30)
            news_headlines = [
                f"  • [{art.get('source', '')}] [{', '.join(art.get('symbols', [])[:3])}] {art['headline']}"
                for art in news_articles[:20]
                if art.get("headline")
            ]
            logger.info(f"Daily picks: fetched {len(news_articles)} news articles from multi-source feed")
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

Your task: Pick the BEST 8-10 trades for maximum profit today — whatever mix of long, short, and inverse ETF gives the highest conviction. Let the market conditions drive the allocation:

- **Strong bull day** → mostly short-term longs, 1-2 bearish hedges
- **Strong bear day** → mostly bearish plays (shorts + inverse ETFs), minimal longs
- **Mixed/choppy day** → balanced mix, focus on uncorrelated plays
- **Sector rotation** → long the hot sector, short the one rolling over

**LONG picks** (direction: "long"):
- Short-term (1-2 weeks): breakouts, momentum, catalysts, sentiment plays, short squeezes
- Long-term (1-3 months): structural tailwinds, macro beneficiaries, sector leaders

**BEARISH plays** (profit when price falls):
- SHORT candidates (direction: "short"): overextended stocks, failed breakouts, negative catalysts, sector headwinds. Must have high float, no squeeze risk, no imminent earnings.
- INVERSE ETFs (direction: "inverse_etf"): SQQQ, SPXU, SOXS, SDOW, TZA — use when broad market or a whole sector looks weak.

**AVOID list**: stocks/sectors that are dangerous right now.

For every pick: be SPECIFIC and BOLD. State exactly why this stock, why now, what drives it, what could go wrong.

Respond ONLY in valid JSON — no markdown, no explanation:
{{
  "summary": "2-3 sentence overall market assessment, what kind of day it is for traders, and what the optimal strategy mix is today",
  "short_term": [
    {{
      "symbol": "XXXX",
      "type": "breakout|momentum|catalyst|sentiment|squeeze|rotation",
      "direction": "long",
      "current_price_approx": 123.45,
      "upside_pct": 15,
      "target_price": 142.00,
      "time_horizon": "1-2 weeks",
      "confidence": "high|medium",
      "thesis": "2-3 sentences: specific reason this stock moves NOW, what catalyst drives it, why the timing is right",
      "entry_zone": "buy below $X or on dip to $Y",
      "invalidation": "what would make this thesis wrong",
      "key_risk": "single biggest risk to this trade"
    }}
  ],
  "long_term": [
    {{
      "symbol": "XXXX",
      "type": "compounder|sector_leader|macro_play|turnaround|geopolitical",
      "direction": "long",
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
  "bearish_plays": [
    {{
      "symbol": "SQQQ",
      "type": "inverse_etf|short_candidate|breakdown|overextended|sector_weakness",
      "direction": "inverse_etf",
      "current_price_approx": 12.50,
      "upside_pct": 20,
      "target_price": 15.00,
      "time_horizon": "3-7 days",
      "confidence": "high|medium",
      "thesis": "2-3 sentences: why this falls, what the bearish catalyst is, why now is the right time",
      "entry_zone": "short above $X / buy inverse ETF at $Y",
      "invalidation": "what would make this bearish thesis wrong",
      "key_risk": "single biggest risk (e.g. short squeeze, unexpected bounce)"
    }}
  ],
  "avoid_today": [
    {{
      "symbol_or_sector": "XXXX or 'Airlines sector'",
      "reason": "one sentence why to avoid"
    }}
  ]
}}

Total picks: 8-10 across all three categories. Allocate based on today's market conditions — maximize profit potential, not category balance. Be specific, actionable, and conviction-driven."""

        raw = ask_ai_pro(prompt, max_tokens=3500)
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            # Fallback: extract JSON object via regex in case of extra prose
            import re as _re
            match = _re.search(r'\{.*\}', raw, _re.DOTALL)
            if match:
                data = json.loads(match.group())
            else:
                raise ValueError(f"Could not extract JSON from AI response: {raw[:200]}")

        result["short_term"] = data.get("short_term", [])
        result["long_term"] = data.get("long_term", [])
        result["bearish_plays"] = data.get("bearish_plays", [])
        result["avoid_today"] = data.get("avoid_today", [])
        result["summary"] = data.get("summary", "")

        logger.info(
            f"Daily picks: {len(result['short_term'])} short-term, "
            f"{len(result['long_term'])} long-term, "
            f"{len(result['bearish_plays'])} bearish | "
            f"Regime: {result['market_regime']} | Geo: {result['geo_risk_level']}"
        )
        logger.info(f"Short-term picks: {[p['symbol'] for p in result['short_term']]}")
        logger.info(f"Long-term picks: {[p['symbol'] for p in result['long_term']]}")
        logger.info(f"Bearish plays: {[p['symbol'] for p in result['bearish_plays']]}")

    except Exception as e:
        logger.error(f"Daily picks generation failed: {e}", exc_info=True)
        result["error"] = str(e)

    # Only cache successful results — on failure, let the next request retry
    if result["error"] is None:
        now_utc = datetime.now(timezone.utc)
        midnight_utc = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc) + timedelta(days=1)
        seconds_until_midnight = int((midnight_utc - now_utc).total_seconds())
        cache_set(_picks_cache_key(), result, max(seconds_until_midnight, 3600))
    else:
        logger.warning("Daily picks generation failed — skipping cache so next request can retry")
    return result
