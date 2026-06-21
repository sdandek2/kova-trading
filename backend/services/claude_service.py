import json
import logging
from datetime import datetime, timezone

from models.trade import TradeDecision
from services.indicators import compute_all
from services import strategy as strategy_service
from services.ai_client import ask_ai, ask_ai_pro, parse_ai_json

logger = logging.getLogger(__name__)

from services.connector_health import track_api

def _get_watchlist() -> list[str]:
    from routers.watchlist import load_watchlist
    return load_watchlist()


def predict_earnings_direction(
    symbol: str,
    snapshot_data: dict,
    sentiment: dict = None,
    news_headlines: list = None,
) -> dict:
    """
    Make a directional prediction for a stock reporting earnings today/tomorrow.
    Returns {"direction": "bullish"|"bearish"|"uncertain", "confidence": "high"|"medium"|"low", "reasoning": str}

    Signals used:
    - 5-day price drift (smart money positioning before report)
    - RSI: >70 = priced for perfection (miss → big drop), <40 = beaten down (beat → squeeze)
    - Relative volume: 2x+ into earnings = conviction; <0.8x = lack of interest
    - News sentiment: positive headlines = analyst upgrades, guidance raises
    """
    closing_prices = snapshot_data.get("closing_prices", [])
    indicators = compute_all(closing_prices) if closing_prices else {}
    rsi = indicators.get("rsi", "N/A")
    macd_hist = indicators.get("macd", {}).get("histogram", "N/A")
    mas = indicators.get("moving_averages", {})
    price = snapshot_data.get("current_price", "N/A")
    five_day = snapshot_data.get("five_day_change_pct", "N/A")
    rel_vol = snapshot_data.get("relative_volume", 1.0)
    news_count = (sentiment or {}).get(symbol, 0)

    relevant_headlines = ""
    if news_headlines:
        matches = [h for h in news_headlines if symbol.lower() in h.lower()][:5]
        if matches:
            relevant_headlines = "\n".join(f"  • {h}" for h in matches)

    prompt = f"""Predict whether {symbol} will react POSITIVELY or NEGATIVELY to its earnings report releasing today/tomorrow.

Signals:
- Price: ${price} | 5-day change: {five_day}%
- RSI: {rsi} | MACD histogram: {macd_hist}
- MA20: ${mas.get('ma20', 'N/A')} | MA50: ${mas.get('ma50', 'N/A')}
- Relative volume: {rel_vol:.1f}x | News mentions: {news_count}
{f'- Headlines:{chr(10)}{relevant_headlines}' if relevant_headlines else ''}

Interpretation guide:
- Stock up 5-15% past week + high volume = market expects a beat → bullish lean
- Stock falling + low volume = fear of miss → bearish lean
- RSI > 70 into earnings = priced for perfection, any miss = large drop → bearish risk
- RSI < 40 into earnings = beaten down, any beat = short squeeze → bullish risk
- Strong positive news (upgrades, guidance raises) → bullish
- Negative news (guidance cuts, sector headwinds, lawsuits) → bearish
- Mixed or insufficient signals → uncertain

Return ONLY valid JSON, no markdown. direction must be one of: bullish, bearish, uncertain.
{{"direction": "<bullish|bearish|uncertain>", "confidence": "<high|medium|low>", "reasoning": "one sentence"}}

Only return bullish/bearish if there is CLEAR directional evidence. Default to uncertain if signals conflict."""

    try:
        raw = ask_ai(prompt, max_tokens=300)
        result = parse_ai_json(raw)
        direction = result.get("direction", "uncertain")
        confidence = result.get("confidence", "low")
        reasoning = result.get("reasoning", "")
        if direction not in ("bullish", "bearish", "uncertain"):
            direction = "uncertain"
        logger.info(f"Earnings prediction {symbol}: {direction} [{confidence}] — {reasoning}")
        return {"direction": direction, "confidence": confidence, "reasoning": reasoning}
    except Exception as e:
        logger.warning(f"Earnings prediction failed for {symbol} (defaulting uncertain): {e}")
        return {"direction": "uncertain", "confidence": "low", "reasoning": "prediction failed"}


@track_api("claude_ai")
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
    sector_context: dict = None,
    recent_trades: list = None,
    earnings_plays: list = None,
    afternoon_pressure: bool = False,
    rejected_symbols: list = None,
    prebreakout_candidates: list = None,
    brain_regime=None,       # RegimeResult from brain/regime.py
    rs_map: dict = None,     # {symbol: RSScore} from brain/rs_ranking.py
    kelly_history: list = None,  # closed trade history for Kelly sizing
) -> list:
    # ── Load prompt override (injected into both steps below) ───────────────
    _prompt_override = ""
    try:
        from services.db import get_setting as _gs
        _prompt_override = (_gs("prompt_override") or "").strip()
    except Exception:
        pass

    # ── Trading budget cap ──────────────────────────────────────────────────
    # If a budget is set (e.g. $2,000), the bot sizes as if the portfolio is
    # only that amount — the rest of the account sits untouched.
    # portfolio_value and account_cash are still passed in as real values;
    # we only override the effective values used for sizing here.
    try:
        from services.db import get_trading_budget as _get_budget
        _budget = _get_budget()
        if _budget and _budget > 0:
            effective_portfolio = min(portfolio_value, _budget)
            effective_cash = min(account_cash, _budget)
            logger.debug(f"Trading budget active: ${_budget:,.2f} — sizing off ${effective_portfolio:,.2f} (real portfolio: ${portfolio_value:,.2f})")
        else:
            effective_portfolio = portfolio_value
            effective_cash = account_cash
    except Exception:
        effective_portfolio = portfolio_value
        effective_cash = account_cash

    current_strategy = strategy_service.get_strategy()
    max_position = effective_portfolio * current_strategy["max_position_pct"]

    positions_text = "\n".join([
        f"  - {p.symbol} [{getattr(p, 'side', 'long').upper()}]: {p.qty} shares @ avg ${p.avg_entry_price:.2f}, "
        f"current ${p.current_price:.2f}, P&L: ${p.unrealized_pl:.2f} ({p.unrealized_pl_percent:.1f}%)"
        + (" ⚠️ SHORT — system-managed, do NOT issue sell or buy on this" if getattr(p, "side", "long") == "short" else "")
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

    budget_note = f" [BUDGET CAP: ${effective_portfolio:,.2f}]" if effective_portfolio < portfolio_value else ""
    portfolio_context = f"""Portfolio: ${effective_portfolio:,.2f} total{budget_note}, ${effective_cash:,.2f} cash, max ${max_position:,.2f} per position ({int(current_strategy['max_position_pct']*100)}%)
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

    # ── Macro context — informational only, no direction override ──
    regime = (macro or {}).get("market_regime", "")
    regime_direction_note = ""
    bearish_etf_note = ""

    # ── Brain regime note — override macro with brain's more precise classification ──
    brain_regime_note = ""
    if brain_regime:
        lev_status = "ALLOWED" if brain_regime.allows_leveraged_etfs else "BLOCKED (regime not bull or VIX elevated)"
        brain_regime_note = f"""
## Brain Regime Analysis (authoritative — use this, not macro regime)
- Regime: {brain_regime.regime.upper()} (confidence={brain_regime.confidence:.0%}, score={brain_regime.score})
- VIX: {brain_regime.vix_level.upper()} | SPY trend: {brain_regime.spy_trend} | Breadth: {brain_regime.breadth_pct:.0f}%
- Leveraged ETFs (SOXL/TQQQ/SPXL): {lev_status}
- {brain_regime.notes}
"""

    # ── RS ranking note — pre-filter the universe ──
    rs_note = ""
    if rs_map:
        top_rs = sorted(rs_map.values(), key=lambda x: x.rs_score, reverse=True)[:5]
        bottom_rs = sorted(rs_map.values(), key=lambda x: x.rs_score)[:3]
        top_str = ", ".join(f"{r.symbol}({r.rs_score:+.1f},p{r.percentile:.0f})" for r in top_rs)
        bot_str = ", ".join(f"{r.symbol}({r.rs_score:+.1f})" for r in bottom_rs)
        rs_note = f"""
## Relative Strength Rankings (vs SPY — only trade top 60th percentile)
- Top RS stocks (outperforming market): {top_str}
- Weak RS stocks (AVOID — underperforming market): {bot_str}
- [RS:XX] tag below = percentile rank. Only nominate stocks with RS percentile ≥ 60.
"""

    # Build rejected symbols note for Step 1
    rejected_note = ""
    if rejected_symbols:
        rejected_note = f"\n⛔ DO NOT nominate these symbols — currently in rejection cooldown (overextended/overbought): {', '.join(rejected_symbols)}\nFind different opportunities instead.\n"

    # Pre-breakout candidates — inject at top of Step 1 so Claude sees them first
    prebreakout_note = ""
    if prebreakout_candidates:
        from services.breakout_scanner import format_for_prompt as _fmt_breakout
        prebreakout_note = "\n" + _fmt_breakout(prebreakout_candidates) + "\n"

    # Annotate snapshot with RS percentile tags
    if rs_map:
        annotated_lines = []
        for line in snapshot_lines:
            sym = line.strip().lstrip("- ").split(" ")[0].rstrip(":")
            rs = rs_map.get(sym)
            if rs:
                tag = f" [RS:{rs.percentile:.0f}]" if rs.is_tradeable else f" [RS:{rs.percentile:.0f}:WEAK]"
                annotated_lines.append(line + tag)
            else:
                annotated_lines.append(line)
        snapshot_text_final = "\n".join(annotated_lines)
    else:
        snapshot_text_final = snapshot_text

    step1_prompt = f"""You are a professional equity analyst managing a real portfolio. Analyze the market data below and identify the best opportunities for trades.

{portfolio_context}
{brain_regime_note}{rs_note}{prebreakout_note}{rejected_note}{eod_step1_context}{macro_text}{geo_text}{news_text}{trade_feedback_text}{earnings_plays_text}{bearish_etf_note}
## Market Universe ({len(snapshot_lines)} stocks)
{snapshot_text_final}

## Indicator guide
- RSI > 70: overbought | RSI < 30: oversold (RSI 50-70 with upward MACD = strong buy zone)
- MACD histogram positive+rising: bullish momentum | negative+falling: bearish
- [RS:XX]: relative strength percentile vs SPY — ONLY nominate stocks with RS ≥ 60. [RS:XX:WEAK] = skip.
- [PENNY]: stock under $5 — high risk/reward, size accordingly
- [NEWS:N]: mentioned in N recent news articles — sentiment catalyst (higher = stronger signal)
- [EARNINGS:today/tomorrow]: AVOID — binary risk around earnings reports
- SOXL/TQQQ/SPXL/UPRO are 3x leveraged ETFs — only suitable when Brain Regime = BULL and VIX is low/normal
- SQQQ/SPXU/SOXS/SDOW/TZA are inverse ETFs — suitable when Brain Regime = BEAR or CHOP
- [SECTOR:signal:pct%]: sector momentum — BULLISH sector supports longs, BEARISH sector supports shorts
- [VOL:Nx]: relative volume — 2x+ means unusual activity, strong confirmation signal

Scan the stocks above. Identify the TOP 7 best opportunities based on technicals and Brain Regime.
- BULL regime: favor longs and leveraged ETFs (only if VIX allowed)
- BEAR regime: favor inverse ETFs and short candidates (RSI > 65 + bearish sector)
- CHOP regime: favor mean reversion (oversold quality stocks) and inverse ETFs — avoid momentum longs
- short_candidate: ONLY nominate when RSI > 65 AND MACD histogram < 0.5
Signal types: "momentum", "breakout", "reversal", "short_candidate", "inverse_etf", "oversold"

Return ONLY a JSON object with ONE key "opportunities". Each entry has exactly TWO fields: "symbol" and "signal". No thesis, no explanation, no extra fields, no markdown.
EXAMPLE (copy this structure exactly): {{"opportunities": [{{"symbol": "AAPL", "signal": "momentum"}}, {{"symbol": "SQQQ", "signal": "inverse_etf"}}]}}"""

    if _prompt_override:
        step1_prompt += f"\n\n## Operator Override Instructions (follow these today)\n{_prompt_override}"

    try:
        step1_raw = ask_ai(step1_prompt, max_tokens=1200)
        step1_data = parse_ai_json(step1_raw)
        raw_opps = step1_data.get("opportunities", [])
        # Validate: only keep entries that have a string symbol — drop malformed entries
        opportunities = [o for o in raw_opps if isinstance(o, dict) and isinstance(o.get("symbol"), str) and o["symbol"]]
        logger.info(f"Step 1 — Top opportunities: {[o['symbol'] for o in opportunities]}")
    except Exception as e:
        logger.error(f"Step 1 failed: {e}. Raw response: {step1_raw[:300] if 'step1_raw' in locals() else 'none'}")
        return [TradeDecision(action="hold", symbol=None, quantity=None,
                              reasoning=f"Market scan error (Step 1 technical failure: {str(e)[:80]}). Holding — will retry next cycle.")]

    if not opportunities:
        return [TradeDecision(action="hold", symbol=None, quantity=None,
                              reasoning="Market scan complete — no clear entry signals this cycle. All candidates in neutral territory or insufficient catalyst. Holding.")]

    # ── Step 2: Deep dive — evaluate ALL candidates, approve up to 3 trades ──
    top_symbols = [o["symbol"] for o in opportunities[:7]]

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
    for opp in opportunities[:7]:
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
    cash_pct = (effective_cash / effective_portfolio * 100) if effective_portfolio > 0 else 0

    pressure_note = "\n📊 AFTERNOON NOTE: Fewer than 2 trades executed today. Lower your bar slightly — consider medium-confidence setups you might otherwise skip, but only if the signal is genuinely present. Don't force a trade on a poor setup.\n" if afternoon_pressure else ""

    # ── Rotation context: assess each held position for momentum strength ──
    # SHORT positions are excluded — they cannot be closed via SELL action,
    # only via the engine's cover logic. Including them causes Claude to waste
    # a decision slot on a sell that will always be rejected.
    rotation_lines = []
    for p in positions:
        if getattr(p, "side", "long") == "short":
            continue  # shorts managed by engine, not Claude rotation
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
Available cash: ${effective_cash:,.2f} ({cash_pct:.0f}% of portfolio)
Use your judgment — if a high-conviction opportunity exists but cash is insufficient, consider rotating out of a weak/flat position to fund it.

Current positions with momentum assessment:
{rotation_text}

ROTATION RULES:
- You MAY sell a weak/flat position to fund a significantly better opportunity
- Only rotate if new opportunity conviction is clearly higher than the position being sold
- NEVER rotate a STRONG momentum position
- SELL action is ONLY valid for [LONG] positions — NEVER issue sell on a [SHORT] position (shorts are covered automatically by the system, not via sell)
- To rotate: add a {{"symbol": "X", "action": "sell", "analysis": "rotating to fund better opp"}} BEFORE the buy in your trades array
- Sell proceeds are immediately available as cash for the next buy in the same cycle
- Max 1 rotation per cycle (1 sell + 1 buy)
"""

    step2_prompt = f"""You are building a high-performance trading portfolio that profits in ANY market direction. Evaluate EACH candidate and approve the best 1-3 trades this cycle.
{pressure_note}{regime_direction_note}
{eod_step2_context}{performance_text}
{portfolio_context}
Cash available: ${effective_cash:,.2f} ({cash_pct:.0f}% of portfolio) | Open positions: {positions_count}
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
- SHORT: sell shares short — profit when price FALLS. Requires: RSI > 65 AND MACD histogram < 0.5. Ideal: RSI > 72 + MACD negative + bearish sector + no upcoming earnings. SKIP if RSI ≤ 65 or MACD ≥ 0.5 — the system will reject it and waste the slot
- SELL: close an existing long position (for rotation or profit-taking)
- Inverse ETFs (SQQQ/SPXU/SOXS/SDOW/TZA/UVXY): always use BUY action — they already profit from market falls
- In bearish regime: prioritize inverse ETF buys + individual stock shorts
- If geo risk is HIGH: only approve inverse ETFs, short sells, or safe havens
- MUST approve at least 1 trade if any candidate has medium+ signal

For LONG trades:
- take_profit_pct: realistic upside. Leveraged ETF (TQQQ/SOXL/SPXL)=0.30-0.80 on bull days, strong catalyst=0.20-0.40, normal stock=0.10-0.30. Let winners run — don't cap early.
- stop_loss_pct: trailing stop (0.03-0.08)

For SHORT trades:
- take_profit_pct: how far you expect it to FALL (0.08-0.20)
- stop_loss_pct: how much RISE to tolerate before covering (0.04-0.07)
- partial_exit: cover 50% at first target, let rest ride

Respond in valid JSON only, no markdown — only include approved trades (put any sell/rotation BEFORE the buy):
{{"trades": [{{"symbol": "X", "action": "buy|short|sell", "confidence": "high|medium|low", "quantity_suggestion": integer, "take_profit_pct": float, "stop_loss_pct": float, "partial_exit": boolean, "analysis": "2 sentences: catalyst + why long/short/sell"}}], "skipped": "brief reason"}}"""

    # ── Inject override into step2 ───────────────────────────────────────────
    # step1 override was already injected above before it was sent.
    # step2_prompt is now fully built — inject override before sending.
    if _prompt_override:
        step2_prompt += f"\n\n## Operator Override Instructions (follow these today)\n{_prompt_override}"

    try:
        step2_raw = ask_ai_pro(step2_prompt, max_tokens=2500)
        step2_data = parse_ai_json(step2_raw)
        approved = step2_data.get("trades", [])
        logger.info(f"Step 2 — Approved {len(approved)} trades: {[t.get('symbol') for t in approved]} | Skipped: {step2_data.get('skipped','')}")
        # ── Save both prompts for viewer — only on successful cycle ──────────
        try:
            from services.db import set_setting as _set_setting
            _set_setting("last_prompts", {
                "step1": step1_prompt,
                "step2": step2_prompt,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as _pe:
            logger.debug(f"Prompt save non-fatal: {_pe}")
    except Exception as e:
        logger.error(f"Step 2 failed: {e}. Raw: {step2_raw[:300] if 'step2_raw' in locals() else 'none'}")
        return [TradeDecision(action="hold", symbol=None, quantity=None,
                              reasoning="Deep analysis failed. Holding positions.")]

    if not approved:
        return [TradeDecision(action="hold", symbol=None, quantity=None,
                              reasoning=f"No trades approved this cycle. Candidates: {', '.join(top_symbols)}")]

    # ── Step 3: Convert approved list into TradeDecision objects ──
    decisions = []
    remaining_cash = effective_cash
    sectors_bought = []

    # Sort sells first so rotation proceeds are added to remaining_cash
    # before any subsequent buy checks affordability — regardless of Claude's output order
    _step3_priority = {"sell": 0, "buy": 1, "short": 1, "hold": 2}
    approved = sorted(approved, key=lambda t: _step3_priority.get(t.get("action", "hold"), 2))

    for trade in approved:  # no cap here — sector/cash checks may discard some; trading_engine enforces max_trades_per_cycle
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

        # TP cap: 0.80 for leveraged ETFs (TQQQ/SOXL can run 60-100%+ on bull weeks),
        # 0.60 for all others. Old 0.40 cap was cutting winners short on strong moves.
        from services.entry_timing import _LEVERAGED_ETFS as _lev_check
        _tp_cap = 0.80 if sym in _lev_check else 0.60
        take_profit_pct = max(0.05, min(_safe_pct(trade.get("take_profit_pct"), default_tp), _tp_cap))
        stop_loss_pct   = max(0.02, min(_safe_pct(trade.get("stop_loss_pct"),   default_sl), 0.10))
        partial_exit = bool(trade.get("partial_exit", False))
        analysis = trade.get("analysis", "")

        if not sym or action not in ("buy", "short", "sell"):
            continue

        # Confidence gate
        min_confidence = current_strategy.get("min_confidence", "medium")
        confidence_rank = {"high": 2, "medium": 1, "low": 0}
        if confidence_rank.get(confidence, 0) < confidence_rank.get(min_confidence, 1):
            logger.info(f"Skipping {sym} — confidence {confidence} below strategy minimum {min_confidence}")
            continue

        # Sector correlation check
        # Leveraged ETFs (SOXL, TQQQ, SPXL etc.) are exempt — they're already
        # capped by _CORR_GROUPS in trading_engine.py. Counting them here would
        # wrongly block individual stocks in the same sector (e.g. MU/NVDA blocked
        # because SOXL already consumed the Semis slot).
        try:
            from services.entry_timing import _LEVERAGED_ETFS as _LEV_ETFS
            from services.sector_momentum import get_sector_for_symbol
            if sym not in _LEV_ETFS:
                sym_sector = get_sector_for_symbol(sym)
                # risk_settings takes precedence; 0 = no limit
                from services.trading_engine import _risk_settings as _te_rs
                _raw_sc = _te_rs.get("sector_cap") if _te_rs.get("sector_cap") is not None else current_strategy.get("sector_cap", 2)
                sector_cap = int(_raw_sc) if _raw_sc else 0
                existing_sector_count = sum(1 for s in sectors_bought if s == sym_sector)
                held_in_sector = [p.symbol for p in positions if p.symbol not in _LEV_ETFS and get_sector_for_symbol(p.symbol) == sym_sector]
                if sector_cap > 0:
                    if existing_sector_count >= sector_cap and sym_sector not in ("Unknown", "Broad"):
                        logger.info(f"Skipping {sym} — already buying {existing_sector_count} {sym_sector} stocks this cycle (cap={sector_cap})")
                        continue
                    if len(held_in_sector) >= sector_cap and sym_sector not in ("Unknown", "Broad"):
                        logger.info(f"Skipping {sym} — already hold {held_in_sector} in {sym_sector} (cap={sector_cap})")
                        continue
                sectors_bought.append(sym_sector)
            else:
                logger.debug(f"{sym} is a leveraged ETF — skipping sector cap check")
        except Exception:
            pass

        if action in ("buy", "short"):
            price = (deep_data.get(sym) or market_snapshot.get(sym, {})).get("current_price") or 0
            if price <= 0:
                continue

            # Penny stock cap
            if action == "buy" and price < 5.0:
                from services import trading_engine as _te
                _penny_pct = float(_te._risk_settings.get("max_penny_position_pct", 3.0)) / 100.0
                effective_max_position = effective_portfolio * _penny_pct
            else:
                effective_max_position = max_position

            # ── Kelly Criterion sizing (replaces flat % sizing) ──
            _rs_score = (rs_map or {}).get(sym)
            _rs_pct = _rs_score.percentile if _rs_score else 50.0
            try:
                from services.brain.kelly import kelly_size
                from services.indicators import compute_atr as _atr_fn
                _sym_data_k = deep_data.get(sym) or market_snapshot.get(sym, {})
                _closes_k = _sym_data_k.get("closing_prices", [])
                _highs_k = _sym_data_k.get("highs", _closes_k)
                _lows_k = _sym_data_k.get("lows", _closes_k)
                _atr_k = _atr_fn(_highs_k, _lows_k, _closes_k) if len(_closes_k) >= 15 else 0.0
                _signal_type = next((o.get("signal") for o in opportunities if o.get("symbol") == sym), None)
                _kelly = kelly_size(
                    symbol=sym,
                    signal_type=_signal_type,
                    conviction=confidence,
                    portfolio_value=effective_portfolio,
                    price=price,
                    atr=_atr_k,
                    trade_history=kelly_history or [],
                    strategy_key=current_strategy.get("key", "aggressive"),
                    rs_percentile=_rs_pct,
                )
                # Kelly is the primary sizing; cap to what strategy and cash allow
                max_shares_by_strategy = int(effective_max_position / price)
                max_shares_by_cash = int(remaining_cash / price) if action == "buy" else max_shares_by_strategy
                max_shares = min(max_shares_by_strategy, max_shares_by_cash)
                final_qty = min(_kelly.shares, max_shares)
                final_qty = max(1, final_qty)
                logger.info(f"Kelly size {sym}: {_kelly.rationale} → capped to {final_qty} shares")
            except Exception as _ke:
                logger.warning(f"Kelly sizing failed for {sym} ({_ke}) — using flat sizing")
                max_shares_by_strategy = int(effective_max_position / price)
                max_shares_by_cash = int(remaining_cash / price) if action == "buy" else max_shares_by_strategy
                max_shares = min(max_shares_by_strategy, max_shares_by_cash)
                size_pct = 1.0 if confidence == "high" else 0.75
                final_qty = max(1, int(max_shares * size_pct))

            cost = price * final_qty
            if action == "buy" and final_qty < 1:
                logger.info(f"Skipping {sym} — insufficient cash (have ${remaining_cash:.0f}, price ${price:.2f})")
                continue
            if action == "buy":
                remaining_cash -= cost
            # Shorts: no cash deduction — margin account, buying power tracked by Alpaca
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
            buffered_proceeds = proceeds * 0.75  # 75% buffer — bracket orders lock both buy + stop leg
            remaining_cash = max(0.0, remaining_cash + buffered_proceeds)  # never go negative
            logger.info(f"Rotation sell: {sym} x{final_qty} @ ${price:.2f} → +${proceeds:,.0f} gross / +${buffered_proceeds:,.0f} usable (75% buffer) → remaining cash ${remaining_cash:,.0f}")
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
