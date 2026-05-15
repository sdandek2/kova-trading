"""
Pre-breakout scanner — catches stocks BEFORE they move.

The main universe feed (most-actives, movers) only surfaces stocks AFTER they've
already run, guaranteeing the MA20 extension check rejects them. This scanner
looks for stocks building a base near MA20 with rising momentum and volume —
identifying them before the extension that triggers rejection.

Scoring (max 12 points):
  MA20 proximity (0-8% above or slight pullback)  → up to 3 pts
  RSI in build zone (48-67)                        → up to 3 pts
  MACD histogram turning positive                  → up to 3 pts
  Volume confirmation (>= 1.2x)                    → up to 3 pts

Minimum score of 6 required to surface as a candidate.
"""

import logging
from services.indicators import compute_rsi, compute_macd, compute_moving_averages

logger = logging.getLogger(__name__)


def scan_prebreakout_candidates(snapshot: dict, top_n: int = 6) -> list[dict]:
    """
    Scan the market snapshot for stocks setting up for a breakout.

    Args:
        snapshot: dict of {symbol: {closing_prices, current_price, relative_volume, ...}}
        top_n:    max candidates to return (sorted by score descending)

    Returns:
        List of candidate dicts with symbol, score, and key signal values.
    """
    candidates = []

    for symbol, data in snapshot.items():
        closing_prices = data.get("closing_prices", [])
        current_price = data.get("current_price", 0)
        rel_vol = data.get("relative_volume", 1.0) or 1.0

        # Need at least 20 bars for MA20
        if len(closing_prices) < 20 or not current_price or current_price <= 0:
            continue

        mas = compute_moving_averages(closing_prices)
        ma20 = mas.get("ma20")
        if not ma20 or ma20 <= 0:
            continue

        pct_vs_ma20 = (current_price / ma20 - 1) * 100

        # Only consider stocks in the setup zone — not already extended, not in freefall
        # Above: 0-10% (approaching breakout but not yet extended)
        # Below: -8% to 0% (pulling back to MA20 — classic re-entry zone)
        if pct_vs_ma20 > 10 or pct_vs_ma20 < -8:
            continue

        rsi = compute_rsi(closing_prices)
        if rsi is None:
            continue

        # Skip clearly weak or clearly overbought RSI
        if rsi < 42 or rsi > 73:
            continue

        macd_data = compute_macd(closing_prices)
        macd_hist = macd_data.get("histogram", 0) or 0

        # Skip if momentum is clearly negative (more than mild dip)
        if macd_hist < -0.08:
            continue

        score = 0

        # ── MA20 proximity score ──────────────────────────────────────────────
        if 0 <= pct_vs_ma20 <= 5:
            score += 3   # ideal: just above MA20, room to run
        elif 5 < pct_vs_ma20 <= 10:
            score += 1   # slightly extended but still pre-breakout
        elif -5 <= pct_vs_ma20 < 0:
            score += 2   # pullback to MA20 — potential bounce entry

        # ── RSI momentum build score ──────────────────────────────────────────
        if 52 <= rsi <= 65:
            score += 3   # sweet spot: building, not overbought
        elif 48 <= rsi < 52:
            score += 2   # just crossing neutral — early momentum
        elif 65 < rsi <= 73:
            score += 1   # momentum strong but approaching ceiling

        # ── MACD histogram turning score ─────────────────────────────────────
        if macd_hist > 0.03:
            score += 3   # clearly positive — momentum confirmed
        elif macd_hist > 0:
            score += 2   # just turned positive — inflection point
        elif macd_hist > -0.03:
            score += 1   # nearly zero — about to turn

        # ── Volume confirmation score ─────────────────────────────────────────
        if rel_vol >= 1.8:
            score += 3   # strong accumulation
        elif rel_vol >= 1.3:
            score += 2   # above average — interest building
        elif rel_vol >= 1.0:
            score += 1   # normal — not a red flag

        if score >= 6:
            candidates.append({
                "symbol": symbol,
                "score": score,
                "pct_vs_ma20": round(pct_vs_ma20, 1),
                "rsi": round(rsi, 1),
                "macd_hist": round(macd_hist, 4),
                "relative_volume": round(rel_vol, 2),
                "current_price": current_price,
                "ma20": ma20,
            })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    top = candidates[:top_n]

    if top:
        logger.info(
            f"Pre-breakout scan: {len(top)} candidates — "
            + ", ".join(f"{c['symbol']}(score={c['score']},RSI={c['rsi']},MA20{c['pct_vs_ma20']:+.1f}%)" for c in top)
        )
    else:
        logger.info("Pre-breakout scan: no qualifying candidates this cycle")

    return top


def format_for_prompt(candidates: list[dict]) -> str:
    """
    Format pre-breakout candidates for injection into Claude's Step 1 prompt.
    These are shown ABOVE the main market universe so Claude prioritises them.
    """
    if not candidates:
        return ""

    lines = [
        "🎯 PRE-BREAKOUT CANDIDATES — caught EARLY, near MA20, momentum building:",
        "   These are set up BEFORE the move — enter now, not after they've extended.",
    ]
    for c in candidates:
        direction = "above" if c["pct_vs_ma20"] >= 0 else "below"
        lines.append(
            f"  • {c['symbol']}: {abs(c['pct_vs_ma20']):.1f}% {direction} MA20 (${c['ma20']:.2f}), "
            f"RSI={c['rsi']}, MACD hist={c['macd_hist']}, vol={c['relative_volume']}x "
            f"[setup score {c['score']}/12]"
        )
    lines.append("Prioritise these over stocks already extended from MA20.")
    return "\n".join(lines)
