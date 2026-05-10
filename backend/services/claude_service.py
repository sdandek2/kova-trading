import json
import logging
from datetime import datetime, timezone

from models.trade import TradeDecision
from services.indicators import compute_all
from services import strategy as strategy_service
from services.ai_client import ask_ai

logger = logging.getLogger(__name__)

def _get_watchlist() -> list[str]:
    from routers.watchlist import load_watchlist
    return load_watchlist()


def analyze_and_decide(
    market_snapshot: dict,
    positions: list,
    account_cash: float,
    portfolio_value: float,
    sentiment: dict = None,
    macro: dict = None,
    sector_info: str = "",
    earnings_map: dict = None,
    geo_context: dict = None,
    trend_forecast: str = "",
    full_data_fetcher=None,
) -> TradeDecision:
    current_strategy = strategy_service.get_strategy()
    max_position = portfolio_value * current_strategy["max_position_pct"]

    positions_text = "\n".join([
        f"  - {p.symbol}: {p.qty} shares @ avg ${p.avg_entry_price:.2f}, "
        f"current ${p.current_price:.2f}, P&L: ${p.unrealized_pl:.2f} ({p.unrealized_pl_percent:.1f}%)"
        for p in positions
    ]) or "  None"

    snapshot_lines = []
    for sym, data in market_snapshot.items():
        closing_prices = data.get("closing_prices", [])
        indicators = compute_all(closing_prices) if closing_prices else {}
        rsi = indicators.get("rsi", "N/A")
        macd = indicators.get("macd", {})
        mas = indicators.get("moving_averages", {})
        price = data.get("current_price", None)
        news_mentions = (sentiment or {}).get(sym, 0)

        penny_flag = " [PENNY]" if price and price < 5.0 else ""
        sentiment_flag = f" [NEWS:{news_mentions}]" if news_mentions > 0 else ""
        earnings_flag = f" [EARNINGS:{(earnings_map or {}).get(sym)}]" if (earnings_map or {}).get(sym) else ""

        line = (
            f"  - {sym}{penny_flag}{sentiment_flag}{earnings_flag}: ${price}, "
            f"5-day: {data.get('five_day_change_pct', 'N/A')}%, "
            f"RSI: {rsi}, "
            f"MACD hist: {macd.get('histogram', 'N/A')}, "
            f"MA20: ${mas.get('ma20', 'N/A')}, MA50: ${mas.get('ma50', 'N/A')}"
        )
        snapshot_lines.append(line)

    snapshot_text = "\n".join(snapshot_lines)

    portfolio_context = f"""Portfolio: ${portfolio_value:,.2f} total, ${account_cash:,.2f} cash, max ${max_position:,.2f} per position ({int(current_strategy['max_position_pct']*100)}%)
Strategy: {current_strategy['name']} — {current_strategy['prompt_modifier']}
Open positions: {positions_text}"""

    # ── Step 1: Broad scan — identify top opportunities ──
    macro_text = ""
    if macro:
        macro_text = f"""
## Macro Market Context
- Regime: {macro.get('market_regime', 'unknown').upper()} | SPY trend: {macro.get('spy_trend')} | Fear/VIX: {macro.get('vix_level')}
- Guidance: {macro.get('guidance', '')}
- Sector rotation: {sector_info}
- [EARNINGS:today/tomorrow] stocks: avoid or tiny position. [EARNINGS:this_week]: size conservatively.
"""

    geo_text = ""
    if geo_context:
        risk_level = geo_context.get("risk_level", "low")
        geo_score = geo_context.get("risk_score", 0)
        themes = geo_context.get("dominant_themes", [])
        headlines = geo_context.get("key_headlines", [])[:3]
        guidance = geo_context.get("forward_guidance", "")
        impact = geo_context.get("market_impact", {})
        scenarios = geo_context.get("scenarios", {})

        headlines_text = "\n".join([f"  • {h['headline']}" for h in headlines]) or "  None"
        at_risk_text = ", ".join(impact.get("sectors_at_risk", [])) or "none"
        safe_havens_text = ", ".join(impact.get("safe_havens", [])) or "GLD, TLT"
        opps_text = ", ".join(impact.get("opportunities", [])) or "none"

        geo_text = f"""
## Geopolitical & Macro Trend Analysis
- Risk level: {risk_level.upper()} (score {geo_score}/100) | Active themes: {', '.join(themes) if themes else 'none'}
- Guidance: {guidance}
- Key headlines driving markets:
{headlines_text}
- Sectors at risk: {at_risk_text}
- Safe havens: {safe_havens_text}
- Geopolitical opportunities: {opps_text}

## 1-5 Day Forward Scenarios
- BULL: {scenarios.get('bull_case', 'Positive resolution of macro concerns → rally.')}
- BASE: {scenarios.get('base_case', 'Status quo — trade on technicals.')}
- BEAR: {scenarios.get('bear_case', 'Risk-off escalation → defensive rotation.')}
"""
    if trend_forecast:
        geo_text += f"\n{trend_forecast}\n"

    step1_prompt = f"""You are an expert stock trader doing a broad market scan with full awareness of current geopolitical events and macro trends.

{portfolio_context}
{macro_text}{geo_text}
## Market Universe ({len(snapshot_lines)} stocks)
{snapshot_text}

## Indicator guide
- RSI > 70: overbought | RSI < 30: oversold
- MACD histogram positive+rising: bullish | negative+falling: bearish
- [PENNY]: stock under $5 — high risk/reward
- [NEWS:N]: mentioned in N news articles today — sentiment catalyst
- SOXL/TQQQ/SPXL are 3x leveraged ETFs — amplified moves

Scan ALL stocks above. Consider geopolitical context and forward scenarios when selecting opportunities.
Identify the TOP 3 best opportunities right now — stocks most likely to move in the next 1-5 days given BOTH technical signals AND the current macro/geopolitical backdrop.

For each, give: symbol, signal type (momentum/reversal/sentiment/breakout/geopolitical/defensive), and 1-sentence reason that incorporates current macro themes if relevant.

Respond in JSON:
{{"opportunities": [{{"symbol": "X", "signal": "momentum", "reason": "..."}}]}}"""

    try:
        step1_raw = ask_ai(step1_prompt, max_tokens=512)
        if step1_raw.startswith("```"):
            step1_raw = step1_raw.split("```")[1]
            if step1_raw.startswith("json"):
                step1_raw = step1_raw[4:]
        step1_data = json.loads(step1_raw.strip())
        opportunities = step1_data.get("opportunities", [])
        logger.info(f"Step 1 — Top opportunities: {[o['symbol'] for o in opportunities]}")
    except Exception as e:
        logger.error(f"Step 1 failed: {e}")
        opportunities = []

    if not opportunities:
        return TradeDecision(action="hold", symbol=None, quantity=None,
                           reasoning="Market scan found no clear opportunities. Holding.")

    # ── Step 2: Deep dive on top candidates ──
    opp_text = "\n".join([f"- {o['symbol']}: {o.get('signal','')} — {o.get('reason','')}"
                          for o in opportunities[:5]])

    # Fetch FULL historical data (90 days) for top candidates only
    top_symbols = [o["symbol"] for o in opportunities[:5]]
    deep_data = {}
    if full_data_fetcher and top_symbols:
        try:
            deep_data = full_data_fetcher(top_symbols)
            logger.info(f"Step 2: fetched deep data for {top_symbols}")
        except Exception as e:
            logger.warning(f"Could not fetch deep data: {e}")

    candidate_detail = []
    for opp in opportunities[:5]:
        sym = opp["symbol"]
        data = deep_data.get(sym) or market_snapshot.get(sym, {})
        closing_prices = data.get("closing_prices", [])
        indicators = compute_all(closing_prices) if closing_prices else {}
        candidate_detail.append(
            f"{sym}: price=${data.get('current_price')}, RSI={indicators.get('rsi','N/A')}, "
            f"5-day={data.get('five_day_change_pct','N/A')}%, "
            f"MA20=${indicators.get('moving_averages',{}).get('ma20','N/A')}, "
            f"MA50=${indicators.get('moving_averages',{}).get('ma50','N/A')}, "
            f"MACD hist={indicators.get('macd',{}).get('histogram','N/A')}, "
            f"news mentions={(sentiment or {}).get(sym, 0)}"
        )

    step2_prompt = f"""You are doing a deep dive on stock candidates for a trade, with full awareness of current geopolitical risks and the 1-5 day market outlook.

{portfolio_context}
{geo_text if geo_context else ""}
## Candidates identified in broad scan:
{opp_text}

## Detailed technical data for candidates:
{chr(10).join(candidate_detail)}

For each candidate, assess:
1. Signal strength (strong/medium/weak) — both technical AND macro/geopolitical alignment
2. Risk/reward ratio — account for current geopolitical risk level
3. Does it fit the {current_strategy['name']} strategy AND the current macro regime?
4. Would you trade it right now given the forward outlook (bull/base/bear scenarios above)?

Geopolitical override rules:
- If geo risk is HIGH or EXTREME: only trade inverse ETFs (SOXS, SQQQ, SPXS) or defensive safe havens; no aggressive longs
- If a candidate is in the "sectors at risk" list: reduce confidence to low
- If a candidate is in the "geopolitical opportunities" list: boost signal strength

Then pick the SINGLE BEST one to trade (or recommend hold if none are good enough).

Respond in JSON:
{{"best_symbol": "X or null", "action": "buy|sell|hold", "confidence": "high|medium|low", "quantity_suggestion": integer_or_null, "deep_analysis": "3-4 sentences on why this is the best trade now, including how geopolitical/macro context supports this decision"}}"""

    try:
        step2_raw = ask_ai(step2_prompt, max_tokens=600)
        if step2_raw.startswith("```"):
            step2_raw = step2_raw.split("```")[1]
            if step2_raw.startswith("json"):
                step2_raw = step2_raw[4:]
        step2_data = json.loads(step2_raw.strip())
        logger.info(f"Step 2 — Best pick: {step2_data.get('best_symbol')} ({step2_data.get('confidence')} confidence)")
    except Exception as e:
        logger.error(f"Step 2 failed: {e}")
        return TradeDecision(action="hold", symbol=None, quantity=None,
                           reasoning="Deep analysis failed. Holding positions.")

    best_symbol = step2_data.get("best_symbol")
    action = step2_data.get("action", "hold")
    confidence = step2_data.get("confidence", "low")
    deep_analysis = step2_data.get("deep_analysis", "")
    qty_suggestion = step2_data.get("quantity_suggestion")

    # Skip low confidence trades
    if confidence == "low" or action == "hold" or not best_symbol:
        return TradeDecision(action="hold", symbol=None, quantity=None,
                           reasoning=f"No high-confidence trade found. {deep_analysis}")

    # ── Step 3: Final decision with quantity ──
    if action == "buy" and best_symbol and account_cash > 0:
        price = market_snapshot.get(best_symbol, {}).get("current_price") or 1
        max_shares = int(max_position / price) if price > 0 else 0

        # Use suggestion from step 2, capped by position limit
        if qty_suggestion:
            final_qty = min(qty_suggestion, max_shares)
        else:
            # Size based on confidence: high=full, medium=half
            size_pct = 1.0 if confidence == "high" else 0.5
            final_qty = max(1, int(max_shares * size_pct))

        if final_qty < 1 or price * final_qty > account_cash:
            return TradeDecision(action="hold", symbol=None, quantity=None,
                               reasoning=f"Insufficient cash to buy {best_symbol}. {deep_analysis}")
    elif action == "sell" and best_symbol:
        pos = next((p for p in positions if p.symbol == best_symbol), None)
        if not pos:
            return TradeDecision(action="hold", symbol=None, quantity=None,
                               reasoning=f"No position in {best_symbol} to sell.")
        final_qty = int(pos.qty)
    else:
        return TradeDecision(action="hold", symbol=None, quantity=None, reasoning=deep_analysis)

    full_reasoning = f"[{confidence.upper()} CONFIDENCE] {deep_analysis} Candidates considered: {', '.join([o['symbol'] for o in opportunities[:3]])}."

    return TradeDecision(
        action=action,
        symbol=best_symbol,
        quantity=final_qty,
        reasoning=full_reasoning,
    )
