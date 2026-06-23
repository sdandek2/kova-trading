"""
Phase 2 — AI Brain.

Replaces claude_service.analyze_and_decide().
Key improvements over the old system:
  1. Claude only sees pre-scored candidates (signals.py filtered) — no noise
  2. Regime is authoritative — prompt is tailored to bull/bear/chop
  3. RS percentile shown per candidate — Claude knows market context
  4. Kelly history informs confidence — Claude knows its own track record
  5. Tighter prompts → faster responses, lower cost, fewer hallucinations
  6. Mean reversion strategy included alongside momentum (Phase 4 ready)
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from models.trade import TradeDecision
from services.ai_client import ask_ai_pro, parse_ai_json
from services.brain.signals import ScoredCandidate, format_candidates_for_prompt

logger = logging.getLogger(__name__)


# ── Regime-specific strategy instructions ────────────────────────────────────
_REGIME_INSTRUCTIONS = {
    "bull": (
        "Market is BULLISH. Favor momentum longs and confirmed breakouts. "
        "Leveraged ETFs (SOXL/TQQQ/SPXL) are permitted if VIX is low/normal. "
        "Avoid shorting individual stocks — buy inverse ETFs if hedging. "
        "Size aggressively on high RS, high conviction setups."
    ),
    "bear": (
        "Market is BEARISH. Favor inverse ETFs (SQQQ/SPXU/SDOW/TZA) and short candidates. "
        "For shorts: RSI > 65 AND MACD negative required. "
        "Any long positions must be mean reversion plays (oversold, RSI < 35) with tight stops. "
        "Reduce position sizes — capital preservation matters more than gains today."
    ),
    "chop": (
        "Market is CHOPPY / range-bound. "
        "Best plays: oversold quality stocks bouncing off support (RSI < 40, above 200MA). "
        "Avoid chasing momentum — breakouts fail in chop. "
        "Inverse ETFs acceptable as small hedges. Keep positions small, take profits quickly."
    ),
}


def _build_performance_note(kelly_history: list) -> str:
    if not kelly_history or len(kelly_history) < 5:
        return ""
    wins = [t for t in kelly_history if (t.get("pl_pct") or 0) > 0]
    losses = [t for t in kelly_history if (t.get("pl_pct") or 0) <= 0]
    win_rate = len(wins) / len(kelly_history) * 100
    avg_win = sum(t.get("pl_pct", 0) for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.get("pl_pct", 0) for t in losses) / len(losses) if losses else 0
    return (
        f"\n## Your Actual Performance ({len(kelly_history)} closed trades)\n"
        f"Win rate: {win_rate:.0f}% | Avg win: +{avg_win:.1f}% | Avg loss: {avg_loss:.1f}%\n"
        f"Use this to size — high win-rate setups deserve larger positions.\n"
    )


def _build_positions_note(positions: list) -> str:
    if not positions:
        return "Open positions: None"
    lines = []
    for p in positions:
        side = getattr(p, "side", "long")
        pnl = getattr(p, "unrealized_pl_percent", 0)
        short_note = " ⚠️ SHORT — managed by engine, do NOT issue sell/buy" if side == "short" else ""
        lines.append(
            f"  {p.symbol} [{side.upper()}]: {p.qty} shares @ ${p.avg_entry_price:.2f} "
            f"P&L: {pnl:+.1f}%{short_note}"
        )
    return "Open positions:\n" + "\n".join(lines)


def decide(
    scored_candidates: list[ScoredCandidate],
    positions: list,
    account_cash: float,
    portfolio_value: float,
    regime_result,               # RegimeResult
    rs_map: dict,
    kelly_history: list,
    strategy: dict,
    earnings_map: dict = None,
    news_headlines: list = None,
    afternoon_pressure: bool = False,
    eod_context: str = "",
    rotation_context: str = "",
    prompt_override: str = "",
    urgent_news_context: list = None,
    trading_window: str = "regular",  # "premarket" | "regular" | "afterhours"
    risk_settings: dict = None,
) -> list[TradeDecision]:
    """
    Main entry point. Takes pre-scored candidates from signals.py and
    asks Claude to approve the best 1-3 trades.

    Returns list of TradeDecision objects ready for trading_engine to execute.
    """
    if not scored_candidates:
        return [TradeDecision(action="hold", symbol=None, quantity=None,
                              reasoning="Signal stack found no candidates above conviction threshold.")]

    regime = regime_result.regime if regime_result else "chop"
    regime_instructions = _REGIME_INSTRUCTIONS.get(regime, _REGIME_INSTRUCTIONS["chop"])
    vix_note = f"VIX: {regime_result.vix_level.upper()}" if regime_result else ""
    breadth_note = f"Breadth: {regime_result.breadth_pct:.0f}% above MA20" if regime_result else ""

    cash_pct = (account_cash / portfolio_value * 100) if portfolio_value > 0 else 0
    max_pos = portfolio_value * strategy.get("max_position_pct", 0.15)

    perf_note = _build_performance_note(kelly_history)
    positions_note = _build_positions_note(positions)
    candidates_text = format_candidates_for_prompt(scored_candidates)

    urgent_note = ""
    if urgent_news_context:
        _window_instructions = {
            "premarket":  "Use LIMIT orders only (pre-market 8:30–9:30 AM ET). Set limit price at or slightly above last price.",
            "afterhours": "Use LIMIT orders only (after-hours 4:00–6:00 PM ET). Focus on earnings plays only — wider spreads expected.",
            "regular":    "Normal market hours — market or limit orders as appropriate.",
        }
        _win_note = _window_instructions.get(trading_window, _window_instructions["regular"])
        lines = [f"## ⚡ BREAKING NEWS — This cycle was triggered by live news events [{trading_window.upper()}]"]
        for event in urgent_news_context[-3:]:
            lines.append(f"  • {event['reason']}")
        lines.append(f"Order type guidance: {_win_note}")
        lines.append("Prioritise candidates whose symbols appear in the news above.")
        urgent_note = "\n".join(lines) + "\n"

    afternoon_note = (
        "\n📊 AFTERNOON NOTE: Fewer than 2 trades today. Consider medium-confidence "
        "setups you might otherwise skip — only if signal is genuine.\n"
    ) if afternoon_pressure else ""

    _long_count = len([p for p in positions if getattr(p, "side", "long") == "long"])
    # risk_settings takes precedence; 0 means no limit (very large number)
    _rs = risk_settings or {}
    _max_positions_raw = _rs.get("max_positions") if _rs.get("max_positions") is not None else strategy.get("max_positions", 6)
    _max_positions = int(_max_positions_raw) if _max_positions_raw else 0
    _no_pos_limit = _max_positions == 0
    thin_portfolio = _long_count < 2
    thin_note = (
        f"\n⚠️ PORTFOLIO THIN: only {len(positions)} positions open. "
        "Prioritise building positions on any quality setup.\n"
    ) if thin_portfolio else ""
    full_portfolio_note = (
        f"\n🚫 PORTFOLIO FULL: {_long_count}/{_max_positions} positions open. "
        "Do NOT open new BUY positions — only SELL/rotate or HOLD.\n"
    ) if (not _no_pos_limit and _long_count >= _max_positions) else ""

    prompt = f"""You are managing a real portfolio. Evaluate the pre-screened candidates below and approve 1-3 trades.

## Market Regime: {regime.upper()} ({regime_result.confidence:.0%} confidence)
{regime_instructions}
{vix_note} | {breadth_note}
{afternoon_note}{thin_note}{full_portfolio_note}
## Portfolio
Value: ${portfolio_value:,.2f} | Cash: ${account_cash:,.2f} ({cash_pct:.0f}%) | Max per position: ${max_pos:,.2f}
Strategy: {strategy.get('name')} — {strategy.get('prompt_modifier', '')}
{positions_note}

{urgent_note}{rotation_context}
{perf_note}
{eod_context}
## Pre-Screened Candidates (signal score / 100 — higher = stronger setup)
{candidates_text}

## Score guide
60-100: Strong setup — approve unless earnings risk or conflicting position
45-59:  Moderate — approve if regime aligned and no red flags
< 45:   Weak — skip unless no better options exist

## Rules
- BUY: long position. Only if suggested_action=buy or regime=bull.
- SHORT: requires RSI > 65 AND MACD < 0.5 AND regime=bear or chop.
- SELL: close an existing long for rotation. Rotate when new candidate signal score is 15+ points above the existing position's current signal score shown in Rotation Opportunities. Always output SELL before BUY so cash is freed first.
- Inverse ETFs (SQQQ/SPXU/TZA/SDOW): always BUY action — they profit from falls.
- Leveraged ETFs: only in BULL regime with low/normal VIX.
- NEVER issue sell or buy on a [SHORT] position — engine manages those.
- BUY on an already-held symbol = adding to a winner (pyramid). Only do this if the existing position is well below the per-position size cap AND the signal is still strong (score ≥ 65). Never add if already at full size.
- take_profit_pct: 0.05-0.09 stocks (9% hard cap enforced by engine), 0.10-0.20 leveraged ETFs, 0.06-0.09 inverse ETFs
- stop_loss_pct: 0.03-0.05 (tight — cut losers fast)
- partial_exit: true when confidence=high AND score≥75 AND strong trend likely to extend (RS≥85, MACD strongly positive) — sells half at TP, lets other half run with trailing stop to capture multi-bagger moves. Use false for medium/low confidence or weak trend signals.
- confidence=high: signal score ≥ 70 — strong multi-signal confluence, approve at full Kelly size
- confidence=medium: signal score 55-69 — decent setup, approve at 75% size
- confidence=low: signal score < 55 or earnings today or multiple red flags — skip unless nothing better exists
{f"⛔ Earnings risk stocks (avoid or tiny position only): {', '.join(k for k,v in (earnings_map or {}).items() if v == 'today/tomorrow')}" if earnings_map else ""}

Return valid JSON only — no markdown:
{{"trades": [{{"symbol": "X", "action": "buy|short|sell", "confidence": "high|medium|low", "quantity_suggestion": integer, "take_profit_pct": float, "stop_loss_pct": float, "partial_exit": boolean, "analysis": "catalyst + why this action in 1 sentence"}}], "skipped": "brief reason for any skipped candidates"}}"""

    if prompt_override:
        prompt += f"\n\n## Operator Override\n{prompt_override}"

    # ── Signal-score baseline: log what a pure signal-only system would do ──────
    # This lets us compare Claude's decisions vs raw signal scores after 60 days.
    # BUY if signal_score >= 55, SKIP otherwise. Logged before Claude is called.
    try:
        from services.db import log_bot_activity as _lba
        for _c in scored_candidates:
            _baseline_action = "buy" if _c.score >= 55 else "skip"
            _lba("signal_baseline",
                 f"signal_only={_baseline_action} score={_c.score:.0f} "
                 f"side={_c.suggested_action}",
                 symbol=_c.symbol)
    except Exception as _be:
        logger.debug(f"signal_baseline logging failed (non-fatal): {_be}")

    try:
        raw = ask_ai_pro(prompt, max_tokens=2000)
        data = parse_ai_json(raw)
        approved = data.get("trades", [])
        skipped = data.get("skipped", "")
        logger.info(f"AI Brain approved {len(approved)} trades: {[t.get('symbol') for t in approved]} | Skipped: {skipped}")

        # ── Claude override logging ───────────────────────────────────────────
        # If Claude skipped a candidate with signal_score >= 65, log as claude_override.
        # After 60 days: if > 30% of high-score signals were overridden, Claude kills alpha.
        try:
            from services.db import log_bot_activity as _lba2
            _approved_syms = {t.get("symbol") for t in approved}
            for _c in scored_candidates:
                if _c.score >= 65 and _c.symbol not in _approved_syms:
                    _lba2("claude_override",
                          f"Claude skipped score={_c.score:.0f} "
                          f"side={_c.suggested_action} (signal said buy, AI said no)",
                          symbol=_c.symbol)
                    logger.info(f"claude_override: {_c.symbol} score={_c.score:.0f} skipped by AI")
        except Exception as _oe:
            logger.debug(f"claude_override logging failed (non-fatal): {_oe}")

        # Save prompt for viewer
        try:
            from services.db import set_setting as _ss
            _ss("last_prompts", {"step1": "(signals.py pre-scored)", "step2": prompt,
                                  "saved_at": datetime.now(timezone.utc).isoformat()})
        except Exception:
            pass

    except Exception as e:
        logger.error(f"AI Brain failed: {e}")
        return [TradeDecision(action="hold", symbol=None, quantity=None,
                              reasoning=f"AI Brain error: {str(e)[:80]}. Holding.")]

    if not approved:
        return [TradeDecision(action="hold", symbol=None, quantity=None,
                              reasoning=f"No trades approved. Candidates: {[c.symbol for c in scored_candidates[:5]]}")]

    # ── Convert to TradeDecision objects ──────────────────────────────────────
    decisions = []
    remaining_cash = account_cash
    strategy_key = strategy.get("key", "aggressive")
    default_tp = strategy.get("default_take_profit_pct", 0.20)
    default_sl = strategy.get("default_stop_loss_pct", 0.04)
    min_confidence = strategy.get("min_confidence", "medium")
    confidence_rank = {"high": 2, "medium": 1, "low": 0}

    # Sells first so rotation proceeds are available for buys
    approved_sorted = sorted(approved, key=lambda t: 0 if t.get("action") == "sell" else 1)

    for trade in approved_sorted:
        sym = trade.get("symbol")
        action = trade.get("action", "hold")
        confidence = trade.get("confidence", "medium")

        if not sym or action not in ("buy", "short", "sell"):
            continue
        if confidence_rank.get(confidence, 0) < confidence_rank.get(min_confidence, 1):
            logger.info(f"Skipping {sym} — confidence {confidence} below minimum {min_confidence}")
            continue

        def _safe_pct(val, default):
            try:
                return float(str(val).replace("%", "").strip()) if val is not None else default
            except (ValueError, TypeError):
                return default

        # TP cap: wider for leveraged ETFs
        from services.entry_timing import _LEVERAGED_ETFS as _lev
        tp_cap = 0.80 if sym in _lev else 0.60
        take_profit_pct = max(0.05, min(_safe_pct(trade.get("take_profit_pct"), default_tp), tp_cap))
        stop_loss_pct = max(0.02, min(_safe_pct(trade.get("stop_loss_pct"), default_sl), 0.10))
        partial_exit = bool(trade.get("partial_exit", False))
        analysis = trade.get("analysis", "")
        qty_suggestion = trade.get("quantity_suggestion")

        candidate = None      # reset each iteration — sell branch never sets this
        _existing_pos = None  # reset; only populated for buy actions
        if action in ("buy", "short"):
            # Hard guards for BUY actions
            if action == "buy":
                _max_pos_dollars = portfolio_value * strategy.get("max_position_pct", 0.15)
                _existing_pos = next(
                    (p for p in positions if p.symbol == sym and getattr(p, "side", "long") == "long"),
                    None
                )
                if _existing_pos:
                    # Add-on buy (pyramid): block only if zero room left under the cap.
                    # Sizing logic below will naturally cap shares to what fits in the remaining room.
                    _existing_value = float(_existing_pos.qty) * float(
                        getattr(_existing_pos, "current_price", None)
                        or getattr(_existing_pos, "avg_entry_price", 0) or 0
                    )
                    _room = _max_pos_dollars - _existing_value
                    if _room <= 0:
                        logger.info(f"Skipping add-on BUY {sym} — position already at cap (${_existing_value:.0f} / ${_max_pos_dollars:.0f})")
                        continue
                    logger.info(f"Add-on BUY {sym} — ${_room:.0f} room remaining under cap")
                else:
                    # New position — check max positions limit (0 = no limit)
                    if not _no_pos_limit:
                        _open_longs = len([p for p in positions if getattr(p, "side", "long") == "long"])
                        _buys_so_far = sum(1 for d in decisions if getattr(d, "action", "") == "buy")
                        if _open_longs + _buys_so_far >= _max_positions:
                            logger.info(f"Skipping BUY {sym} — at max positions ({_max_positions})")
                            continue

            # Find pre-scored candidate for this symbol
            candidate = next((c for c in scored_candidates if c.symbol == sym), None)
            price = candidate.price if candidate else 0.0
            if price <= 0:
                logger.warning(f"Skipping {sym} — no price in candidates")
                continue

            # Kelly sizing
            rs_pct = candidate.rs_percentile if candidate else 50.0
            try:
                from services.brain.kelly import kelly_size
                from services.indicators import compute_atr as _atr_fn
                # Use closing prices from the scored candidate (available via universe_snapshot)
                _closes = getattr(candidate, "closing_prices", []) or []
                _highs  = getattr(candidate, "high_prices", _closes) or _closes
                _lows   = getattr(candidate, "low_prices", _closes) or _closes
                _atr = _atr_fn(_highs, _lows, _closes) if len(_closes) >= 15 else 0.0
                signal_type = candidate.signal_type if candidate else None
                _kelly = kelly_size(
                    symbol=sym,
                    signal_type=signal_type,
                    conviction=confidence,
                    portfolio_value=portfolio_value,
                    price=price,
                    atr=_atr,
                    trade_history=kelly_history or [],
                    strategy_key=strategy_key,
                    rs_percentile=rs_pct,
                )
                _full_pos_dollars = portfolio_value * strategy.get("max_position_pct", 0.15)
                if _existing_pos:
                    # Add-on: floor to int — 4.3 shares of room → buy 4, 0.28 → 0 (skip)
                    _used = float(_existing_pos.qty) * float(
                        getattr(_existing_pos, "current_price", None)
                        or getattr(_existing_pos, "avg_entry_price", 0) or 0
                    )
                    _addon_dollars = min(max(0, _full_pos_dollars - _used), remaining_cash)
                    final_qty = int(_addon_dollars / price) if price > 0 else 0
                else:
                    max_by_strategy = int(_full_pos_dollars / price)
                    max_by_cash = int(remaining_cash / price)
                    max_shares = min(max_by_strategy, max_by_cash)
                    if qty_suggestion:
                        final_qty = max(1, min(int(qty_suggestion), max_shares))
                    else:
                        final_qty = max(1, min(_kelly.shares, max_shares))
            except Exception as _ke:
                logger.warning(f"Kelly sizing failed for {sym}: {_ke}")
                _full_pos_dollars = portfolio_value * strategy.get("max_position_pct", 0.15)
                if _existing_pos:
                    _used = float(_existing_pos.qty) * float(
                        getattr(_existing_pos, "current_price", None)
                        or getattr(_existing_pos, "avg_entry_price", 0) or 0
                    )
                    _addon_dollars = min(max(0, _full_pos_dollars - _used), remaining_cash)
                    final_qty = int(_addon_dollars / price) if price > 0 else 0
                else:
                    max_by_strategy = int(_full_pos_dollars / price)
                    max_by_cash = int(remaining_cash / price)
                    max_shares = min(max_by_strategy, max_by_cash)
                    size_pct = 1.0 if confidence == "high" else 0.75
                    final_qty = max(1, int(max_shares * size_pct))

            if action == "buy" and final_qty <= 0:
                logger.info(f"Skipping {sym} — insufficient cash or no room")
                continue
            if action == "buy":
                remaining_cash -= price * final_qty

        elif action == "sell":
            pos = next((p for p in positions if p.symbol == sym and getattr(p, "side", "long") == "long"), None)
            if not pos:
                logger.warning(f"Sell skipped for {sym} — no long position found")
                continue
            final_qty = max(1, round(float(pos.qty)))
            price = pos.current_price or 0
            # 0.75 haircut: sell proceeds aren't settled when the next buy is sized.
            # At <$100k this costs ~1 share per rotation — negligible.
            # TODO(scale@$100k): replace with two-pass execution — execute sell first,
            # confirm fill, re-query Alpaca cash, then call decide() for the buy.
            # At $1M, 1 rotation/day leaves ~$37,500 idle per cycle (~$18k/yr in missed gains).
            remaining_cash = max(0.0, remaining_cash + price * final_qty * 0.75)
        else:
            continue

        reasoning = (
            f"[{confidence.upper()}] {analysis} "
            f"TP={take_profit_pct*100:.0f}% | SL={stop_loss_pct*100:.0f}%"
            f"{' | partial exit' if partial_exit else ''}."
        )
        # Classify holding period for options routing:
        # high-conviction + swing signal → options candidate; everything else → intraday (stock)
        _sig_type = candidate.signal_type if candidate else "momentum"
        _holding = (
            "swing"
            if confidence == "high" and _sig_type not in ("inverse_etf",)
            else "intraday"
        )
        decisions.append(TradeDecision(
            action=action,
            symbol=sym,
            quantity=final_qty,
            reasoning=reasoning,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
            partial_exit=partial_exit,
            holding_period=_holding,
            signal_type=_sig_type,
        ))
        logger.info(f"Brain approved: {action.upper()} {sym} x{final_qty} TP={take_profit_pct*100:.0f}% SL={stop_loss_pct*100:.0f}%")

    if not decisions:
        return [TradeDecision(action="hold", symbol=None, quantity=None,
                              reasoning="All approved trades filtered by risk/cash checks.")]
    return decisions
