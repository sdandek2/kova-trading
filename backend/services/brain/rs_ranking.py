"""
Relative Strength ranking.
Ranks every stock in the universe by performance vs SPY.
Only the top 60th percentile is worth trading — everything below is underperforming the market.

Weighting: recent 4 weeks count 2x so the ranking reflects current momentum,
not a stock that was hot 10 weeks ago and has since faded.
"""
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RSScore:
    symbol: str
    rs_score: float       # positive = outperforming SPY, negative = underperforming
    percentile: float     # 0-100; only trade stocks above 60th percentile
    weeks_12_return: float
    weeks_4_return: float
    is_tradeable: bool    # True if percentile >= 60


def _pct_return(prices: list[float], lookback: int) -> Optional[float]:
    """Return % change over the last `lookback` bars."""
    if len(prices) < lookback + 1:
        return None
    start = prices[-(lookback + 1)]
    end = prices[-1]
    if start == 0:
        return None
    return round((end - start) / start * 100, 2)


def rank_universe(
    universe_snapshot: dict,
    spy_prices: list[float],
) -> list[RSScore]:
    """
    Score every symbol in universe_snapshot relative to SPY.

    RS score formula:
      raw_rs = (symbol_12w_return * 1.0) + (symbol_4w_return * 2.0)
               - (spy_12w_return * 1.0) - (spy_4w_return * 2.0)

    The 2x weight on 4-week return means recent momentum matters more.

    Returns list sorted by rs_score descending (best first).
    """
    if not universe_snapshot:
        return []

    spy_12w = _pct_return(spy_prices, 60) or 0.0  # ~12 weeks of trading days
    spy_4w = _pct_return(spy_prices, 20) or 0.0   # ~4 weeks

    raw_scores: list[tuple[str, float, float, float]] = []

    for symbol, data in universe_snapshot.items():
        prices = data.get("closing_prices", [])
        if len(prices) < 21:
            continue

        sym_12w = _pct_return(prices, min(60, len(prices) - 1)) or 0.0
        sym_4w = _pct_return(prices, min(20, len(prices) - 1)) or 0.0

        rs = (sym_12w * 1.0 + sym_4w * 2.0) - (spy_12w * 1.0 + spy_4w * 2.0)
        raw_scores.append((symbol, round(rs, 2), sym_12w, sym_4w))

    if not raw_scores:
        return []

    # Sort by RS score descending
    raw_scores.sort(key=lambda x: x[1], reverse=True)
    n = len(raw_scores)

    results = []
    for rank, (symbol, rs, w12, w4) in enumerate(raw_scores):
        percentile = round((1 - rank / n) * 100, 1)
        results.append(RSScore(
            symbol=symbol,
            rs_score=rs,
            percentile=percentile,
            weeks_12_return=w12,
            weeks_4_return=w4,
            is_tradeable=(percentile >= 60),
        ))

    tradeable = [r for r in results if r.is_tradeable]
    logger.info(
        f"RS ranking: {len(results)} stocks scored, {len(tradeable)} above 60th percentile | "
        f"SPY 12w={spy_12w:+.1f}% 4w={spy_4w:+.1f}%"
    )
    if results:
        top3 = [f"{r.symbol}({r.rs_score:+.1f})" for r in results[:3]]
        bot3 = [f"{r.symbol}({r.rs_score:+.1f})" for r in results[-3:]]
        logger.info(f"RS top: {top3} | bottom: {bot3}")

    return results


def get_rs_map(rs_ranks: list[RSScore]) -> dict[str, RSScore]:
    """Convert list to symbol-keyed dict for fast lookups."""
    return {r.symbol: r for r in rs_ranks}


def filter_by_rs(
    symbols: list[str],
    rs_map: dict[str, RSScore],
    min_percentile: float = 60.0,
    allow_all_if_empty: bool = True,
) -> list[str]:
    """
    Filter a list of symbols to only those meeting the RS threshold.
    If no symbols pass (very bearish market), returns all if allow_all_if_empty=True
    so the bot doesn't go fully idle.
    """
    passed = [s for s in symbols if rs_map.get(s, RSScore(s, 0, 0, 0, 0, False)).percentile >= min_percentile]
    if not passed and allow_all_if_empty:
        logger.info(f"RS filter: no symbols above {min_percentile}th percentile — returning all {len(symbols)} unfiltered")
        return symbols
    return passed
