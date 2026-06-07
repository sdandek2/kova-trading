"""
Phase 2 — Unified signal stack.

Scores every stock in the universe 0-100 before Claude sees them.
Claude only evaluates the top candidates — no more wasting tokens on noise.

Scoring breakdown (max 100):
  RS rank          +25 (top 20%) / +15 (top 40%) / 0 (top 60%) / -20 (below 60%)
  MACD             +10 (positive+rising) / 0 (flat) / -15 (negative+falling)
  Volume anomaly   +15 (2x+) / +8 (1.5x) / -10 (<0.8x)
  News sentiment   +5 per mention, max +20
  Regime align     +20 (signal matches regime) / -15 (signal fights regime)
  RSI zone         +15 (oversold <35, reversal) / -10 (overbought >75, long entry)
  Price vs MA20    +10 (above MA20, confirmed) / -5 (far below MA20 for momentum)

External data (Phase 7, added when API keys available):
  Options flow     +25 (strong unusual call/put activity)
  Dark pool        +25 (large institutional print)
  Earnings revision +20 (estimate raised) / -20 (estimate cut)
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ScoredCandidate:
    symbol: str
    score: int                    # 0-100 conviction score
    signal_type: str              # "momentum" | "breakout" | "reversal" | "oversold" | "short_candidate" | "inverse_etf"
    suggested_action: str         # "buy" | "short" | "skip"
    price: float
    rsi: Optional[float]
    macd_hist: Optional[float]
    rs_percentile: float
    rel_volume: float
    regime_aligned: bool
    score_breakdown: dict = field(default_factory=dict)
    notes: str = ""
    closing_prices: list = field(default_factory=list)   # for ATR/Kelly sizing in ai_brain
    high_prices: list = field(default_factory=list)
    low_prices: list = field(default_factory=list)

    @property
    def is_strong(self) -> bool:
        return self.score >= 60

    @property
    def is_tradeable(self) -> bool:
        return self.score >= 45 and self.suggested_action != "skip"


def _safe_rsi(prices: list[float]) -> Optional[float]:
    try:
        from services.indicators import compute_rsi
        return compute_rsi(prices)
    except Exception:
        return None


def _safe_macd(prices: list[float]) -> dict:
    try:
        from services.indicators import compute_macd
        return compute_macd(prices) or {}
    except Exception:
        return {}


def _safe_ma20(prices: list[float]) -> Optional[float]:
    if len(prices) < 20:
        return None
    return sum(prices[-20:]) / 20


def _score_symbol(
    symbol: str,
    data: dict,
    regime_result,          # RegimeResult from brain/regime.py
    rs_score,               # RSScore from brain/rs_ranking.py (or None)
    sentiment: dict,
    news_headlines: list,
) -> ScoredCandidate:
    prices = data.get("closing_prices", [])
    high_prices = data.get("high_prices", [])
    low_prices = data.get("low_prices", [])
    price = data.get("current_price") or (prices[-1] if prices else 0)
    rel_vol = data.get("relative_volume", 1.0)
    news_count = (sentiment or {}).get(symbol, 0)

    rsi = _safe_rsi(prices) if len(prices) >= 15 else None
    macd_data = _safe_macd(prices) if len(prices) >= 35 else {}
    macd_hist = macd_data.get("histogram")
    ma20 = _safe_ma20(prices)

    breakdown = {}
    score = 0

    # ── RS rank ──────────────────────────────────────────────────────────────
    rs_pct = rs_score.percentile if rs_score else 50.0
    if rs_pct >= 80:
        breakdown["rs"] = 25
    elif rs_pct >= 60:
        breakdown["rs"] = 15
    elif rs_pct >= 40:
        breakdown["rs"] = 0
    else:
        breakdown["rs"] = -20
    score += breakdown["rs"]

    # ── MACD momentum ────────────────────────────────────────────────────────
    if macd_hist is not None:
        _prev_hist = macd_data.get("prev_histogram")
        _zero_cross = _prev_hist is not None and _prev_hist < 0 and macd_hist >= 0
        if _zero_cross:
            breakdown["macd"] = 20  # strongest reversal signal: crossed from negative to positive
        elif macd_hist > 0.10:
            breakdown["macd"] = 10
        elif macd_hist > 0:
            breakdown["macd"] = 5
        elif macd_hist > -0.10:
            breakdown["macd"] = 0
        else:
            breakdown["macd"] = -15
    else:
        breakdown["macd"] = 0
    score += breakdown["macd"]

    # ── Volume anomaly ────────────────────────────────────────────────────────
    if rel_vol >= 2.0:
        breakdown["volume"] = 15
    elif rel_vol >= 1.5:
        breakdown["volume"] = 8
    elif rel_vol < 0.8:
        breakdown["volume"] = -10
    else:
        breakdown["volume"] = 0
    score += breakdown["volume"]

    # ── News sentiment ────────────────────────────────────────────────────────
    breakdown["news"] = min(news_count * 5, 20)
    score += breakdown["news"]

    # ── RSI zone ─────────────────────────────────────────────────────────────
    if rsi is not None:
        if rsi < 35:
            breakdown["rsi"] = 15   # oversold — reversal/bounce candidate
        elif rsi < 50:
            breakdown["rsi"] = 8    # healthy pullback zone
        elif rsi < 65:
            breakdown["rsi"] = 5    # neutral-bullish
        elif rsi < 75:
            breakdown["rsi"] = 0    # getting stretched
        else:
            breakdown["rsi"] = -10  # overbought — risky long entry
    else:
        breakdown["rsi"] = 0
    score += breakdown["rsi"]

    # ── Price vs MA20 ─────────────────────────────────────────────────────────
    if ma20 and price > 0:
        pct_above = (price / ma20 - 1) * 100
        if 0 <= pct_above <= 5:
            breakdown["ma20"] = 10   # just above MA20 — ideal momentum entry
        elif 5 < pct_above <= 12:
            breakdown["ma20"] = 5    # extended but still reasonable
        elif pct_above > 12:
            breakdown["ma20"] = -5   # overextended
        elif -5 <= pct_above < 0:
            breakdown["ma20"] = 3    # slight pullback — potential reversal
        else:
            breakdown["ma20"] = -5   # well below MA20
    else:
        breakdown["ma20"] = 0
    score += breakdown["ma20"]

    # ── External data boosts (Phase 7 — returns 0 if API key not set) ────────
    try:
        from services.brain.connectors.unusual_whales import get_options_flow
        flow = get_options_flow(symbol)
        if flow.get("signal") not in ("unavailable", "neutral"):
            breakdown["options_flow"] = flow.get("conviction_boost", 0)
            score += breakdown["options_flow"]
    except Exception:
        pass

    try:
        from services.brain.connectors.fmp import get_estimate_revision
        rev = get_estimate_revision(symbol)
        if rev.get("signal") not in ("unavailable", "unchanged"):
            breakdown["earnings_rev"] = rev.get("conviction_boost", 0)
            score += breakdown["earnings_rev"]
    except Exception:
        pass

    try:
        from services.brain.connectors.quiver import get_darkpool_signal
        dp = get_darkpool_signal(symbol)
        if dp.get("signal") not in ("unavailable", "neutral"):
            breakdown["darkpool"] = dp.get("conviction_boost", 0)
            score += breakdown["darkpool"]
    except Exception:
        pass

    # ── Determine signal type and action ─────────────────────────────────────
    regime = regime_result.regime if regime_result else "chop"
    is_leveraged = symbol in _LEVERAGED_ETFS
    is_inverse = symbol in _INVERSE_ETFS

    if is_inverse:
        signal_type = "inverse_etf"
        suggested_action = "buy" if regime in ("bear", "chop") else "skip"
    elif is_leveraged:
        signal_type = "momentum"
        suggested_action = "buy" if (regime_result and regime_result.allows_leveraged_etfs) else "skip"
    elif rsi is not None and rsi > 70 and macd_hist is not None and macd_hist < 0.5:
        signal_type = "short_candidate"
        suggested_action = "short" if regime in ("bear", "chop") else "skip"
    elif rsi is not None and rsi < 35:
        signal_type = "oversold"
        suggested_action = "buy" if regime in ("bull", "chop") else "skip"
    elif macd_hist is not None and macd_hist > 0.05 and rel_vol >= 1.5:
        signal_type = "breakout"
        suggested_action = "buy"
    elif macd_hist is not None and macd_hist > 0:
        signal_type = "momentum"
        suggested_action = "buy"
    else:
        signal_type = "reversal"
        suggested_action = "skip"

    # ── Regime alignment bonus/penalty ───────────────────────────────────────
    regime_aligned = False
    if regime == "bull" and suggested_action == "buy":
        breakdown["regime"] = 20
        regime_aligned = True
    elif regime == "bear" and suggested_action == "short":
        breakdown["regime"] = 20
        regime_aligned = True
    elif regime == "bear" and suggested_action == "buy" and not is_inverse:
        breakdown["regime"] = -15  # buying longs in bear market
    elif regime == "chop" and signal_type == "oversold":
        breakdown["regime"] = 10   # mean reversion works in chop
        regime_aligned = True
    else:
        breakdown["regime"] = 0
    score += breakdown["regime"]

    # Clamp 0-100
    score = max(0, min(100, score))

    notes_parts = []
    if rsi: notes_parts.append(f"RSI={rsi:.0f}")
    if macd_hist is not None: notes_parts.append(f"MACD={macd_hist:.3f}")
    if rel_vol >= 1.5: notes_parts.append(f"Vol={rel_vol:.1f}x")
    if rs_score: notes_parts.append(f"RS={rs_pct:.0f}p")

    return ScoredCandidate(
        symbol=symbol,
        score=score,
        signal_type=signal_type,
        suggested_action=suggested_action,
        price=price,
        rsi=rsi,
        macd_hist=macd_hist,
        rs_percentile=rs_pct,
        rel_volume=rel_vol,
        regime_aligned=regime_aligned,
        score_breakdown=breakdown,
        notes=" | ".join(notes_parts),
        closing_prices=prices,
        high_prices=high_prices,
        low_prices=low_prices,
    )


# ── Known leveraged/inverse ETF lists (copied from entry_timing.py) ──────────
_LEVERAGED_ETFS = {
    "TQQQ", "SQQQ", "SOXL", "SOXS", "SPXL", "SPXS", "UPRO", "SPXU",
    "TECL", "TECS", "LABU", "LABD", "FNGU", "FNGS", "CURE", "DFEN",
    "TNA", "TZA", "UDOW", "SDOW", "URTY", "SRTY",
}
_INVERSE_ETFS = {
    "SQQQ", "SOXS", "SPXS", "SPXU", "TECS", "LABD", "FNGS",
    "TZA", "SDOW", "SRTY",
}


def score_universe(
    universe_snapshot: dict,
    regime_result,
    rs_map: dict,
    sentiment: dict,
    news_headlines: list,
    top_n: int = 12,
    min_score: int = 35,
) -> list[ScoredCandidate]:
    """
    Score every symbol in universe_snapshot and return top_n candidates.
    Only returns candidates with score >= min_score.
    """
    candidates = []
    for symbol, data in universe_snapshot.items():
        rs_score = (rs_map or {}).get(symbol)
        try:
            c = _score_symbol(symbol, data, regime_result, rs_score, sentiment, news_headlines)
            candidates.append(c)
        except Exception as e:
            logger.debug(f"Signal scoring failed for {symbol}: {e}")

    # ── Mean reversion boost (bull/chop only) ────────────────────────────────
    if regime_result and regime_result.regime in ("bull", "chop"):
        try:
            from services.brain.mean_reversion import scan_for_candidates
            mr_candidates = scan_for_candidates(universe_snapshot, regime_result)
            mr_symbols = {c.symbol for c in mr_candidates}
            for cand in candidates:
                if cand.symbol in mr_symbols:
                    cand.score = min(100, cand.score + 20)
                    cand.signal_type = "mean_reversion"
                    cand.notes += " | MEAN_REV"
            if mr_symbols:
                logger.info("mean_reversion boost applied to: %s", ", ".join(mr_symbols))
        except Exception as e:
            logger.debug("mean_reversion scan failed (non-fatal): %s", e)

    # Sort by score descending
    candidates.sort(key=lambda x: x.score, reverse=True)

    # Filter and take top N
    filtered = [c for c in candidates if c.score >= min_score][:top_n]

    if filtered:
        top_str = ", ".join(f"{c.symbol}({c.score})" for c in filtered[:5])
        logger.info(f"Signal scores — top {len(filtered)}: {top_str}")
    else:
        logger.info("Signal scoring: no candidates above min_score — returning top 7 unfiltered")
        filtered = candidates[:7]

    return filtered


def format_candidates_for_prompt(candidates: list[ScoredCandidate]) -> str:
    """Format scored candidates as a clean prompt section for ai_brain.py."""
    if not candidates:
        return "No candidates above conviction threshold."
    lines = []
    for c in candidates:
        action_tag = f"[{c.suggested_action.upper()}]" if c.suggested_action != "skip" else "[REVIEW]"
        lines.append(
            f"  {c.symbol} {action_tag} score={c.score}/100 [{c.signal_type}]: "
            f"${c.price:.2f} | {c.notes} | RS={c.rs_percentile:.0f}p"
            f"{' | REGIME_ALIGNED' if c.regime_aligned else ''}"
        )
    return "\n".join(lines)
