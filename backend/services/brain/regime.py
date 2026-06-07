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
      +1  Breadth > 60% (most stocks participating)
      -1  VIX high (≥20) or extreme (≥30 = -2)
      +1  VIX low (<15)

    Final score:
      ≥ 2  → bull
      ≤ -1 → bear
      else → chop
    """
    notes_parts = []
    score = 0

    vix_level = _classify_vix(vix_value)

    # SPY trend signals
    spy_trend = "at_ma20"
    if spy_prices and len(spy_prices) >= 50:
        current_spy = spy_prices[-1]
        ma20_spy = _moving_average(spy_prices, 20)
        ma50_spy = _moving_average(spy_prices, 50)

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
    elif score <= -1:
        regime = "bear"
        confidence = min(1.0, 0.5 + abs(score) * 0.1)
    else:
        regime = "chop"
        confidence = 0.5

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
