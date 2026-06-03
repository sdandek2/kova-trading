import json
import logging
from datetime import datetime, timezone

from models.trade import TradeDecision
from services.indicators import compute_all
from services import strategy as strategy_service
from services.ai_client import ask_ai, ask_ai_pro, parse_ai_json

logger = logging.getLogger(__name__)


def _predictive_side_from_action(action: str) -> str:
    return "short" if action == "short" else "long"


def _predictive_boost(expectancy_pct: float, trades: int) -> float:
    if trades <= 0:
        return 0.0
    sample_mult = min(trades, 8) / 8.0
    return max(-12.0, min(12.0, expectancy_pct * 6.0 * sample_mult))


def _pre_rank_market_snapshot(
    market_snapshot: dict,
    sentiment: dict | None = None,
    sector_context: dict | None = None,
    macro: dict | None = None,
    earnings_map: dict | None = None,
) -> tuple[list[str], list[str], dict]:
    """
    Deterministic pre-ranker for Step 1.
    Reduces prompt noise by surfacing the best long and short candidates before the LLM scans them.
    """
    regime = (macro or {}).get("market_regime", "")
    spy_trend = (macro or {}).get("spy_trend", "")
    vix_level = (macro or {}).get("vix_level", "normal")
    is_bull = regime in ("bull", "bullish") and "uptrend" in (spy_trend or "")
    is_bear = regime in ("bear", "bearish") or "downtrend" in (spy_trend or "")

    long_scores: list[tuple[float, str]] = []
    short_scores: list[tuple[float, str]] = []
    details: dict[str, dict] = {}

    for sym, data in market_snapshot.items():
        closes = data.get("closing_prices", [])
        if not closes:
            continue
        indicators = compute_all(closes)
        rsi = indicators.get("rsi")
        macd_hist = (indicators.get("macd") or {}).get("histogram")
        price = data.get("current_price") or 0
        rel_vol = float(data.get("relative_volume", 1.0) or 1.0)
        five_day = float(data.get("five_day_change_pct", 0.0) or 0.0)
        news_count = float((sentiment or {}).get(sym, 0) or 0)
        earnings_timing = (earnings_map or {}).get(sym)
        sector = (sector_context or {}).get(sym, {})
        sector_pct = float(sector.get("sector_pct", 0.0) or 0.0)

        long_score = 50.0
        short_score = 50.0

        long_score += min(max(rel_vol - 1.0, 0.0), 3.0) * 7.0
        short_score += min(max(rel_vol - 1.0, 0.0), 3.0) * 5.0

        long_score += min(max(five_day, -4.0), 12.0) * 1.3
        short_score += min(max(-five_day, -4.0), 12.0) * 1.0

        long_score += min(news_count, 8.0) * 2.0
        short_score += min(news_count, 8.0) * 1.5

        long_score += sector_pct * 2.0
        short_score += (-sector_pct) * 1.8

        if isinstance(rsi, (int, float)):
            if 48 <= rsi <= 72:
                long_score += 8.0
            elif rsi > 82:
                long_score -= 10.0
            elif rsi < 35:
                long_score += 2.0

            if rsi >= 68:
                short_score += min((rsi - 68.0) * 1.8, 18.0)
            elif rsi < 60:
                short_score -= 10.0

        if isinstance(macd_hist, (int, float)) and price > 0:
            macd_hist_pct = (macd_hist / price) * 100
            long_score += min(max(macd_hist_pct, -0.2), 0.5) * 28.0
            short_score += min(max(-macd_hist_pct, -0.2), 0.5) * 28.0
        else:
            macd_hist_pct = None

        if earnings_timing == "today/tomorrow":
            long_score -= 35.0
            short_score -= 35.0
        elif earnings_timing == "this_week":
            long_score -= 8.0
            short_score -= 8.0

        if is_bull:
            long_score += 6.0
            short_score -= 8.0
        elif is_bear:
            long_score -= 6.0
            short_score += 6.0

        if vix_level in ("elevated", "extreme_fear"):
            long_score -= 4.0
            short_score += 2.0

        details[sym] = {
            "long_score": round(long_score, 2),
            "short_score": round(short_score, 2),
            "rsi": rsi,
            "macd_hist_pct": round(macd_hist_pct, 3) if macd_hist_pct is not None else None,
            "relative_volume": rel_vol,
        }
        long_scores.append((long_score, sym))
        short_scores.append((short_score, sym))

    long_symbols = [sym for _, sym in sorted(long_scores, reverse=True)[:26]]
    short_symbols = [sym for _, sym in sorted(short_scores, reverse=True)[:14]]
    return long_symbols, short_symbols, details

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
    rejected_symbols: list = None,     # symbols in rejection cooldown — Claude must not nominate these
    prebreakout_candidates: list = None,  # pre-breakout setups from breakout_scanner — prioritise these
    urgent_news_context: list = None,  # high-impact news that woke this cycle early
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

    ranked_longs, ranked_shorts, pre_rank_details = _pre_rank_market_snapshot(
        market_snapshot=market_snapshot,
        sentiment=sentiment,
        sector_context=sector_context,
        macro=macro,
        earnings_map=earnings_map,
    )
    pre_rank_symbols = []
    for sym in ranked_longs + ranked_shorts:
        if sym not in pre_rank_symbols:
            pre_rank_symbols.append(sym)
    if pre_rank_symbols:
        market_snapshot = {sym: market_snapshot[sym] for sym in pre_rank_symbols if sym in market_snapshot}
        logger.info(
            f"Step 1 pre-ranker shortlisted {len(market_snapshot)} symbols | "
            f"top longs: {ranked_longs[:6]} | top shorts: {ranked_shorts[:4]}"
        )

    predictive_priors = {}
    try:
        from services.db import get_predictive_trade_priors
        predictive_priors = get_predictive_trade_priors(
            symbols=list(market_snapshot.keys())[:40],
            market_regime=(macro or {}).get("market_regime"),
            days=180,
        )
    except Exception as pred_exc:
        logger.debug(f"Predictive priors unavailable (non-fatal): {pred_exc}")

    current_regime = (macro or {}).get("market_regime") or "unknown"
    for sym, detail in pre_rank_details.items():
        long_regime_key = f"{sym}|{current_regime}|long"
        short_regime_key = f"{sym}|{current_regime}|short"
        long_key = f"{sym}|long"
        short_key = f"{sym}|short"
        long_prior = (
            predictive_priors.get("symbol_regime_side", {}).get(long_regime_key)
            or predictive_priors.get("symbol_side", {}).get(long_key)
        )
        short_prior = (
            predictive_priors.get("symbol_regime_side", {}).get(short_regime_key)
            or predictive_priors.get("symbol_side", {}).get(short_key)
        )
        if long_prior:
            detail["long_predictive_expectancy_pct"] = long_prior["expectancy_pct"]
            detail["long_predictive_trades"] = long_prior["trades"]
            detail["long_score"] += _predictive_boost(long_prior["expectancy_pct"], long_prior["trades"])
        if short_prior:
            detail["short_predictive_expectancy_pct"] = short_prior["expectancy_pct"]
            detail["short_predictive_trades"] = short_prior["trades"]
            detail["short_score"] += _predictive_boost(short_prior["expectancy_pct"], short_prior["trades"])

    ranked_longs = sorted(
        [sym for sym in market_snapshot.keys()],
        key=lambda sym: pre_rank_details.get(sym, {}).get("long_score", 0.0),
        reverse=True,
    )[:26]
    ranked_shorts = sorted(
        [sym for sym in market_snapshot.keys()],
        key=lambda sym: pre_rank_details.get(sym, {}).get("short_score", 0.0),
        reverse=True,
    )[:14]
    reordered_symbols = []
    for sym in ranked_longs + ranked_shorts:
        if sym not in reordered_symbols and sym in market_snapshot:
            reordered_symbols.append(sym)
    if reordered_symbols:
        market_snapshot = {sym: market_snapshot[sym] for sym in reordered_symbols}

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
        pre_rank = pre_rank_details.get(sym, {})
        rank_flag = ""
        if pre_rank:
            rank_flag = (
                f" [RANK:L{int(round(pre_rank.get('long_score', 0)))}"
                f"/S{int(round(pre_rank.get('short_score', 0)))}]"
            )
        pred_flag = ""
        _lp = pre_rank.get("long_predictive_expectancy_pct")
        _lt = int(pre_rank.get("long_predictive_trades", 0) or 0)
        _sp = pre_rank.get("short_predictive_expectancy_pct")
        _st = int(pre_rank.get("short_predictive_trades", 0) or 0)
        if _lt >= 2 or _st >= 2:
            pred_flag = (
                f" [PRED:L{_lp:+.2f}%/{_lt}t" if _lt >= 2 and _lp is not None else " [PRED:Ln/a"
            )
            pred_flag += (
                f" S{_sp:+.2f}%/{_st}t]" if _st >= 2 and _sp is not None else " Sn/a]"
            )

        line = (
            f"  - {sym}{penny_flag}{sentiment_flag}{earnings_flag}: ${price}, "
            f"5-day: {data.get('five_day_change_pct', 'N/A')}%, "
            f"RSI: {rsi}, "
            f"MACD hist: {macd.get('histogram', 'N/A')}, "
            f"MA20: ${mas.get('ma20', 'N/A')}, MA50: ${mas.get('ma50', 'N/A')}"
            f"{vol_flag}{rank_flag}{pred_flag}"
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
        from services.db import get_trade_learning_summary, get_trade_performance_summary, get_rejection_summary
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
        learning = get_trade_learning_summary(limit=80)
        rejection_summary = get_rejection_summary(hours=24, limit=200)
        learning_lines = []
        for lesson in learning.get("lessons", [])[:6]:
            learning_lines.append(f"- {lesson}")
        for lesson in rejection_summary.get("lessons", [])[:5]:
            learning_lines.append(f"- {lesson}")
        if learning_lines:
            rejected_syms = ", ".join([
                f"{s['symbol']}({s['count']})"
                for s in rejection_summary.get("symbols", [])[:5]
            ])
            performance_text += f"""
## Aggressive Learning Loop (stay active, avoid low-quality repeats)
{chr(10).join(learning_lines)}
{f"- Frequently rejected symbols today: {rejected_syms}" if rejected_syms else ""}
Use this as a quality filter, not a reason to sit idle: rotate toward setups that are working and replace rejected patterns with better candidates.
"""
        regime_side = predictive_priors.get("regime_side", {})
        news_profiles = predictive_priors.get("news_profile", {})
        conviction_profiles = predictive_priors.get("conviction_profile", {})
        setup_profiles = predictive_priors.get("setup", {})
        if regime_side or news_profiles or conviction_profiles:
            _reg_long = regime_side.get(f"{current_regime}|long")
            _reg_short = regime_side.get(f"{current_regime}|short")
            _news = news_profiles.get("news_event")
            _non_news = news_profiles.get("non_news")
            _rocket = conviction_profiles.get("rocket")
            _standard = conviction_profiles.get("standard")
            _best_setup = None
            _worst_setup = None
            if setup_profiles:
                _best_setup = max(setup_profiles.items(), key=lambda kv: kv[1]["expectancy_pct"])
                _worst_setup = min(setup_profiles.items(), key=lambda kv: kv[1]["expectancy_pct"])
            performance_text += f"""
## Predictive Priors (trained on your closed trades)
- Regime prior now: long {(_reg_long or {}).get('expectancy_pct', 'n/a')}% expectancy / short {(_reg_short or {}).get('expectancy_pct', 'n/a')}% expectancy in {current_regime}
- News trades: {(_news or {}).get('expectancy_pct', 'n/a')}% expectancy vs non-news: {(_non_news or {}).get('expectancy_pct', 'n/a')}%
- Rocket trades: {(_rocket or {}).get('expectancy_pct', 'n/a')}% expectancy vs standard: {(_standard or {}).get('expectancy_pct', 'n/a')}%
- Best setup prior: {_best_setup[0] if _best_setup else 'n/a'} ({_best_setup[1]['expectancy_pct']}%) | Worst setup prior: {_worst_setup[0] if _worst_setup else 'n/a'} ({_worst_setup[1]['expectancy_pct']}%)
Use these priors as a tie-breaker and conviction guide. Avoid forcing setups with clearly negative expectancy unless fresh evidence is unusually strong.
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
    # Claude decides individually per stock based on its own signals (RSI, MACD, sector momentum).
    # SPY trend is provided as context but does NOT force a long/short direction.
    regime = (macro or {}).get("market_regime", "")
    spy_trend = (macro or {}).get("spy_trend", "")
    vix_level = (macro or {}).get("vix_level", "normal")
    is_bearish_day = False  # no longer used to block longs
    regime_direction_note = ""
    bearish_etf_note = ""

    # ── 3-tier regime: determines long/short pool sizes and RSI short floor ──
    # Bull:    SPY strong_uptrend + VIX normal + macro bull  → 8 longs + 2 shorts, RSI 72+
    # Neutral: SPY sideways or VIX elevated or macro neutral → 7 longs + 3 shorts, RSI 68+
    # Bear:    SPY downtrend or macro bear                   → 6 longs + 4 shorts, RSI 65+
    _is_bull = (
        regime in ("bull", "bullish") and
        "uptrend" in (spy_trend or "") and
        vix_level in ("normal", "low", "low_fear")
    )
    _is_bear = regime in ("bear", "bearish") or "downtrend" in (spy_trend or "")
    if _is_bull:
        _market_tier = "bull"
        _long_count, _short_count = 8, 2
        _short_rsi_floor = 70 if current_strategy.get("key") == "aggressive" else 68
    elif _is_bear:
        _market_tier = "bear"
        _long_count, _short_count = 6, 4
        _short_rsi_floor = 62 if current_strategy.get("key") == "aggressive" else 60
    else:
        _market_tier = "neutral"
        _long_count, _short_count = 7, 3
        _short_rsi_floor = 66 if current_strategy.get("key") == "aggressive" else 65
    logger.info(f"Market tier: {_market_tier} → {_long_count} longs + {_short_count} shorts | RSI short floor: {_short_rsi_floor}")

    # ── Top News Catalysts block ────────────────────────────────────────────
    # Pull top symbols by mention count from the sentiment dict and enrich
    # with event types from the real-time news stream cache so the AI sees
    # explicit catalyst labels (analyst_upgrade, mna, etc.) rather than just
    # a buried [NEWS:N] tag in the market universe table.
    top_news_block = ""
    try:
        _top_news_syms = sorted(
            [(sym, cnt) for sym, cnt in (sentiment or {}).items() if cnt > 0],
            key=lambda x: x[1],
            reverse=True,
        )[:8]
        if _top_news_syms:
            # Enrich with event types from cached real-time news stream
            _cached_events: dict[str, list[str]] = {}
            try:
                from services.news_stream import get_cached_news as _gcn
                for _art in _gcn(limit=150, max_age_minutes=90):
                    for _s in (_art.get("symbols") or []):
                        if _s not in _cached_events:
                            _cached_events[_s] = []
                        _cached_events[_s].extend(_art.get("event_types") or [])
            except Exception:
                pass

            _catalyst_lines = []
            for _sym, _cnt in _top_news_syms:
                _evts = list(dict.fromkeys(_cached_events.get(_sym, [])))  # deduplicate, preserve order
                _evt_str = f" [{', '.join(_evts[:3])}]" if _evts else ""
                _catalyst_lines.append(f"  • {_sym}: {_cnt} mentions{_evt_str}")

            top_news_block = f"""
## 🔥 Top News Catalysts RIGHT NOW
These symbols have the highest news activity this cycle — they may have analyst upgrades, partnerships, earnings, or major announcements driving them. Check each one's technicals and include the strongest setups in your candidate pool:
{chr(10).join(_catalyst_lines)}
Evaluate these early, but skip them if price, volume, and momentum do not confirm the headline.
"""
    except Exception:
        pass

    # Build rejected symbols note for Step 1
    rejected_note = ""
    if rejected_symbols:
        rejected_note = f"\n⛔ DO NOT nominate these symbols — currently in rejection cooldown (overextended/overbought): {', '.join(rejected_symbols)}\nFind different opportunities instead.\n"

    # Pre-breakout candidates — inject at top of Step 1 so Claude sees them first
    prebreakout_note = ""
    if prebreakout_candidates:
        from services.breakout_scanner import format_for_prompt as _fmt_breakout
        prebreakout_note = "\n" + _fmt_breakout(prebreakout_candidates) + "\n"

    step1_prompt = f"""You are a professional equity analyst managing a paper trading portfolio. Analyze the market data below and identify the best opportunities for simulated trades. This is Alpaca paper trading — no real money involved.

{portfolio_context}
{regime_direction_note}{prebreakout_note}{rejected_note}{eod_step1_context}{macro_text}{geo_text}{news_text}{trade_feedback_text}{earnings_plays_text}{bearish_etf_note}{top_news_block}
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

Scan the stocks above. Identify separate candidate pools based on technicals and macro context.
Current market tier: {_market_tier.upper()} → nominate TOP {_long_count} long candidates + TOP {_short_count} short candidates (total 10).
- [RANK:Lx/Sy] is the deterministic pre-ranker score. Higher = stronger fit. Use it as a strong prior, but override it when fresh catalyst or technical context clearly says otherwise.
- BULL tier: favor longs and leveraged ETFs — short slots are scarce, only the most obvious overextensions qualify
- NEUTRAL tier: balanced mix — shorts need clear overbought signals
- BEAR tier: favor inverse ETFs and short candidates — more short setups available
- long_candidates: TOP {_long_count} long/inverse ETF opportunities
- short_candidates: TOP {_short_count} individual short-sale opportunities only
- short_candidate: ONLY nominate when RSI > {_short_rsi_floor} AND MACD histogram <= 0 or clearly falling. RSI below {_short_rsi_floor} = not overbought enough for this market tier, skip it. Positive/rising MACD = momentum too strong, skip it.
Signal types: "momentum", "breakout", "reversal", "short_candidate", "inverse_etf", "oversold"

Return ONLY a JSON object with TWO keys: "long_candidates" and "short_candidates". Each entry has exactly TWO fields: "symbol" and "signal". No thesis, no explanation, no extra fields, no markdown. Long candidates should be the primary pool; only include shorts when the setup is clearly asymmetric.
EXAMPLE (copy this structure exactly): {{"long_candidates": [{{"symbol": "AAPL", "signal": "momentum"}}, {{"symbol": "SQQQ", "signal": "inverse_etf"}}], "short_candidates": [{{"symbol": "XYZ", "signal": "short_candidate"}}]}}"""

    if _prompt_override:
        step1_prompt += f"\n\n## Operator Override Instructions (follow these today)\n{_prompt_override}"

    try:
        logger.info(f"Step 1 prompt size: ~{len(step1_prompt) // 4:,} tokens across {len(market_snapshot)} symbols")
        step1_raw = ask_ai(step1_prompt, max_tokens=4000)
        step1_data = parse_ai_json(step1_raw)
        raw_longs = step1_data.get("long_candidates", step1_data.get("opportunities", []))
        raw_shorts = step1_data.get("short_candidates", [])
        # Validate: only keep entries that have a string symbol — drop malformed entries
        raw_opps = list(raw_longs or []) + list(raw_shorts or [])
        opportunities = []
        seen_symbols = set()
        for o in raw_opps:
            if not isinstance(o, dict) or not isinstance(o.get("symbol"), str) or not o["symbol"]:
                continue
            sym = o["symbol"].upper()
            if sym in seen_symbols:
                continue
            seen_symbols.add(sym)
            opportunities.append({**o, "symbol": sym})
        logger.info(f"Step 1 — Top opportunities: {[o['symbol'] for o in opportunities]}")
    except Exception as e:
        logger.error(f"Step 1 failed: {e}. Raw response: {step1_raw[:300] if 'step1_raw' in locals() else 'none'}")
        return [TradeDecision(action="hold", symbol=None, quantity=None,
                              reasoning=f"Market scan error (Step 1 technical failure: {str(e)[:80]}). Holding — will retry next cycle.")]

    if not opportunities:
        return [TradeDecision(action="hold", symbol=None, quantity=None,
                              reasoning="Market scan complete — no clear entry signals this cycle. All candidates in neutral territory or insufficient catalyst. Holding.")]

    # ── Step 2: Deep dive — evaluate ALL candidates, approve up to 3 trades ──
    top_symbols = [o["symbol"] for o in opportunities[:12]]

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
    realtime_news_by_symbol = {}
    try:
        from services.news_stream import get_cached_news
        for article in get_cached_news(limit=100, max_age_minutes=90):
            for news_sym in article.get("symbols") or []:
                realtime_news_by_symbol.setdefault(news_sym, []).append(article)
    except Exception:
        pass

    for opp in opportunities[:12]:
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
        symbol_events = realtime_news_by_symbol.get(sym, [])[:2]
        if symbol_events:
            event_bits = []
            for article in symbol_events:
                types = ",".join(article.get("event_types") or ["news"])
                sentiment_label = article.get("event_sentiment", "neutral")
                impact = article.get("event_impact", "normal")
                headline = (article.get("headline") or "")[:110]
                event_bits.append(f"{sentiment_label}/{impact}/{types}: {headline}")
            line += f", NEWS_EVENT={' | '.join(event_bits)}"
        if intraday_summary.get(sym):
            line += f", {intraday_summary[sym]}"
        candidate_detail.append(line)

    default_tp = current_strategy.get("default_take_profit_pct", 0.10)
    default_sl = current_strategy.get("default_stop_loss_pct", 0.04)
    max_ai_trades = 4 if current_strategy.get("key") == "aggressive" else 3
    min_trade_instruction = (
        "Approve only A- or B-grade setups. Holding cash is acceptable when signals are mixed, crowded, or low-quality."
        if current_strategy.get("key") == "aggressive"
        else "Approve trades only when setup quality is clearly sufficient."
    )
    positions_count = len(positions)
    cash_pct = (effective_cash / effective_portfolio * 100) if effective_portfolio > 0 else 0

    pressure_note = "\n⚠️ AFTERNOON CHECK: Activity has been light today. If a clean setup exists, don't ignore it, but do NOT force a trade just to increase count.\n" if afternoon_pressure else ""

    # ── Urgent news context — injected when a high-impact headline woke this cycle early ──
    urgent_news_note = ""
    if urgent_news_context:
        news_lines = []
        for n in urgent_news_context[-3:]:
            symbols_str = ", ".join(n.get("symbols", [])[:5]) or "general market"
            reason = n.get("reason", "")[:200]
            news_lines.append(f"  • [{symbols_str}] {reason}")
        urgent_news_note = f"""
⚡ URGENT NEWS TRIGGER — This cycle was woken early by high-impact news. Evaluate the affected symbols early.
{chr(10).join(news_lines)}
Treat these events as important context, not automatic trades. Only approve when price, volume, and momentum confirm the news direction.
"""

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

    step2_prompt = f"""You are building a high-performance trading portfolio that profits in ANY market direction. Evaluate EACH candidate and approve the best 0-{max_ai_trades} trades this cycle.
{pressure_note}{urgent_news_note}{regime_direction_note}
{eod_step2_context}{performance_text}
{portfolio_context}
Cash available: ${effective_cash:,.2f} ({cash_pct:.0f}% of portfolio) | Open positions: {positions_count}
{"Cash is available — deploy only into A/B-grade setups. Never trade to stay active." if positions_count < 3 else ""}
{rotation_note}
{geo_text if geo_context else ""}{news_text}{bearish_etf_note}
## Candidates to evaluate:
{chr(10).join(candidate_detail)}

Strategy: {current_strategy['name']} — {current_strategy['prompt_modifier']}
Market tier: {_market_tier.upper()} | Short RSI floor this cycle: {_short_rsi_floor}

For EACH candidate decide: BUY, SHORT, or SKIP.
Rules:
- Approve up to {max_ai_trades} trades per cycle (mix of longs and shorts)
- BUY: standard long position — profit when price rises
- SHORT: sell shares short — profit when price FALLS. Requires: RSI > {_short_rsi_floor} AND MACD histogram <= 0. Ideal: RSI > {_short_rsi_floor + 6} + MACD negative + bearish sector/news + no upcoming earnings. SKIP if RSI ≤ {_short_rsi_floor} or MACD > 0 — the system will reject it and waste the slot
- SELL: close an existing long position (for rotation or profit-taking)
- Inverse ETFs (SQQQ/SPXU/SOXS/SDOW/TZA/UVXY): always use BUY action — they already profit from market falls
- In bearish regime: prioritize inverse ETF buys first; use individual stock shorts only when the bearish setup is unusually clear
- If geo risk is HIGH: only approve inverse ETFs, short sells, or safe havens
- {min_trade_instruction}
- Do NOT trade just to stay active. Skip mediocre setups even in aggressive mode.
- Prefer fewer high-quality trades over multiple average trades.
- Avoid rotating or re-entering a symbol unless the new setup is clearly better than the previous one.
- Mark high_conviction=true only for rare A+ setups: fresh catalyst or breakout, high relative volume, strong trend alignment, and unusually asymmetric upside/downside.
- Treat NEWS_EVENT as high-priority evidence. Bullish high-impact events favor BUY; bearish high-impact events usually favor inverse ETF BUY or SELL first, and SHORT only when price/volume confirms downside.

For LONG trades:
- take_profit_pct: REALISTIC session-level upside only. Normal stock=0.05-0.12, strong catalyst=0.08-0.15, leveraged ETF (TQQQ/SOXL/SPXL)=0.10-0.25 on strong trend days only. DO NOT set targets above 0.25 — they will never be hit intraday and positions will ride into stop losses instead.
- stop_loss_pct: trailing stop (0.03-0.06). REQUIRED: take_profit_pct must be at least 2× stop_loss_pct (minimum 2:1 R:R). If you can't find a 2:1 setup, SKIP the trade.
- partial_exit=true when upside > 0.10 — lock in half at first target, trail the rest

For SHORT trades:
- take_profit_pct: how far you expect it to FALL (0.05-0.12)
- stop_loss_pct: how much RISE to tolerate before covering (0.03-0.06)
- REQUIRED: take_profit_pct must be at least 2× stop_loss_pct
- partial_exit: cover 50% at first target, let rest ride

Respond in valid JSON only, no markdown — only include approved trades (put any sell/rotation BEFORE the buy):
{{"trades": [{{"symbol": "X", "action": "buy|short|sell", "confidence": "high|medium|low", "high_conviction": boolean, "quantity_suggestion": integer, "take_profit_pct": float, "stop_loss_pct": float, "partial_exit": boolean, "analysis": "2 sentences: catalyst + why long/short/sell"}}], "skipped": "brief reason"}}"""

    # ── Inject override into step2 ───────────────────────────────────────────
    # step1 override was already injected above before it was sent.
    # step2_prompt is now fully built — inject override before sending.
    if _prompt_override:
        step2_prompt += f"\n\n## Operator Override Instructions (follow these today)\n{_prompt_override}"

    try:
        logger.info(f"Step 2 prompt size: ~{len(step2_prompt) // 4:,} tokens across {len(top_symbols)} candidates")
        step2_raw = ask_ai_pro(step2_prompt, max_tokens=5000)
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
    sold_this_cycle: set = set()  # guard: prevent re-buying a symbol sold in the same cycle

    # Sort sells first so rotation proceeds are added to remaining_cash
    # before any subsequent buy checks affordability — regardless of Claude's output order
    _step3_priority = {"sell": 0, "buy": 1, "short": 1, "hold": 2}
    approved = sorted(approved, key=lambda t: _step3_priority.get(t.get("action", "hold"), 2))

    for trade in approved:  # no cap here — sector/cash checks may discard some; trading_engine enforces max_trades_per_cycle
        sym = trade.get("symbol")
        action = trade.get("action", "hold")
        confidence = trade.get("confidence", "medium")
        qty_suggestion = trade.get("quantity_suggestion")
        high_conviction = bool(trade.get("high_conviction", False))

        def _strategy_tag(symbol: str, action_value: str, raw_signal: str) -> str:
            signal = (raw_signal or "").lower()
            data = deep_data.get(symbol) or market_snapshot.get(symbol, {})
            symbol_detail = next((line for line in candidate_detail if line.startswith(f"{symbol} ")), "")
            rsi_val = "N/A"
            try:
                ind = compute_all(data.get("closing_prices", [])) if data.get("closing_prices") else {}
                rsi_val = ind.get("rsi", "N/A")
            except Exception:
                pass

            if action_value == "short":
                return "short_news" if "NEWS_EVENT=bearish" in symbol_detail else "short_overbought"
            if action_value == "sell":
                return "rotation_sell"
            if signal == "inverse_etf" or symbol in {"SQQQ", "SPXU", "SOXS", "SDOW", "TZA", "UVXY"}:
                return "inverse_etf"
            if "NEWS_EVENT=bullish" in symbol_detail:
                return "long_news"
            if signal == "breakout":
                return "long_breakout"
            if signal == "reversal" or (isinstance(rsi_val, (int, float)) and rsi_val < 35):
                return "long_reversal"
            return "long_momentum"

        signal_for_trade = next((o.get("signal", "") for o in opportunities if o.get("symbol") == sym), "")
        predictive_side = _predictive_side_from_action(action)
        predictive_detail = pre_rank_details.get(sym, {})
        predictive_expectancy_pct = predictive_detail.get(
            "short_predictive_expectancy_pct" if predictive_side == "short" else "long_predictive_expectancy_pct"
        )
        predictive_trades = int(predictive_detail.get(
            "short_predictive_trades" if predictive_side == "short" else "long_predictive_trades", 0
        ) or 0)

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

        # TP cap: 0.25 for leveraged ETFs, 0.20 for all others (realistic intraday targets).
        # Prior caps of 0.80/0.60 caused TPs to never be hit — positions rode into stop losses.
        from services.entry_timing import _LEVERAGED_ETFS as _lev_check
        _tp_cap = 0.25 if sym in _lev_check else 0.20
        take_profit_pct = max(0.05, min(_safe_pct(trade.get("take_profit_pct"), default_tp), _tp_cap))
        stop_loss_pct   = max(0.02, min(_safe_pct(trade.get("stop_loss_pct"),   default_sl), 0.08))

        # Enforce minimum 2:1 R:R — if AI set a TP that doesn't clear the bar, reject the trade
        if action in ("buy", "short") and take_profit_pct < stop_loss_pct * 2.0:
            logger.info(
                f"Skipping {sym} — R:R below 2:1 (TP={take_profit_pct*100:.0f}% vs SL={stop_loss_pct*100:.0f}%)"
            )
            continue
        partial_exit = bool(trade.get("partial_exit", False))
        analysis = trade.get("analysis", "")

        if not sym or action not in ("buy", "short", "sell"):
            continue

        # Guard: skip re-buy of a symbol sold in the same cycle (prevents TQQQ sell→rebuy waste)
        if action == "buy" and sym in sold_this_cycle:
            logger.info(f"Skipping BUY {sym} — already sold this cycle (rotation guard)")
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
                sector_cap = current_strategy.get("sector_cap", 1)
                existing_sector_count = sum(1 for s in sectors_bought if s == sym_sector)
                held_in_sector = [p.symbol for p in positions if p.symbol not in _LEV_ETFS and get_sector_for_symbol(p.symbol) == sym_sector]
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
            # Apply penny stock cap in Step 3 so cash reservation is correctly sized.
            # trading_engine.py applies the same cap for final ATR-adjusted sizing.
            if action == "buy" and price < 5.0:
                from services import trading_engine as _te
                _penny_pct = float(_te._risk_settings.get("max_penny_position_pct", 3.0)) / 100.0
                effective_max_position = effective_portfolio * _penny_pct
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
                final_qty = max(1, int(max_shares * size_pct)) if max_shares > 0 else 0

            cost = price * final_qty
            if action == "buy" and final_qty < 1:
                # ── Auto-rotation: sell weakest long to fund this trade ────────
                # Don't rely on Claude to explicitly output a rotation sell —
                # if cash is insufficient for an approved trade, find the lowest
                # P&L long position and sell it automatically.
                _rotation_done = False
                _long_positions = [
                    p for p in positions
                    if getattr(p, "side", "long") == "long"
                    and p.symbol not in sold_this_cycle
                    and p.symbol != sym
                ]
                if _long_positions:
                    # ── Score each position's momentum to find the best rotation candidate ──
                    # Uses the same signals as the rotation context sent to Claude in Step 2:
                    # P&L %, RSI, and MACD histogram. Lower score = better to sell.
                    #
                    # Tier 0 — WEAK:     losing money OR overbought+falling → sell first
                    # Tier 1 — FLAT:     barely moving, MACD near zero → sell if new trade is high/medium confidence
                    # Tier 2 — MODERATE: small gain, mixed signals → only sell for high confidence new trades
                    # Tier 3 — STRONG:   profitable + positive MACD → never auto-rotate
                    #
                    # Confidence gate: don't disrupt a MODERATE position for a medium/low
                    # confidence new trade. Only rotate STRONG positions for nothing.
                    def _momentum_score(p) -> tuple:
                        _d = deep_data.get(p.symbol) or market_snapshot.get(p.symbol, {})
                        _ind = compute_all(_d.get("closing_prices", [])) if _d.get("closing_prices") else {}
                        _rsi = _ind.get("rsi", 50) or 50
                        _macd = (_ind.get("macd") or {}).get("histogram", 0) or 0
                        _pl = p.unrealized_pl_percent
                        if _pl > 5 and _macd > 0:
                            tier = 3  # STRONG — do not rotate
                        elif _pl < -2 or (_rsi > 70 and _macd < 0):
                            tier = 0  # WEAK — rotate first
                        elif _pl < 1 and abs(_macd) < 0.05:
                            tier = 1  # FLAT — rotate if confidence allows
                        else:
                            tier = 2  # MODERATE — only for high confidence
                        # Secondary sort: within same tier, lowest P&L exits first
                        return (tier, _pl)

                    # Filter by what the incoming confidence level allows us to touch
                    _max_tier = {"high": 2, "medium": 1, "low": -1}.get(confidence, 1)
                    _candidates = [p for p in _long_positions if _momentum_score(p)[0] <= _max_tier]

                    _weakest = min(_candidates, key=_momentum_score) if _candidates else None

                    if _weakest:
                        _w_tier, _w_pl = _momentum_score(_weakest)
                        _tier_labels = {0: "WEAK", 1: "FLAT", 2: "MODERATE", 3: "STRONG"}
                        _sell_price = (
                            (deep_data.get(_weakest.symbol) or market_snapshot.get(_weakest.symbol, {})).get("current_price")
                            or _weakest.current_price
                        )
                        _sell_qty = max(1, round(float(_weakest.qty)))
                        _proceeds = _sell_price * _sell_qty * 0.80  # 80% buffer (same as Claude-initiated rotations)
                        _new_cash = remaining_cash + _proceeds

                        if int(_new_cash / price) >= 1:
                            # Rotation makes the trade affordable — execute the sell
                            remaining_cash = _new_cash
                            sold_this_cycle.add(_weakest.symbol)
                            _sell_reasoning = (
                                f"[AUTO-ROTATION] {_tier_labels[_w_tier]} position sold to fund {confidence.upper()} "
                                f"confidence {sym}. {_weakest.symbol} momentum: {_tier_labels[_w_tier]} "
                                f"(P&L {_w_pl:+.1f}%) | Freed: ${_proceeds:,.0f}"
                            )
                            decisions.append(TradeDecision(
                                action="sell",
                                symbol=_weakest.symbol,
                                quantity=_sell_qty,
                                reasoning=_sell_reasoning,
                            ))
                            logger.info(
                                f"Auto-rotation: {_weakest.symbol} [{_tier_labels[_w_tier]}, "
                                f"P&L {_w_pl:+.1f}%] → selling x{_sell_qty} "
                                f"→ +${_proceeds:,.0f} usable → funding {confidence} {sym} @ ${price:.2f}"
                            )
                            # Recalculate qty with new cash
                            max_shares_by_cash = int(remaining_cash / price)
                            max_shares = min(max_shares_by_strategy, max_shares_by_cash)
                            if qty_suggestion:
                                final_qty = min(int(qty_suggestion), max_shares)
                            else:
                                is_aggressive = current_strategy.get("key") == "aggressive"
                                size_pct = 1.0 if confidence == "high" else (0.75 if is_aggressive else 0.5)
                                final_qty = max(1, int(max_shares * size_pct)) if max_shares > 0 else 0
                            remaining_cash -= price * final_qty
                            _rotation_done = True
                    else:
                        logger.info(
                            f"Auto-rotation skipped for {sym} — no position weak enough "
                            f"to sell for {confidence} confidence trade "
                            f"(all held positions are STRONG or MODERATE)"
                        )

                if not _rotation_done:
                    logger.info(f"Skipping {sym} — insufficient cash (have ${remaining_cash:.0f}, price ${price:.2f}) and no rotation candidate available")
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
            sold_this_cycle.add(sym)  # prevent re-buying this symbol later in the same cycle
            logger.info(f"Rotation sell: {sym} x{final_qty} @ ${price:.2f} → +${proceeds:,.0f} gross / +${buffered_proceeds:,.0f} usable (80% buffer) → remaining cash ${remaining_cash:,.0f}")
        else:
            continue

        reasoning = (
            f"[{confidence.upper()}]{'[ROCKET]' if high_conviction else ''}[STRATEGY:{_strategy_tag(sym, action, signal_for_trade)}] {analysis} "
            f"{f'[PRED:{predictive_expectancy_pct:+.2f}%/{predictive_trades}t] ' if predictive_expectancy_pct is not None and predictive_trades >= 2 else ''}"
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
            high_conviction=high_conviction,
            predictive_expectancy_pct=predictive_expectancy_pct,
            predictive_trades=predictive_trades,
        ))
        logger.info(f"Approved: {action.upper()} {sym} x{final_qty} | TP={take_profit_pct*100:.0f}% SL={stop_loss_pct*100:.0f}%")

    if not decisions:
        return [TradeDecision(action="hold", symbol=None, quantity=None,
                              reasoning=f"All approved trades filtered out by risk/cash checks. Candidates: {', '.join(top_symbols)}")]
    return decisions
