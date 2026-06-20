"""
Market regime detector.
Classifies the current market as bull / bear / chop.
Used by every other brain module to select appropriate strategy.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RegimeResult:
    regime: str                  # "bull" | "bear" | "chop"
    confidence: float            # 0.0 – 1.0
    vix_level: str               # "low" | "normal" | "high" | "extreme"
    spy_trend: str               # "above_ma20" | "below_ma20" | "at_ma20"
    breadth_pct: float           # % of universe stocks above their own MA20
    score: int                   # raw score used to determine regime (-3 to +3)
    notes: str = ""

    @property
    def is_bull(self) -> bool:
        return self.regime == "bull"

    @property
    def is_bear(self) -> bool:
        return self.regime == "bear"

    @property
    def is_chop(self) -> bool:
        return self.regime == "chop"

    @property
    def allows_leveraged_etfs(self) -> bool:
        return self.regime == "bull" and self.vix_level in ("low", "normal")

    @property
    def allows_new_longs(self) -> bool:
        return self.regime in ("bull", "chop")

    @property
    def allows_shorts(self) -> bool:
        return self.regime in ("bear", "chop")


def _classify_vix(vix_value: Optional[float]) -> str:
    if vix_value is None:
        return "normal"
    if vix_value < 15:
        return "low"
    if vix_value < 20:
        return "normal"
    if vix_value < 30:
        return "high"
    return "extreme"


def _moving_average(prices: list[float], period: int) -> Optional[float]:
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def _breadth(universe_snapshot: dict) -> float:
    """% of universe stocks currently trading above their 20-day MA."""
    above, total = 0, 0
    for sym, data in universe_snapshot.items():
        prices = data.get("closing_prices", [])
        if len(prices) < 21:
            continue
        ma20 = sum(prices[-20:]) / 20
        current = data.get("current_price") or prices[-1]
        total += 1
        if current > ma20:
            above += 1
    if total == 0:
        return 50.0
    return round((above / total) * 100, 1)


def detect_regime(
    spy_prices: list[float],
    vix_value: Optional[float],
    universe_snapshot: dict,
) -> RegimeResult:
    """
    Determine current market regime from SPY trend, VIX, and market breadth.

    Scoring (+1 bull / -1 bear per signal):
      +1  SPY above MA20
      +1  SPY MA20 above MA50 (medium-term uptrend)
      +1  SPY above MA200 (long-term structural uptrend)
      +1  Breadth > 60% (most stocks participating)
      +1  VIX low (<15)
      -1  SPY below MA20
      -1  SPY MA20 below MA50 (downtrend)
      -1  SPY below MA200 (long-term structural downtrend)
      -1  Breadth < 40% (narrow/weak)
      -1  VIX high (≥20), -2 VIX extreme (≥30)

    Final score:
      ≥  2 → bull
      ≤ -2 → bear   (symmetric with bull — requires 2+ confirming bearish signals)
      else → chop    (captures -1 to 1: normal pullbacks, transition periods)
    """
    notes_parts = []
    score = 0

    vix_level = _classify_vix(vix_value)

    # SPY trend signals
    spy_trend = "at_ma20"
    if spy_prices and len(spy_prices) >= 200:
        current_spy = spy_prices[-1]
        ma20_spy  = _moving_average(spy_prices, 20)
        ma50_spy  = _moving_average(spy_prices, 50)
        ma200_spy = _moving_average(spy_prices, 200)

        if ma20_spy:
            if current_spy > ma20_spy * 1.005:
                spy_trend = "above_ma20"
                score += 1
                notes_parts.append("SPY above MA20")
            elif current_spy < ma20_spy * 0.995:
                spy_trend = "below_ma20"
                score -= 1
                notes_parts.append("SPY below MA20")

        if ma20_spy and ma50_spy:
            if ma20_spy > ma50_spy:
                score += 1
                notes_parts.append("MA20>MA50 uptrend")
            else:
                score -= 1
                notes_parts.append("MA20<MA50 downtrend")

        if ma200_spy:
            if current_spy > ma200_spy * 1.005:
                score += 1
                notes_parts.append("SPY above MA200")
            elif current_spy < ma200_spy * 0.995:
                score -= 1
                notes_parts.append("SPY below MA200")

    elif spy_prices and len(spy_prices) >= 50:
        current_spy = spy_prices[-1]
        ma20_spy  = _moving_average(spy_prices, 20)
        ma50_spy  = _moving_average(spy_prices, 50)

        if ma20_spy:
            if current_spy > ma20_spy * 1.005:
                spy_trend = "above_ma20"
                score += 1
                notes_parts.append("SPY above MA20")
            elif current_spy < ma20_spy * 0.995:
                spy_trend = "below_ma20"
                score -= 1
                notes_parts.append("SPY below MA20")

        if ma20_spy and ma50_spy:
            if ma20_spy > ma50_spy:
                score += 1
                notes_parts.append("MA20>MA50 uptrend")
            else:
                score -= 1
                notes_parts.append("MA20<MA50 downtrend")

    elif spy_prices and len(spy_prices) >= 20:
        current_spy = spy_prices[-1]
        ma20_spy = _moving_average(spy_prices, 20)
        if ma20_spy:
            if current_spy > ma20_spy:
                spy_trend = "above_ma20"
                score += 1
            else:
                spy_trend = "below_ma20"
                score -= 1

    # VIX signals
    if vix_level == "low":
        score += 1
        notes_parts.append("VIX low (risk-on)")
    elif vix_level == "high":
        score -= 1
        notes_parts.append("VIX high (caution)")
    elif vix_level == "extreme":
        score -= 2
        notes_parts.append("VIX extreme (risk-off)")

    # Market breadth
    breadth = _breadth(universe_snapshot)
    if breadth > 60:
        score += 1
        notes_parts.append(f"Breadth {breadth:.0f}% (broad participation)")
    elif breadth < 40:
        score -= 1
        notes_parts.append(f"Breadth {breadth:.0f}% (narrow/weak)")
    else:
        notes_parts.append(f"Breadth {breadth:.0f}% (neutral)")

    # Classify regime
    if score >= 2:
        regime = "bull"
        confidence = min(1.0, 0.5 + score * 0.1)
    elif score <= -2:
        regime = "bear"
        confidence = min(1.0, 0.5 + abs(score) * 0.1)
    else:
        regime = "chop"
        confidence = 0.5

    # ── Session 6: FRED macro modifier ────────────────────────────────────────
    # Adjusts confidence (not regime) based on 3-month economic trends.
    # A single bad data point (one jobs report) does NOT change the regime —
    # only sustained deterioration (3+ months) shifts macro_total meaningfully.
    try:
        macro_total = _get_fred_macro_score()
        if macro_total >= 2:
            confidence = min(1.0, confidence + 0.15)
            notes_parts.append(f"FRED macro strong (+{macro_total}) → +15% confidence")
        elif macro_total <= -2:
            confidence = max(0.1, confidence - 0.15)
            notes_parts.append(f"FRED macro weak ({macro_total}) → -15% confidence")
        else:
            notes_parts.append(f"FRED macro neutral ({macro_total:+d})")
    except Exception as _fred_err:
        logger.debug("FRED macro modifier skipped: %s", _fred_err)

    result = RegimeResult(
        regime=regime,
        confidence=round(confidence, 2),
        vix_level=vix_level,
        spy_trend=spy_trend,
        breadth_pct=breadth,
        score=score,
        notes=" | ".join(notes_parts),
    )
    logger.info(
        f"Regime: {regime.upper()} (score={score}, confidence={confidence:.0%}) "
        f"VIX={vix_level} SPY={spy_trend} Breadth={breadth:.0f}% | {result.notes}"
    )
    return result


# ── FRED macro helper ─────────────────────────────────────────────────────────

import time as _time

_FRED_CACHE: dict = {}
_FRED_TTL = 86_400  # 24 hours


def _get_fred_macro_score() -> int:
    """
    Compute macro score from FRED data (-3 to +3).
    Uses 3-month trends, not single readings — resistant to one-off noise.
    Cached 24h.
    """
    global _FRED_CACHE
    if _FRED_CACHE.get("expires", 0) > _time.time():
        return _FRED_CACHE["score"]

    import httpx, csv, io

    def fetch_series(series_id: str) -> list[float]:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        r = httpx.get(url, timeout=10, headers={"User-Agent": "Kova Trading kova@trading.com"})
        r.raise_for_status()
        rows = list(csv.reader(io.StringIO(r.text)))
        values = []
        for row in rows[1:]:  # skip header
            if len(row) >= 2 and row[1] not in (".", "", " "):
                try:
                    values.append(float(row[1]))
                except ValueError:
                    pass
        return values[-6:] if len(values) >= 6 else values  # last 6 months

    # Unemployment rate — rising = bad, falling = good
    unrate = fetch_series("UNRATE")
    unemp_score = 0
    if len(unrate) >= 3:
        trend = unrate[-1] - unrate[-3]
        if trend > 0.3:
            unemp_score = -1
        elif trend < -0.3:
            unemp_score = +1

    # Non-farm payrolls — compute monthly change
    payems = fetch_series("PAYEMS")
    jobs_score = 0
    if len(payems) >= 4:
        monthly_adds = [(payems[i] - payems[i-1]) * 1000 for i in range(-3, 0)]
        avg_adds = sum(monthly_adds) / len(monthly_adds)
        if avg_adds >= 150_000:
            jobs_score = +1
        elif avg_adds < 75_000:
            jobs_score = -1

    # Fed funds rate — cutting = accommodative = good, hiking = restrictive = bad
    fedfunds = fetch_series("FEDFUNDS")
    fed_score = 0
    if len(fedfunds) >= 3:
        fed_trend = fedfunds[-1] - fedfunds[-3]
        if fed_trend < -0.1:
            fed_score = +1   # cutting rates
        elif fed_trend > 0.1:
            fed_score = -1   # hiking rates

    total = unemp_score + jobs_score + fed_score

    _FRED_CACHE = {
        "score": total,
        "expires": _time.time() + _FRED_TTL,
        "unemp": unemp_score,
        "jobs": jobs_score,
        "fed": fed_score,
        "unrate_latest": unrate[-1] if unrate else None,
    }
    logger.info(
        "FRED macro: unemp=%+d jobs=%+d fed=%+d total=%+d "
        "(UNRATE=%.1f%%)",
        unemp_score, jobs_score, fed_score, total,
        unrate[-1] if unrate else 0,
    )
    return total
