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
        f"  - {p.symbol} [{getattr(p, 'side', 'long').upper()}]: {p.qty} shares @ avg ${p.avg_entry_price:.2f}, "
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

    # ── Realized P&L performance summary — helps Claude avoid repeat losers ──
    performance_text = ""
    try:
        from services.db import get_trade_performance_summary
        perf = get_trade_performance_summary()
        if perf and perf.get("total_trades", 0) >= 3:
            best_syms = ", ".join([f"{s['symbol']}(+{s['avg_pct']}%)" for s in perf.get("best_symbols", [])])
            worst_syms = ", ".join([f"{s['symbol']}({s['avg_pct']}%)" for s in perf.get("worst_symbols", [])])
            performance_text = f"""
## Realized Trade Performance (your actual results — use this to improve)
- Total closed trades: {perf['total_trades']} | Win rate: {perf['win_rate_pct']}% | Total realized P&L: ${perf['total_realized_pl']:+,.2f}
- Avg win: +{perf['avg_win_pct']}% | Avg loss: {perf['avg_loss_pct']}%
- Best symbols historically: {best_syms or 'not enough data'}
- Worst symbols historically: {worst_syms or 'not enough data'}
Note: Avoid re-entering worst symbols unless fundamentals have materially changed. Increase size on best symbols when they re-appear with strong signals.
"""
    except Exception:
        pass

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

    # ── EOD Feedback: inject yesterday's learnings into today's prompts ──
    # Wrapped in try/except — if anything fails, trading continues unaffected.
    eod_step1_context = ""
    eod_step2_context = ""
    try:
        from services.eod_analysis_service import get_latest_eod_report
        eod = get_latest_eod_report()
        if eod and isinstance(eod, dict) and eod.get("analysis"):
            a = eod["analysis"]
            # Truncate fields so we never bloat the prompt unexpectedly
            key_insight = (a.get("key_insight") or "")[:300].strip()
            risk_note   = (a.get("risk_note")   or "")[:200].strip()
            watchlist   = a.get("tomorrow_watchlist") or []
            eod_date    = eod.get("date", "recent")

            if key_insight or risk_note or watchlist:
                watch_lines = "\n".join(
                    f"  • {w.get('symbol','?')}: {str(w.get('thesis',''))[:100]} [{w.get('action','watch').upper()}]"
                    for w in (watchlist[:4] if isinstance(watchlist, list) else [])
                )
                eod_step1_context = f"""
## Previous Day's Learning ({eod_date} — incorporate into today's scan)
- Key insight: {key_insight or 'N/A'}
- Risk flag: {risk_note or 'None'}
Apply these learnings: avoid patterns that failed yesterday, prioritize patterns that succeeded.
"""
                eod_step2_context = f"""
## Previous Day's Learning + Priority Watchlist ({eod_date})
- Key insight: {key_insight or 'N/A'}
- Risk flag: {risk_note or 'None'}
- Yesterday's watchlist (prioritize if signals confirm today):
{watch_lines if watch_lines else '  None'}
Factor these into your trade approvals — confirm or override based on today's technicals.
"""
                logger.info(f"EOD feedback injected: date={eod_date}, insight={len(key_insight)}c, watchlist={len(watchlist)} tickers")
    except Exception as _eod_exc:
        # Never block trading — EOD context is a bonus, not a requirement
        logger.debug(f"EOD feedback load skipped (non-fatal): {_eod_exc}")

    # ── Inject inverse ETFs on bearish days ──
    regime = (macro or {}).get("market_regime", "")
    vix = (macro or {}).get("vix_level", "low")
    is_bearish_day = regime in ("bear", "volatile", "bearish", "risk-off") or vix in ("extreme_fear", "elevated", "extreme", "high")
    bearish_etf_note = ""
    if is_bearish_day:
        bearish_etf_note = """
## Bearish / Inverse ETF Opportunities (market regime is BEARISH — PRIORITIZE these)
These ETFs profit when the market FALLS. Use them aggressively today:
  • SQQQ  — 3x inverse QQQ (tech/growth bear play)
  • SPXU  — 3x inverse S&P 500
  • SPXS  — 1x inverse S&P 500 (less volatile)
  • SOXS  — 3x inverse semiconductors
  • SDOW  — 3x inverse Dow Jones
  • TZA   — 3x inverse Russell 2000 (small-cap bear)
  • UVXY  — long VIX volatility (spikes when market panics)
Buy inverse ETFs exactly like regular stocks — they profit automatically as the index falls.
"""

    step1_prompt = f"""You are a professional equity analyst managing a paper trading portfolio. Analyze the market data below and identify the best opportunities for simulated trades. This is Alpaca paper trading — no real money involved.

{portfolio_context}
{eod_step1_context}{macro_text}{geo_text}{news_text}{trade_feedback_text}{earnings_plays_text}{bearish_etf_note}
## Market Universe ({len(snapshot_lines)} stocks)
{snapshot_text}

## Indicator guide
- RSI > 70: overbought | RSI < 30: oversold (RSI 50-70 with upward MACD = strong buy zone)
- MACD histogram positive+rising: bullish momentum | negative+falling: bearish
- [PENNY]: stock under $5 — high risk/reward, size accordingly
- [NEWS:N]: mentioned in N recent news articles — sentiment catalyst (higher = stronger signal)
- [EARNINGS:today/tomorrow]: AVOID — binary risk around earnings reports
- SOXL/TQQQ/SPXL/UPRO are 3x leveraged ETFs — suitable when market regime is bullish
- SQQQ/SPXU/SOXS/SDOW/TZA are inverse ETFs — suitable when market regime is bearish
- [SECTOR:signal:pct%]: sector momentum — BULLISH sector supports longs, BEARISH sector supports shorts
- [VOL:Nx]: relative volume — 2x+ means unusual activity, strong confirmation signal

Scan the stocks above. Identify the TOP 5 best opportunities based on technicals and macro context.
- BULLISH regime: favor longs and leveraged ETFs
- BEARISH regime: favor inverse ETFs and short candidates (RSI > 72 + bearish sector)
- NEUTRAL: balanced mix
Signal types: "momentum", "breakout", "reversal", "short_candidate", "inverse_etf", "oversold"

Return ONLY a JSON object with ONE key "opportunities". Each entry has exactly TWO fields: "symbol" and "signal". No thesis, no explanation, no extra fields, no markdown.
EXAMPLE (copy this structure exactly): {{"opportunities": [{{"symbol": "AAPL", "signal": "momentum"}}, {{"symbol": "SQQQ", "signal": "inverse_etf"}}]}}"""

    try:
        step1_raw = ask_ai(step1_prompt, max_tokens=1200)
        if step1_raw.startswith("```"):
            step1_raw = step1_raw.split("```")[1]
            if step1_raw.startswith("json"):
                step1_raw = step1_raw[4:]
        try:
            step1_data = json.loads(step1_raw.strip())
        except json.JSONDecodeError:
            import re as _re
            match = _re.search(r'\{.*\}', step1_raw, _re.DOTALL)
            if match:
                step1_data = json.loads(match.group())
            else:
                raise ValueError(f"Could not extract JSON from Step 1 response: {step1_raw[:200]}")
        opportunities = step1_data.get("opportunities", [])
        logger.info(f"Step 1 — Top opportunities: {[o['symbol'] for o in opportunities]}")
    except Exception as e:
        logger.error(f"Step 1 failed: {e}. Raw response: {step1_raw[:300] if 'step1_raw' in locals() else 'none'}")
        return [TradeDecision(action="hold", symbol=None, quantity=None,
                              reasoning=f"Market scan error (Step 1 technical failure: {str(e)[:80]}). Holding — will retry next cycle.")]

    if not opportunities:
        return [TradeDecision(action="hold", symbol=None, quantity=None,
                              reasoning="Market scan complete — no clear entry signals this cycle. All candidates in neutral territory or insufficient catalyst. Holding.")]

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
        rsi = indicators.get("rsi", "N/A")
        macd_hist = indicators.get("macd", {}).get("histogram", "N/A")
        ma20 = indicators.get("moving_averages", {}).get("ma20", "N/A")
        price = data.get("current_price")
        rel_vol = data.get("relative_volume", 1.0)
        news_count = (sentiment or {}).get(sym, 0)
        earnings_timing = (earnings_map or {}).get(sym)

        # RSI interpretation for Claude
        if rsi != "N/A":
            if rsi < 30: rsi_tag = f"RSI={rsi:.0f}[OVERSOLD]"
            elif rsi > 70: rsi_tag = f"RSI={rsi:.0f}[OVERBOUGHT]"
            else: rsi_tag = f"RSI={rsi:.0f}[NEUTRAL]"
        else:
            rsi_tag = "RSI=N/A"

        # Volume context
        vol_tag = f"RelVol={rel_vol:.1f}x"
        if rel_vol >= 2.0: vol_tag += "[HIGH]"
        elif rel_vol < 0.8: vol_tag += "[LOW]"

        # Earnings warning embedded in detail
        earnings_tag = f" ⚠️EARNINGS={earnings_timing}" if earnings_timing else ""

        line = (
            f"{sym} [{opp.get('signal','')}]{earnings_tag}: price=${price}, "
            f"{rsi_tag}, MACD={macd_hist}, MA20=${ma20}, "
            f"{vol_tag}, news={news_count}"
        )
        if intraday_summary.get(sym):
            line += f", {intraday_summary[sym]}"
        candidate_detail.append(line)

    default_tp = current_strategy.get("default_take_profit_pct", 0.10)
    default_sl = current_strategy.get("default_stop_loss_pct", 0.04)
    positions_count = len(positions)
    cash_pct = (account_cash / portfolio_value * 100) if portfolio_value > 0 else 0

    pressure_note = "\n⚠️ AFTERNOON PRESSURE: Fewer than 2 trades executed today. You MUST approve at least 1 trade now unless ALL signals are clearly negative. Idle cash by close = lost opportunity.\n" if afternoon_pressure else ""

    # ── Rotation context: assess each held position for momentum strength ──
    rotation_lines = []
    for p in positions:
        sym_data = deep_data.get(p.symbol) or market_snapshot.get(p.symbol, {})
        closing_prices = sym_data.get("closing_prices", [])
        indicators = compute_all(closing_prices) if closing_prices else {}
        rsi = indicators.get("rsi", 50)
        macd_hist = indicators.get("macd", {}).get("histogram", 0) or 0
        pl_pct = p.unrealized_pl_percent

        # Classify momentum strength
        if pl_pct > 5 and macd_hist > 0:
            momentum = "STRONG — do not rotate"
        elif pl_pct < -2 or (rsi > 70 and macd_hist < 0):
            momentum = "WEAK — rotation candidate"
        elif pl_pct < 1 and abs(macd_hist) < 0.05:
            momentum = "FLAT — rotation candidate if clearly better opp exists"
        else:
            momentum = "MODERATE — hold unless significantly better opp"

        est_value = p.current_price * float(p.qty)
        rotation_lines.append(
            f"  - {p.symbol} [{getattr(p, 'side', 'long').upper()}]: "
            f"P&L={pl_pct:+.1f}%, RSI={rsi:.0f}, MACD={macd_hist:.3f}, "
            f"value≈${est_value:,.0f} → {momentum}"
        )
    rotation_text = "\n".join(rotation_lines) if rotation_lines else "  None"

    rotation_note = f"""
## Portfolio Rotation (capital efficiency)
Available cash: ${account_cash:,.2f} ({cash_pct:.0f}% of portfolio)
Use your judgment — if a high-conviction opportunity exists but cash is insufficient, consider rotating out of a weak/flat position to fund it.

Current positions with momentum assessment:
{rotation_text}

ROTATION RULES:
- You MAY sell a weak/flat position to fund a significantly better opportunity
- Only rotate if new opportunity conviction is clearly higher than the position being sold
- NEVER rotate a STRONG momentum position
- To rotate: add a {{"symbol": "X", "action": "sell", "analysis": "rotating to fund better opp"}} BEFORE the buy in your trades array
- Sell proceeds are immediately available as cash for the next buy in the same cycle
- Max 1 rotation per cycle (1 sell + 1 buy)
"""

    step2_prompt = f"""You are building a high-performance trading portfolio that profits in ANY market direction. Evaluate EACH candidate and approve the best 1-3 trades this cycle.
{pressure_note}
{eod_step2_context}{performance_text}
{portfolio_context}
Cash available: ${account_cash:,.2f} ({cash_pct:.0f}% of portfolio) | Open positions: {positions_count}
{"⚠️ PORTFOLIO THIN — only " + str(positions_count) + " positions open. Prioritise building positions." if positions_count < 3 else ""}
{rotation_note}
{geo_text if geo_context else ""}{news_text}{bearish_etf_note}
## Candidates to evaluate:
{chr(10).join(candidate_detail)}

Strategy: {current_strategy['name']} — {current_strategy['prompt_modifier']}

For EACH candidate decide: BUY, SHORT, or SKIP.
Rules:
- Approve up to 3 trades per cycle (mix of longs and shorts)
- BUY: standard long position — profit when price rises
- SHORT: sell shares short — profit when price FALLS. Best for: RSI > 72 + bearish sector + no upcoming earnings
- SELL: close an existing long position (for rotation or profit-taking)
- Inverse ETFs (SQQQ/SPXU/SOXS/SDOW/TZA/UVXY): always use BUY action — they already profit from market falls
- In bearish regime: prioritize inverse ETF buys + individual stock shorts
- If geo risk is HIGH: only approve inverse ETFs, short sells, or safe havens
- MUST approve at least 1 trade if any candidate has medium+ signal

For LONG trades:
- take_profit_pct: realistic upside (0.08-0.40). Strong catalyst=0.15-0.25, leveraged ETF=0.20-0.35
- stop_loss_pct: trailing stop (0.03-0.08)

For SHORT trades:
- take_profit_pct: how far you expect it to FALL (0.08-0.20)
- stop_loss_pct: how much RISE to tolerate before covering (0.04-0.07)
- partial_exit: cover 50% at first target, let rest ride

Respond in JSON — only include approved trades (put any sell/rotation BEFORE the buy):
{{"trades": [{{"symbol": "X", "action": "buy|short|sell", "confidence": "high|medium|low", "quantity_suggestion": integer, "take_profit_pct": float, "stop_loss_pct": float, "partial_exit": boolean, "analysis": "2 sentences: catalyst + why long/short/sell"}}], "skipped": "brief reason"}}"""

    try:
        step2_raw = ask_ai(step2_prompt, max_tokens=1800)
        if step2_raw.startswith("```"):
            step2_raw = step2_raw.split("```")[1]
            if step2_raw.startswith("json"):
                step2_raw = step2_raw[4:]
        try:
            step2_data = json.loads(step2_raw.strip())
        except json.JSONDecodeError:
            import re as _re
            match = _re.search(r'\{.*\}', step2_raw, _re.DOTALL)
            if match:
                step2_data = json.loads(match.group())
            else:
                raise ValueError(f"Could not extract JSON from Step 2 response: {step2_raw[:200]}")
        approved = step2_data.get("trades", [])
        logger.info(f"Step 2 — Approved {len(approved)} trades: {[t.get('symbol') for t in approved]} | Skipped: {step2_data.get('skipped','')}")
    except Exception as e:
        logger.error(f"Step 2 failed: {e}. Raw: {step2_raw[:300] if 'step2_raw' in locals() else 'none'}")
        return [TradeDecision(action="hold", symbol=None, quantity=None,
                              reasoning="Deep analysis failed. Holding positions.")]

    if not approved:
        return [TradeDecision(action="hold", symbol=None, quantity=None,
                              reasoning=f"No trades approved this cycle. Candidates: {', '.join(top_symbols)}")]

    # ── Step 3: Convert approved list into TradeDecision objects ──
    decisions = []
    remaining_cash = account_cash
    sectors_bought = []

    # Sort sells first so rotation proceeds are added to remaining_cash
    # before any subsequent buy checks affordability — regardless of Claude's output order
    _step3_priority = {"sell": 0, "buy": 1, "short": 1, "hold": 2}
    approved = sorted(approved, key=lambda t: _step3_priority.get(t.get("action", "hold"), 2))

    for trade in approved[:3]:  # max 3 per cycle
        sym = trade.get("symbol")
        action = trade.get("action", "hold")
        confidence = trade.get("confidence", "medium")
        qty_suggestion = trade.get("quantity_suggestion")

        # Sanitize pct fields — Claude occasionally returns "15%" (string with %) or null.
        # A raw float("15%") raises ValueError which would crash the entire loop and
        # skip ALL remaining trades. Strip % and fall back to strategy defaults safely.
        def _safe_pct(val, default):
            try:
                # Bug fix: use `is not None` not truthiness — val=0 is a valid float
                # and `if val` would wrongly treat 0% as missing and return the default.
                return float(str(val).replace("%", "").strip()) if val is not None else default
            except (ValueError, TypeError):
                return default

        take_profit_pct = max(0.05, min(_safe_pct(trade.get("take_profit_pct"), default_tp), 0.40))
        stop_loss_pct   = max(0.02, min(_safe_pct(trade.get("stop_loss_pct"),   default_sl), 0.10))
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
            sector_cap = current_strategy.get("sector_cap", 1)
            existing_sector_count = sum(1 for s in sectors_bought if s == sym_sector)
            held_in_sector = [p.symbol for p in positions if get_sector_for_symbol(p.symbol) == sym_sector]
            if existing_sector_count >= sector_cap and sym_sector not in ("Unknown", "Broad"):
                logger.info(f"Skipping {sym} — already buying {existing_sector_count} {sym_sector} stocks this cycle (cap={sector_cap})")
                continue
            if len(held_in_sector) >= sector_cap and sym_sector not in ("Unknown", "Broad"):
                logger.info(f"Skipping {sym} — already hold {held_in_sector} in {sym_sector} (cap={sector_cap})")
                continue
            sectors_bought.append(sym_sector)
        except Exception:
            pass

        if action in ("buy", "short"):
            price = (deep_data.get(sym) or market_snapshot.get(sym, {})).get("current_price") or 0
            if price <= 0:
                continue
            # Apply penny stock cap in Step 3 so cash reservation is correctly sized.
            # trading_engine.py applies the same cap for final ATR-adjusted sizing.
            if action == "buy" and price < 5.0:
                from services import trading_engine as _te
                _penny_pct = float(_te._risk_settings.get("max_penny_position_pct", 3.0)) / 100.0
                effective_max_position = portfolio_value * _penny_pct
            else:
                effective_max_position = max_position
            # Cap max_shares to what's actually affordable from remaining cash.
            # Without this cap the bot computes max_shares from the strategy max_position
            # (e.g. 20% of $30k = $6,000 → 78 TQQQ shares) and then skips the trade
            # entirely when cash is only $1,000 — even though 13 affordable shares exist.
            max_shares_by_strategy = int(effective_max_position / price)
            max_shares_by_cash     = int(remaining_cash / price) if action == "buy" else max_shares_by_strategy
            max_shares = min(max_shares_by_strategy, max_shares_by_cash)

            is_aggressive = current_strategy.get("key") == "aggressive"
            if qty_suggestion:
                final_qty = min(int(qty_suggestion), max_shares)
            else:
                size_pct = 1.0 if confidence == "high" else (0.75 if is_aggressive else 0.5)
                final_qty = max(1, int(max_shares * size_pct))

            cost = price * final_qty
            if action == "buy" and final_qty < 1:
                logger.info(f"Skipping {sym} — insufficient cash (have ${remaining_cash:.0f}, price ${price:.2f})")
                continue
            if action == "buy":
                remaining_cash -= cost
            # Shorts don't consume cash directly (margin), but we still need buying power

        elif action == "sell":
            pos = next((p for p in positions if p.symbol == sym and p.side == "long"), None)
            if not pos:
                # Bug fix: log warning instead of silent skip — helps diagnose cases
                # where Claude issues a sell on a short position or a stale symbol.
                logger.warning(f"Sell for {sym} skipped — no long position found (held as short or already closed)")
                continue
            final_qty = max(1, round(float(pos.qty)))
            # Add estimated proceeds to remaining_cash so a subsequent buy in
            # the same cycle can use the freed capital (rotation).
            # Use 90% of proceeds as a safety buffer — Alpaca settles T+1 so the
            # full amount isn't immediately available, which causes negative cash
            # if the bot buys the full rotated amount in the same cycle.
            price = (deep_data.get(sym) or market_snapshot.get(sym, {})).get("current_price") or pos.current_price
            proceeds = price * final_qty
            # 80% buffer (down from 90%): bracket orders reserve funds for both the
            # buy leg and stop-loss leg simultaneously, so the effective available
            # cash is lower than the raw sell proceeds. 80% prevents negative cash
            # when the bot rotates (sell + buy) within the same cycle.
            buffered_proceeds = proceeds * 0.80
            remaining_cash += buffered_proceeds
            logger.info(f"Rotation sell: {sym} x{final_qty} @ ${price:.2f} → +${proceeds:,.0f} gross / +${buffered_proceeds:,.0f} usable (80% buffer) → remaining cash ${remaining_cash:,.0f}")
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
