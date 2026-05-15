"""
Pre-breakout scanner — catches stocks BEFORE they move.

The main universe feed (most-actives, movers) only surfaces stocks AFTER they've
already run, guaranteeing the MA20 extension check rejects them. This scanner
looks for stocks building a base near MA20 with rising momentum and volume —
identifying them before the extension that triggers rejection.

Scoring (max 14 points):
  MA20 proximity (0-8% above or slight pullback)  → up to 3 pts
  RSI in build zone (48-67)                        → up to 3 pts
  MACD histogram turning positive                  → up to 3 pts
  Volume confirmation (>= 1.2x)                    → up to 3 pts
  News catalyst present                            → up to 2 pts

Minimum score of 6 required for universe candidates.
EOD watchlist symbols get guaranteed slots (score >= 3) so thesis carries forward.
"""

import logging
from services.indicators import compute_rsi, compute_macd, compute_moving_averages

logger = logging.getLogger(__name__)


def scan_prebreakout_candidates(
    snapshot: dict,
    top_n: int = 6,
    watchlist_symbols: list = None,
    sentiment: dict = None,
) -> list[dict]:
    """
    Scan the market snapshot for stocks setting up for a breakout.

    Args:
        snapshot:          dict of {symbol: {closing_prices, current_price, relative_volume, five_day_change_pct, ...}}
        top_n:             max candidates from the universe scan (watchlist gets separate guaranteed slots)
        watchlist_symbols: EOD watchlist symbols — always included if they have any positive setup (score >= 3)
        sentiment:         {symbol: news_mention_count} — boosts score for stocks with catalysts

    Returns:
        List of candidate dicts sorted by score. Watchlist symbols are flagged separately.
    """
    watchlist_set = set(watchlist_symbols or [])
    sentiment = sentiment or {}
    candidates = []
    watchlist_hits = []

    for symbol, data in snapshot.items():
        closing_prices = data.get("closing_prices", [])
        current_price  = data.get("current_price", 0)
        rel_vol        = data.get("relative_volume", 1.0) or 1.0
        five_day_chg   = data.get("five_day_change_pct") or 0
        news_count     = sentiment.get(symbol, 0)

        # Need at least 20 bars for MA20
        if len(closing_prices) < 20 or not current_price or current_price <= 0:
            continue

        mas = compute_moving_averages(closing_prices)
        ma20 = mas.get("ma20")
        if not ma20 or ma20 <= 0:
            continue

        pct_vs_ma20 = (current_price / ma20 - 1) * 100

        # Only consider stocks in the setup zone
        # Above: 0-10% (approaching breakout, not yet extended)
        # Below: -8% to 0% (pulling back to MA20 — classic re-entry zone)
        if pct_vs_ma20 > 10 or pct_vs_ma20 < -8:
            # Watchlist stocks get a wider window — thesis may still be valid
            if symbol not in watchlist_set or pct_vs_ma20 > 20 or pct_vs_ma20 < -15:
                continue

        rsi = compute_rsi(closing_prices)
        if rsi is None:
            continue

        # Skip clearly weak or clearly overbought RSI
        # Watchlist stocks get slightly more latitude
        rsi_ceiling = 78 if symbol in watchlist_set else 73
        if rsi < 42 or rsi > rsi_ceiling:
            continue

        macd_data = compute_macd(closing_prices)
        macd_hist = macd_data.get("histogram", 0) or 0

        # Skip if momentum is clearly negative
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
        elif symbol in watchlist_set:
            score += 1   # wider window for watchlist — thesis carries forward

        # ── RSI momentum build score ──────────────────────────────────────────
        if 52 <= rsi <= 65:
            score += 3   # sweet spot: building, not overbought
        elif 48 <= rsi < 52:
            score += 2   # just crossing neutral — early momentum
        elif 65 < rsi <= rsi_ceiling:
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

        # ── News catalyst score (why is momentum building?) ──────────────────
        # A technical setup with a news catalyst is far more likely to follow through
        # than one with no identifiable reason.
        if news_count >= 3:
            score += 2   # strong coverage — real catalyst
        elif news_count >= 1:
            score += 1   # some coverage — worth noting

        is_watchlist = symbol in watchlist_set

        entry = {
            "symbol":        symbol,
            "score":         score,
            "pct_vs_ma20":   round(pct_vs_ma20, 1),
            "rsi":           round(rsi, 1),
            "macd_hist":     round(macd_hist, 4),
            "relative_volume": round(rel_vol, 2),
            "five_day_chg":  five_day_chg,
            "news_count":    news_count,
            "current_price": current_price,
            "ma20":          ma20,
            "is_watchlist":  is_watchlist,
        }

        if is_watchlist:
            # Watchlist stocks: guaranteed slot if any positive setup (score >= 3)
            if score >= 3:
                watchlist_hits.append(entry)
        else:
            if score >= 6:
                candidates.append(entry)

    # Sort each group by score
    candidates.sort(key=lambda x: x["score"], reverse=True)
    watchlist_hits.sort(key=lambda x: x["score"], reverse=True)

    # Watchlist stocks first (guaranteed), then top universe candidates
    combined = watchlist_hits + candidates[:top_n]

    if combined:
        logger.info(
            f"Pre-breakout scan: {len(watchlist_hits)} watchlist + {min(len(candidates), top_n)} universe = {len(combined)} total — "
            + ", ".join(
                f"{'📋' if c['is_watchlist'] else '🔍'}{c['symbol']}(score={c['score']},RSI={c['rsi']},MA20{c['pct_vs_ma20']:+.1f}%,5d={c['five_day_chg']}%)"
                for c in combined
            )
        )
    else:
        logger.info("Pre-breakout scan: no qualifying candidates this cycle")

    return combined


def format_for_prompt(candidates: list[dict]) -> str:
    """
    Format pre-breakout candidates for injection into Claude's Step 1 prompt.
    Watchlist stocks shown first with their EOD thesis context.
    """
    if not candidates:
        return ""

    watchlist = [c for c in candidates if c.get("is_watchlist")]
    universe  = [c for c in candidates if not c.get("is_watchlist")]

    lines = []

    if watchlist:
        lines.append("📋 EOD WATCHLIST SETUPS — yesterday's thesis + today's technicals confirming:")
        for c in watchlist:
            direction = "above" if c["pct_vs_ma20"] >= 0 else "below"
            news_note = f", {c['news_count']} news mentions" if c["news_count"] > 0 else ""
            lines.append(
                f"  • {c['symbol']}: {abs(c['pct_vs_ma20']):.1f}% {direction} MA20 (${c['ma20']:.2f}), "
                f"RSI={c['rsi']}, MACD={c['macd_hist']}, vol={c['relative_volume']}x, "
                f"5d={c['five_day_chg']}%{news_note} [WATCHLIST — prioritise if signals confirm]"
            )

    if universe:
        lines.append("🎯 PRE-BREAKOUT SETUPS — near MA20, momentum building, caught early:")
        for c in universe:
            direction = "above" if c["pct_vs_ma20"] >= 0 else "below"
            news_note = f", {c['news_count']} news" if c["news_count"] > 0 else ""
            lines.append(
                f"  • {c['symbol']}: {abs(c['pct_vs_ma20']):.1f}% {direction} MA20 (${c['ma20']:.2f}), "
                f"RSI={c['rsi']}, MACD={c['macd_hist']}, vol={c['relative_volume']}x, "
                f"5d={c['five_day_chg']}%{news_note} [score {c['score']}/14]"
            )

    if lines:
        lines.append("Enter these BEFORE they extend — not after MA20 rejection.")

    return "\n".join(lines)
