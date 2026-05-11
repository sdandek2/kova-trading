import json
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

from config import settings
from services import alpaca_service
from services.ai_client import ask_ai
from services.db import cache_get, cache_set
from services.indicators import compute_all, compute_atr
from services.macro import get_macro_context, get_sector_rotation
from services.geopolitical import get_geopolitical_context

logger = logging.getLogger(__name__)

CACHE_TTL_MINUTES = 60
CACHE_TTL_SECONDS = CACHE_TTL_MINUTES * 60


def get_stock_prediction(symbol: str) -> dict:
    """
    Generate a Claude-powered long-term prediction for a single stock.
    Returns 1W / 1M / 3M price targets, bull/base/bear scenarios,
    key catalysts, risks, and a recommendation.
    Cached for 1 hour.
    """
    cached = cache_get(f"prediction:{symbol}")
    if cached:
        logger.info(f"Prediction cache hit for {symbol}")
        return cached

    result = {
        "symbol": symbol,
        "current_price": None,
        "recommendation": "hold",
        "confidence": "low",
        "technical_signal": "neutral",
        "sentiment_signal": "neutral",
        "macro_alignment": "neutral",
        "targets": {
            "week_1": {"price": None, "change_pct": None, "rationale": ""},
            "month_1": {"price": None, "change_pct": None, "rationale": ""},
            "month_3": {"price": None, "change_pct": None, "rationale": ""},
        },
        "scenarios": {
            "bull": {"price_target": None, "trigger": "", "probability": "low"},
            "base": {"price_target": None, "trigger": "", "probability": "medium"},
            "bear": {"price_target": None, "trigger": "", "probability": "low"},
        },
        "key_catalysts": [],
        "key_risks": [],
        "reasoning": "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cache_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=CACHE_TTL_MINUTES)).isoformat(),
    }

    try:
        # Fetch 90-day price data + indicators
        snapshot = alpaca_service.get_market_snapshot([symbol])
        sym_data = snapshot.get(symbol, {})
        closing_prices = sym_data.get("closing_prices", [])
        high_prices = sym_data.get("high_prices", [])
        low_prices = sym_data.get("low_prices", [])
        current_price = sym_data.get("current_price")
        result["current_price"] = current_price

        # Early exit if no price data — OTC, delisted, or not on Alpaca
        if current_price is None:
            result["reasoning"] = (
                f"No price data available for {symbol}. "
                "It may be OTC, delisted, or not tradeable on Alpaca."
            )
            return result

        indicators = compute_all(closing_prices) if closing_prices else {}
        atr = compute_atr(high_prices, low_prices, closing_prices)

        rsi = indicators.get("rsi", "N/A")
        macd = indicators.get("macd", {})
        mas = indicators.get("moving_averages", {})

        # News sentiment
        try:
            from alpaca.data.requests import NewsRequest
            from alpaca.data.historical.news import NewsClient
            nc = NewsClient(settings.alpaca_api_key, settings.alpaca_secret_key)
            news_resp = nc.get_news(NewsRequest(symbols=[symbol], limit=30))
            headlines = [a.headline for a in news_resp.news[:10] if a.headline]
            news_text = "\n".join([f"  • {h}" for h in headlines]) or "  No recent news"
        except Exception:
            news_text = "  Could not fetch news"

        # Macro + geo context
        macro = get_macro_context()
        geo = get_geopolitical_context()
        geo_risk = geo.get("risk_level", "low")
        geo_themes = ", ".join(geo.get("dominant_themes", [])) or "none"

        # 90-day price summary
        if closing_prices and len(closing_prices) >= 20:
            high_90 = max(closing_prices)
            low_90 = min(closing_prices)
            change_90 = round((closing_prices[-1] - closing_prices[0]) / closing_prices[0] * 100, 2)
            change_20 = round((closing_prices[-1] - closing_prices[-20]) / closing_prices[-20] * 100, 2)
            change_5 = round((closing_prices[-1] - closing_prices[-5]) / closing_prices[-5] * 100, 2)
            price_summary = (
                f"Current: ${current_price} | 90d high: ${high_90:.2f} | 90d low: ${low_90:.2f}\n"
                f"5d change: {change_5:+.2f}% | 20d change: {change_20:+.2f}% | 90d change: {change_90:+.2f}%"
            )
        else:
            price_summary = f"Current: ${current_price}"

        prompt = f"""You are a professional equity analyst providing a detailed long-term stock analysis and price prediction.

## Stock: {symbol}

### Price Data (90 days)
{price_summary}

### Technical Indicators
- RSI(14): {rsi} {"[OVERBOUGHT]" if isinstance(rsi, (int,float)) and rsi > 70 else "[OVERSOLD]" if isinstance(rsi, (int,float)) and rsi < 30 else ""}
- MACD histogram: {macd.get('histogram', 'N/A')} (positive = bullish momentum)
- MA20: ${mas.get('ma20', 'N/A')} | MA50: ${mas.get('ma50', 'N/A')}
- ATR(14): ${atr:.2f} (daily volatility range)
- Price vs MA20: {"ABOVE" if current_price and mas.get('ma20') and current_price > mas.get('ma20') else "BELOW"}
- Price vs MA50: {"ABOVE" if current_price and mas.get('ma50') and current_price > mas.get('ma50') else "BELOW"}

### Recent News Headlines
{news_text}

### Macro & Geopolitical Context
- Market regime: {macro.get('market_regime', 'neutral').upper()} | SPY: {macro.get('spy_trend')} | Fear: {macro.get('vix_level')}
- Geopolitical risk: {geo_risk.upper()} | Active themes: {geo_themes}
- Macro guidance: {macro.get('guidance', '')}
- Forward outlook: {geo.get('scenarios', {}).get('base_case', '')}

Provide a comprehensive prediction covering:
1. Short-term (1 week): price target and rationale
2. Medium-term (1 month): price target and rationale
3. Long-term (3 months): price target and rationale
4. Three scenarios: bull case (optimistic), base case (most likely), bear case (pessimistic)
5. 3-5 key catalysts that could drive the stock higher
6. 3-5 key risks that could hurt the stock
7. Overall recommendation and confidence

Respond ONLY in JSON with this exact structure:
{{
  "recommendation": "strong_buy|buy|hold|sell|strong_sell",
  "confidence": "high|medium|low",
  "technical_signal": "bullish|bearish|neutral",
  "sentiment_signal": "bullish|bearish|neutral",
  "macro_alignment": "aligned|against|neutral",
  "targets": {{
    "week_1": {{"price": 123.45, "change_pct": 3.2, "rationale": "one sentence"}},
    "month_1": {{"price": 130.00, "change_pct": 8.5, "rationale": "one sentence"}},
    "month_3": {{"price": 145.00, "change_pct": 21.0, "rationale": "one sentence"}}
  }},
  "scenarios": {{
    "bull": {{"price_target": 160.0, "trigger": "what would cause this", "probability": "low|medium|high"}},
    "base": {{"price_target": 135.0, "trigger": "most likely path", "probability": "low|medium|high"}},
    "bear": {{"price_target": 110.0, "trigger": "what would cause this", "probability": "low|medium|high"}}
  }},
  "key_catalysts": ["catalyst 1", "catalyst 2", "catalyst 3"],
  "key_risks": ["risk 1", "risk 2", "risk 3"],
  "reasoning": "2-3 sentence overall investment thesis"
}}"""

        raw = ask_ai(prompt, max_tokens=2048)
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            # AI returned malformed JSON — try to extract the JSON object via regex
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match:
                data = json.loads(match.group())
            else:
                raise ValueError(f"Could not parse AI response as JSON: {raw[:200]}")

        result.update({k: v for k, v in data.items() if k in result})
        result["generated_at"] = datetime.now(timezone.utc).isoformat()
        result["cache_expires_at"] = (datetime.now(timezone.utc) + timedelta(minutes=CACHE_TTL_MINUTES)).isoformat()

        logger.info(f"Prediction for {symbol}: {result['recommendation']} ({result['confidence']} confidence)")

        # Only cache successful predictions
        cache_set(f"prediction:{symbol}", result, CACHE_TTL_SECONDS)

    except Exception as e:
        logger.error(f"Prediction failed for {symbol}: {e}")
        result["reasoning"] = f"Prediction unavailable: {str(e)}"
        # Do NOT cache failures — let the next request retry

    return result


def get_top_suggestions(n: int = 8) -> list[dict]:
    """
    Return top N stock suggestions from the dynamic universe right now.
    Uses Claude to rank and explain opportunities with short and long-term context.
    Cached for 1 hour.
    """
    cached_list = cache_get("suggestions:top")
    if cached_list:
        logger.info("Suggestions cache hit")
        return cached_list

    suggestions = []

    try:
        # Get universe + light snapshot
        universe = alpaca_service.get_tradeable_universe()
        snapshot = alpaca_service.get_market_snapshot_light(universe)

        # Filter: need valid price data
        valid = {s: d for s, d in snapshot.items() if d.get("current_price") and d["current_price"] > 0}

        # Build compact universe summary for Claude
        lines = []
        for sym, data in list(valid.items())[:120]:
            price = data.get("current_price")
            chg = data.get("five_day_change_pct", "N/A")
            penny = " [PENNY]" if price and price < 5 else ""
            lines.append(f"  {sym}{penny}: ${price:.2f}, 5d: {chg}%")
        universe_text = "\n".join(lines)

        macro = get_macro_context()
        geo = get_geopolitical_context()
        geo_risk = geo.get("risk_level", "low")
        geo_themes = ", ".join(geo.get("dominant_themes", [])) or "none"
        geo_opps = ", ".join(geo.get("market_impact", {}).get("opportunities", [])) or "none"
        geo_safe = ", ".join(geo.get("market_impact", {}).get("safe_havens", [])) or "none"

        prompt = f"""You are a stock analyst identifying the best trading and investment opportunities right now.

## Current Market Context
- Macro regime: {macro.get('market_regime', 'neutral').upper()} | SPY: {macro.get('spy_trend')} | Fear: {macro.get('vix_level')}
- Macro guidance: {macro.get('guidance', '')}
- Geopolitical risk: {geo_risk.upper()} | Themes: {geo_themes}
- Geopolitical opportunities: {geo_opps}
- Safe havens: {geo_safe}
- Bull scenario: {geo.get('scenarios', {}).get('bull_case', '')}
- Bear scenario: {geo.get('scenarios', {}).get('bear_case', '')}

## Available Stocks (price + 5-day momentum)
{universe_text}

Select the TOP {n} stocks/ETFs that offer the best opportunity RIGHT NOW considering:
1. Current price momentum (5-day change)
2. Alignment with macro regime (bull/bear/volatile)
3. Geopolitical tailwinds or safe-haven status
4. Risk/reward for both short-term (1 week) and long-term (1-3 months)

For each pick, provide a short-term and long-term thesis.

Respond ONLY in JSON:
{{
  "suggestions": [
    {{
      "symbol": "XXXX",
      "type": "momentum|defensive|geopolitical|contrarian|etf",
      "horizon": "short_term|long_term|both",
      "short_term_thesis": "1-2 sentences for next 1-2 weeks",
      "long_term_thesis": "1-2 sentences for next 1-3 months",
      "risk_level": "low|medium|high",
      "entry_note": "what to watch before buying (e.g. confirm above MA20)",
      "upside_pct": estimated_upside_percentage_as_number
    }}
  ]
}}"""

        raw = ask_ai(prompt, max_tokens=2000)
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw.strip())
        raw_suggestions = data.get("suggestions", [])

        # Enrich with current price from snapshot
        for s in raw_suggestions:
            sym = s.get("symbol", "")
            s["current_price"] = valid.get(sym, {}).get("current_price")
            s["five_day_change_pct"] = valid.get(sym, {}).get("five_day_change_pct")

        suggestions = raw_suggestions
        logger.info(f"Generated {len(suggestions)} suggestions: {[s['symbol'] for s in suggestions]}")

        # Only cache successful results
        cache_set("suggestions:top", suggestions, CACHE_TTL_SECONDS)

    except Exception as e:
        logger.error(f"Suggestions failed: {e}")
        suggestions = []
        # Do NOT cache failures

    return suggestions
