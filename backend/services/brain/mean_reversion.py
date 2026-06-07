"""
Phase 4 — Mean Reversion.

Identifies quality stocks that are temporarily oversold in a bull market.
These snap back fast when the overall market is healthy.

Entry criteria (ALL must pass):
  1. Regime = bull  (never mean-revert into a bear market)
  2. Stock down 8–15% in last 5 days  (oversold, not in freefall)
  3. RSI < 35  (momentum confirmation of oversold condition)
  4. Still above 200-day MA  (long-term uptrend intact — not a broken stock)
  5. Positive earnings revisions OR no revision data  (not a fundamental breakdown)

Exit signals (checked each cycle against open positions):
  - RSI recovers above 50  (momentum restored)
  - Price returns to MA20  (mean achieved)
  - Stock falls below 200-day MA  (thesis broken — hard stop)
  - Max hold 10 days (time stop — snap-back usually happens within a week)
"""
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class MeanReversionCandidate:
    symbol: str
    price: float
    rsi: float
    drop_pct_5d: float          # % decline over last 5 days (positive = drop)
    ma20: float
    ma200: float
    above_ma200: bool
    earnings_revision_ok: bool  # True if positive or unknown (no data)
    conviction: int             # 0-100 score for this candidate
    entry_rationale: str


@dataclass
class MeanReversionExit:
    symbol: str
    should_exit: bool
    reason: str                 # "rsi_recovered" | "price_at_ma20" | "below_ma200" | "time_stop" | "hold"
    current_price: float
    current_rsi: Optional[float]


# ── Indicator helpers ─────────────────────────────────────────────────────────

def _compute_rsi(prices: list[float], period: int = 14) -> Optional[float]:
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(-period, 0):
        delta = prices[i] - prices[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _sma(prices: list[float], period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def _drop_pct(prices: list[float], days: int = 5) -> Optional[float]:
    """Positive return = price dropped. Negative = price rose."""
    if len(prices) < days + 1:
        return None
    start = prices[-(days + 1)]
    end = prices[-1]
    if start == 0:
        return None
    return round((start - end) / start * 100, 2)


def _check_earnings_revision(symbol: str) -> bool:
    """
    Returns True if earnings revisions are positive or unavailable (no FMP key).
    Returns False only if revisions are definitively negative.
    """
    try:
        from services.brain.connectors.fmp import get_estimate_revision
        rev = get_estimate_revision(symbol)
        signal = rev.get("signal", "unavailable")
        # "unavailable" or "raised" → OK to trade; "cut" → skip
        return signal != "cut"
    except Exception:
        return True  # no data = not a known fundamental breakdown


# ── Scanning ──────────────────────────────────────────────────────────────────

def scan_for_candidates(
    universe_snapshot: dict,
    regime_result,
) -> list[MeanReversionCandidate]:
    """
    Scan the universe for mean reversion setups.
    Only runs when regime == "bull" — returns [] otherwise.

    Args:
        universe_snapshot: {symbol: {"closing_prices": [...], "current_price": float, ...}}
        regime_result: RegimeResult from brain/regime.py

    Returns:
        List of MeanReversionCandidate sorted by conviction descending.
    """
    if not regime_result or regime_result.regime != "bull":
        logger.debug("mean_reversion: regime=%s — skipping scan", getattr(regime_result, "regime", "unknown"))
        return []

    candidates: list[MeanReversionCandidate] = []

    for symbol, data in universe_snapshot.items():
        prices = data.get("closing_prices", [])
        price = data.get("current_price") or (prices[-1] if prices else None)

        if not price or len(prices) < 201:
            continue  # need enough history for MA200

        rsi = _compute_rsi(prices)
        ma20 = _sma(prices, 20)
        ma200 = _sma(prices, 200)
        drop_5d = _drop_pct(prices, 5)

        if rsi is None or ma20 is None or ma200 is None or drop_5d is None:
            continue

        # ── Entry filter ──────────────────────────────────────────────────────
        if rsi >= 35:
            continue  # not oversold enough

        if not (8.0 <= drop_5d <= 15.0):
            continue  # too small a dip or in freefall (>15% = broken)

        if price <= ma200:
            continue  # long-term downtrend — not a mean reversion candidate

        earnings_ok = _check_earnings_revision(symbol)
        if not earnings_ok:
            logger.info("mean_reversion: %s skipped — negative earnings revision", symbol)
            continue

        # ── Conviction scoring ────────────────────────────────────────────────
        conviction = 50  # base: passed all filters

        # Deeper oversold = stronger bounce
        if rsi < 25:
            conviction += 20
        elif rsi < 30:
            conviction += 10

        # Bigger dip with clean stop = better risk/reward
        if 10.0 <= drop_5d <= 13.0:
            conviction += 15
        elif 8.0 <= drop_5d < 10.0:
            conviction += 8

        # Close to MA200 = strong support nearby
        pct_above_ma200 = (price / ma200 - 1) * 100
        if pct_above_ma200 <= 5:
            conviction += 10  # near strong support
        elif pct_above_ma200 > 30:
            conviction -= 10  # far from long-term floor — less reliable bounce

        # High VIX regime reduces confidence (indiscriminate selling)
        if regime_result.vix_level in ("high", "extreme"):
            conviction -= 15

        conviction = max(0, min(100, conviction))

        rationale_parts = [
            f"RSI={rsi:.0f}",
            f"drop_5d={drop_5d:.1f}%",
            f"above_MA200={pct_above_ma200:.1f}%",
        ]
        if earnings_ok:
            rationale_parts.append("earnings_revision=OK")

        candidates.append(MeanReversionCandidate(
            symbol=symbol,
            price=price,
            rsi=rsi,
            drop_pct_5d=drop_5d,
            ma20=ma20,
            ma200=ma200,
            above_ma200=True,
            earnings_revision_ok=earnings_ok,
            conviction=conviction,
            entry_rationale=" | ".join(rationale_parts),
        ))

    candidates.sort(key=lambda c: c.conviction, reverse=True)

    if candidates:
        top = candidates[0]
        logger.info(
            "mean_reversion: found %d candidates — best: %s (conviction=%d, %s)",
            len(candidates), top.symbol, top.conviction, top.entry_rationale,
        )
    else:
        logger.debug("mean_reversion: no candidates passed all filters")

    return candidates


# ── Exit evaluation ───────────────────────────────────────────────────────────

def evaluate_exit(
    symbol: str,
    prices: list[float],
    entry_date: date,
    max_hold_days: int = 10,
) -> MeanReversionExit:
    """
    Check whether an open mean reversion position should be exited.

    Args:
        symbol:         ticker of the open position
        prices:         recent closing prices (at least 21 values)
        entry_date:     date the position was opened
        max_hold_days:  time stop (default 10 days)

    Returns:
        MeanReversionExit with should_exit=True and reason when exit is triggered.
    """
    price = prices[-1] if prices else 0.0
    rsi = _compute_rsi(prices)
    ma20 = _sma(prices, 20)
    ma200 = _sma(prices, 200)

    hold_days = (date.today() - entry_date).days

    # Time stop
    if hold_days >= max_hold_days:
        return MeanReversionExit(
            symbol=symbol,
            should_exit=True,
            reason="time_stop",
            current_price=price,
            current_rsi=rsi,
        )

    # Thesis broken: stock fell below 200-day MA
    if ma200 and price < ma200:
        return MeanReversionExit(
            symbol=symbol,
            should_exit=True,
            reason="below_ma200",
            current_price=price,
            current_rsi=rsi,
        )

    # Target achieved: RSI recovered
    if rsi is not None and rsi >= 50:
        return MeanReversionExit(
            symbol=symbol,
            should_exit=True,
            reason="rsi_recovered",
            current_price=price,
            current_rsi=rsi,
        )

    # Target achieved: price returned to MA20
    if ma20 and price >= ma20:
        return MeanReversionExit(
            symbol=symbol,
            should_exit=True,
            reason="price_at_ma20",
            current_price=price,
            current_rsi=rsi,
        )

    return MeanReversionExit(
        symbol=symbol,
        should_exit=False,
        reason="hold",
        current_price=price,
        current_rsi=rsi,
    )


# ── Prompt formatter (used by ai_brain.py) ────────────────────────────────────

def format_for_prompt(candidates: list[MeanReversionCandidate]) -> str:
    """Format mean reversion candidates for inclusion in the AI brain prompt."""
    if not candidates:
        return ""
    lines = ["## Mean Reversion Candidates (oversold quality stocks in bull market)"]
    for c in candidates[:5]:  # cap at 5 to avoid prompt bloat
        lines.append(
            f"  {c.symbol} [MEAN_REVERSION] conviction={c.conviction}/100 "
            f"@ ${c.price:.2f} | {c.entry_rationale}"
        )
    lines.append(
        "Strategy: buy dip, target MA20, stop below MA200. "
        "Only enter if you agree this is a temporary pullback, not fundamental deterioration."
    )
    return "\n".join(lines)
