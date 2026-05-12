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
    news_headlines: list = None,
    full_data_fetcher=None,
    sector_context: dict = None,      # {symbol: {"sector": str, "sector_pct": float, "sector_signal": str}}
    recent_trades: list = None,        # last 10 AI decisions from DB
    earnings_plays: list = None,       # pre-earnings play candidates
    afternoon_pressure: bool = False,  # True if < 2 trades by 2 PM — lower bar
) -> list:
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

        rel_vol = data.get("relative_volume", 1.0)
        vol_flag = f" [VOL:{rel_vol:.1f}x]" if rel_vol >= 1.5 else ""

        line = (
            f"  - {sym}{penny_flag}{sentiment_flag}{earnings_flag}: ${price}, "
            f"5-day: {data.get('five_day_change_pct', 'N/A')}%, "
            f"RSI: {rsi}, "
            f"MACD hist: {macd.get('histogram', 'N/A')}, "
            f"MA20: ${mas.get('ma20', 'N/A')}, MA50: ${mas.get('ma50', 'N/A')}"
            f"{vol_flag}"
        )

        if sector_context and sym in sector_context:
            sc = sector_context[sym]
            sector_tag = f" [{sc['sector']}:{sc['sector_signal'].upper()}:{sc['sector_pct']:+.1f}%]"
            line = line + sector_tag

        snapshot_lines.append(line)

    snapshot_text = "\n".join(snapshot_lines)

    portfolio_context = f"""Portfolio: ${portfolio_value:,.2f} total, ${account_cash:,.2f} cash, max ${max_position:,.2f} per position ({int(current_strategy['max_position_pct']*100)}%)
Strategy: {current_strategy['name']} — {current_strategy['prompt_modifier']}
Open positions: {positions_text}"""

    # ── Trade feedback: last 10 decisions ──
    trade_feedback_text = ""
    if recent_trades:
        lines = []
        for t in recent_trades[:10]:
            action = t.get("action", "hold")
            sym = t.get("symbol", "N/A")
            conf = t.get("confidence", "?")
            ts = t.get("timestamp", "")[:10]  # just the date
            reasoning_short = (t.get("reasoning") or "")[:80]
            lines.append(f"  • {ts} {action.upper()} {sym} [{conf}]: {reasoning_short}")
        trade_feedback_text = f"""
## Your Recent Trade Decisions (learn from these)
{chr(10).join(lines)}
Note: Use this history to avoid repeating losing setups and to identify what's been working.
"""

    earnings_plays_text = ""
    if earnings_plays:
        lines = [f"  • {p['symbol']}: {p['reason']}" for p in earnings_plays]
        earnings_plays_text = f"""
## Pre-Earnings Play Opportunities (small positions, high reward)
{chr(10).join(lines)}
These stocks have earnings coming this week. Consider a small position (5% max) for the pre-earnings run-up. SELL before the actual report.
"""

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

    news_text = ""
    if news_headlines:
        headlines_formatted = "\n".join([f"  • {h}" for h in news_headlines])
        news_text = f"""
## Breaking News & Market Catalysts (live multi-source feed)
{headlines_formatted}
"""

    step1_prompt = f"""You are an expert stock trader and portfolio manager executing an AGGRESSIVE growth strategy. Your mandate is to find the best trade RIGHT NOW.

{portfolio_context}
{macro_text}{geo_text}{news_text}{trade_feedback_text}{earnings_plays_text}
## Market Universe ({len(snapshot_lines)} stocks)
{snapshot_text}

## Indicator guide
- RSI > 70: overbought | RSI < 30: oversold (RSI 50-70 with upward MACD = strong buy zone)
- MACD histogram positive+rising: bullish momentum | negative+falling: bearish
- [PENNY]: stock under $5 — high risk/reward, size accordingly
- [NEWS:N]: mentioned in N recent news articles — sentiment catalyst (higher = stronger signal)
- [EARNINGS:today/tomorrow]: avoid — binary risk; [EARNINGS:this_week]: small position only
- SOXL/TQQQ/SPXL/UPRO are 3x leveraged ETFs — use when market regime is bullish
- [SECTOR:signal:pct%]: sector momentum — BULLISH sector boosts conviction, BEARISH reduces it
- [VOL:Nx]: relative volume — 2x+ means unusual activity, strong confirmation signal

Scan ALL stocks above. Use ALL signals: technicals, news catalysts, momentum, macro alignment.
Identify the TOP 5 best opportunities right now — stocks most likely to move in the next 1-5 days.
Prioritize: strong news catalysts + technical breakout + macro tailwind (highest conviction).
Boost conviction when: [VOL:2x+] + bullish sector + strong technical signal = highest priority. Reduce conviction when sector is BEARISH even if individual stock looks good.
You MUST find opportunities — "hold" is only acceptable if EVERY signal is negative across the board.

Return ONLY symbol and signal type — no descriptions, no explanations. The detailed analysis happens in the next step.

Respond in JSON with ONLY these two fields per entry:
{{"opportunities": [{{"symbol": "X", "signal": "momentum"}}]}}"""

    try:
        step1_raw = ask_ai(step1_prompt, max_tokens=256)
        if step1_raw.startswith("```"):
            step1_raw = step1_raw.split("```")[1]
            if step1_raw.startswith("json"):
                step1_raw = raw = step1_raw[4:]
        step1_data = json.loads(step1_raw.strip())
        opportunities = step1_data.get("opportunities", [])
        logger.info(f"Step 1 — Top opportunities: {[o['symbol'] for o in opportunities]}")
    except Exception as e:
        logger.error(f"Step 1 failed: {e}. Raw response: {step1_raw[:300] if 'step1_raw' in locals() else 'none'}")
        opportunities = []

    if not opportunities:
        return TradeDecision(action="hold", symbol=None, quantity=None,
                           reasoning="Market scan found no clear opportunities. Holding.")

    # ── Step 2: Deep dive — evaluate ALL candidates, approve up to 3 trades ──
    top_symbols = [o["symbol"] for o in opportunities[:5]]

    # Fetch full historical data for all candidates
    deep_data = {}
    if full_data_fetcher and top_symbols:
        try:
            deep_data = full_data_fetcher(top_symbols)
            logger.info(f"Step 2: fetched deep data for {top_symbols}")
        except Exception as e:
            logger.warning(f"Could not fetch deep data: {e}")

    # 15-min intraday bars for trend confirmation
    intraday_summary = {}
    try:
        from services import alpaca_service as _alpaca_svc
        intraday_data = _alpaca_svc.get_intraday_bars(top_symbols, lookback_bars=8)
        for sym in top_symbols:
            bars = intraday_data.get(sym, [])
            if len(bars) >= 3:
                closes = [b["close"] for b in bars]
                recent_trend = "up" if closes[-1] > closes[-3] else "down"
                vol_now = bars[-1]["volume"] if bars else 0
                intraday_summary[sym] = f"15min trend:{recent_trend}, vol:{vol_now:,}"
    except Exception as e:
        logger.warning(f"Intraday bars failed (non-fatal): {e}")

    candidate_detail = []
    for opp in opportunities[:5]:
        sym = opp["symbol"]
        data = deep_data.get(sym) or market_snapshot.get(sym, {})
        closing_prices = data.get("closing_prices", [])
        indicators = compute_all(closing_prices) if closing_prices else {}
        line = (
            f"{sym} [{opp.get('signal','')}]: price=${data.get('current_price')}, "
            f"RSI={indicators.get('rsi','N/A')}, 5d={data.get('five_day_change_pct','N/A')}%, "
            f"MA20=${indicators.get('moving_averages',{}).get('ma20','N/A')}, "
            f"MACD={indicators.get('macd',{}).get('histogram','N/A')}, "
            f"news={(sentiment or {}).get(sym, 0)}"
        )
        if intraday_summary.get(sym):
            line += f", {intraday_summary[sym]}"
        candidate_detail.append(line)

    default_tp = current_strategy.get("default_take_profit_pct", 0.10)
    default_sl = current_strategy.get("default_stop_loss_pct", 0.04)
    positions_count = len(positions)
    cash_pct = (account_cash / portfolio_value * 100) if portfolio_value > 0 else 0

    pressure_note = "\n⚠️ AFTERNOON PRESSURE: Fewer than 2 trades executed today. You MUST approve at least 1 trade now unless ALL signals are clearly negative. Idle cash by close = lost opportunity.\n" if afternoon_pressure else ""

    step2_prompt = f"""You are building a high-performance trading portfolio. Evaluate EACH candidate independently and approve the best 1-3 trades this cycle.
{pressure_note}

{portfolio_context}
Cash available: ${account_cash:,.2f} ({cash_pct:.0f}% of portfolio) | Open positions: {positions_count}
{"⚠️ PORTFOLIO THIN — only {positions_count} positions open. Prioritise building positions." if positions_count < 3 else ""}
{geo_text if geo_context else ""}{news_text}
## Candidates to evaluate:
{chr(10).join(candidate_detail)}

Strategy: {current_strategy['name']} — {current_strategy['prompt_modifier']}

For EACH candidate decide: BUY, SELL, or SKIP.
Rules:
- Approve up to 3 buys per cycle — we WANT a diversified portfolio working simultaneously
- Never approve 2 stocks from the same sector unless both are very high conviction
- Each approved trade must stand on its own merit
- If geo risk is HIGH: only approve inverse ETFs or safe havens
- MUST approve at least 1 trade if any candidate has medium+ signal

Exit rules per approved trade:
- take_profit_pct: realistic upside (0.08-0.40). Strong catalyst=0.15-0.25, ETF=0.15-0.30
- stop_loss_pct: trailing stop (0.03-0.08). High conviction=0.05-0.07, volatile=0.06-0.08
- partial_exit: true when upside ≥ 15% (sell half at target, let half compound)

Respond in JSON — only include approved trades (skip = omit from list):
{{"trades": [{{"symbol": "X", "action": "buy|sell", "confidence": "high|medium", "quantity_suggestion": integer, "take_profit_pct": float, "stop_loss_pct": float, "partial_exit": boolean, "analysis": "2 sentences: catalyst + upside target"}}], "skipped": "brief reason why other candidates were skipped"}}"""

    try:
        step2_raw = ask_ai(step2_prompt, max_tokens=1000)
        if step2_raw.startswith("```"):
            step2_raw = step2_raw.split("```")[1]
            if step2_raw.startswith("json"):
                step2_raw = step2_raw[4:]
        step2_data = json.loads(step2_raw.strip())
        approved = step2_data.get("trades", [])
        logger.info(f"Step 2 — Approved {len(approved)} trades: {[t.get('symbol') for t in approved]} | Skipped: {step2_data.get('skipped','')}")
    except Exception as e:
        logger.error(f"Step 2 failed: {e}")
        return [TradeDecision(action="hold", symbol=None, quantity=None,
                              reasoning="Deep analysis failed. Holding positions.")]

    if not approved:
        return [TradeDecision(action="hold", symbol=None, quantity=None,
                              reasoning=f"No trades approved this cycle. Candidates: {', '.join(top_symbols)}")]

    # ── Step 3: Convert approved list into TradeDecision objects ──
    decisions = []
    remaining_cash = account_cash
    sectors_bought = []

    for trade in approved[:3]:  # max 3 per cycle
        sym = trade.get("symbol")
        action = trade.get("action", "hold")
        confidence = trade.get("confidence", "medium")
        qty_suggestion = trade.get("quantity_suggestion")
        take_profit_pct = max(0.05, min(float(trade.get("take_profit_pct") or default_tp), 0.40))
        stop_loss_pct = max(0.02, min(float(trade.get("stop_loss_pct") or default_sl), 0.10))
        partial_exit = bool(trade.get("partial_exit", False))
        analysis = trade.get("analysis", "")

        if not sym or action == "hold":
            continue

        # Confidence gate
        min_confidence = current_strategy.get("min_confidence", "medium")
        confidence_rank = {"high": 2, "medium": 1, "low": 0}
        if confidence_rank.get(confidence, 0) < confidence_rank.get(min_confidence, 1):
            logger.info(f"Skipping {sym} — confidence {confidence} below strategy minimum {min_confidence}")
            continue

        # Sector correlation check
        try:
            from services.sector_momentum import get_sector_for_symbol
            sym_sector = get_sector_for_symbol(sym)
            existing_sector_count = sum(1 for s in sectors_bought if s == sym_sector)
            held_in_sector = [p.symbol for p in positions if get_sector_for_symbol(p.symbol) == sym_sector]
            if existing_sector_count >= 1 and sym_sector not in ("Unknown", "Broad"):
                logger.info(f"Skipping {sym} — already buying another {sym_sector} stock this cycle")
                continue
            if len(held_in_sector) >= 2 and sym_sector not in ("Unknown", "Broad"):
                logger.info(f"Skipping {sym} — already hold {held_in_sector} in {sym_sector}")
                continue
            sectors_bought.append(sym_sector)
        except Exception:
            pass

        if action == "buy":
            price = (deep_data.get(sym) or market_snapshot.get(sym, {})).get("current_price") or 0
            if price <= 0:
                continue
            max_shares = int(max_position / price)
            is_aggressive = current_strategy.get("key") == "aggressive"
            if qty_suggestion:
                final_qty = min(int(qty_suggestion), max_shares)
            else:
                size_pct = 1.0 if confidence == "high" else (0.75 if is_aggressive else 0.5)
                final_qty = max(1, int(max_shares * size_pct))

            cost = price * final_qty
            if final_qty < 1 or cost > remaining_cash:
                logger.info(f"Skipping {sym} — insufficient cash (need ${cost:.0f}, have ${remaining_cash:.0f})")
                continue
            remaining_cash -= cost

        elif action == "sell":
            pos = next((p for p in positions if p.symbol == sym), None)
            if not pos:
                continue
            final_qty = max(1, round(float(pos.qty)))
        else:
            continue

        reasoning = (
            f"[{confidence.upper()}] {analysis} "
            f"TP={take_profit_pct*100:.0f}% | SL={stop_loss_pct*100:.0f}%"
            f"{' | partial exit' if partial_exit else ''}."
        )
        decisions.append(TradeDecision(
            action=action,
            symbol=sym,
            quantity=final_qty,
            reasoning=reasoning,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
            partial_exit=partial_exit,
        ))
        logger.info(f"Approved: {action.upper()} {sym} x{final_qty} | TP={take_profit_pct*100:.0f}% SL={stop_loss_pct*100:.0f}%")

    if not decisions:
        return [TradeDecision(action="hold", symbol=None, quantity=None,
                              reasoning=f"All approved trades filtered out by risk/cash checks. Candidates: {', '.join(top_symbols)}")]
    return decisions
