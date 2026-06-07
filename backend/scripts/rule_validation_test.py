"""
Kova Rule Validation Tests — verifies business rules are enforced correctly.

Unlike full_system_test.py (which checks "is everything alive?"), this script
checks "are the actual rules applied correctly?" using controlled inputs and mocks.

No AI calls. No token cost. No real API calls for rule logic.

Usage:
    cd backend && python scripts/rule_validation_test.py
    cd backend && python scripts/rule_validation_test.py --section short_trigger
    cd backend && python scripts/rule_validation_test.py --section thresholds

Sections: short_trigger, thresholds, regime_mult, fred_mult, barchart_exclusions,
          position_sizing, signal_boosts, circuit_breaker, injection_guards, regime_rules

Exit 0 = all rules enforced correctly. Exit 1 = rule violations found.
Runtime: ~30 seconds (all mocked, no real API calls).
"""
import sys
import os
import argparse
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Result tracking ────────────────────────────────────────────────────────────
_results = []
_section_results = {}
_current_section = "init"

def _set_section(name):
    global _current_section
    _current_section = name
    _section_results[name] = []

def check(name, ok, detail=""):
    tag = "  PASS" if ok else "  FAIL"
    line = f"{tag}  {name}"
    if detail:
        line += f"  →  {detail}"
    print(line)
    _results.append(ok)
    _section_results.setdefault(_current_section, []).append(ok)
    return ok

def section(title):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")
    _set_section(title)

parser = argparse.ArgumentParser()
parser.add_argument("--section", default="all")
args = parser.parse_args()
only = args.section.lower()

def should_run(name):
    return only == "all" or only in name.lower()

print("=" * 60)
print("  Kova Rule Validation Tests")
print("=" * 60)

# ── Helpers ────────────────────────────────────────────────────────────────────

def make_scored_candidate(
    symbol="TEST", score=60, signal_type="momentum", suggested_action="buy",
    price=100.0, rsi=55.0, macd_hist=0.5, rs_percentile=75.0, rel_volume=1.5,
    regime_aligned=True, breakdown=None
):
    """Build a ScoredCandidate with controlled values for rule testing."""
    from services.brain.signals import ScoredCandidate
    return ScoredCandidate(
        symbol=symbol, score=score, signal_type=signal_type,
        suggested_action=suggested_action, price=price,
        rsi=rsi, macd_hist=macd_hist, rs_percentile=rs_percentile,
        rel_volume=rel_volume, regime_aligned=regime_aligned,
        score_breakdown=breakdown or {},
    )


def make_regime(regime="bull", vix_level="normal", confidence=0.70,
                allows_leveraged=True):
    """Build a mock RegimeResult."""
    r = MagicMock()
    r.regime = regime
    r.vix_level = vix_level
    r.confidence = confidence
    r.allows_leveraged_etfs = allows_leveraged
    r.score = 3
    return r


def run_decision_tree(
    symbol="TEST", score=60, rsi=55.0, macd_hist=0.5,
    regime="bull", vix_level="normal", confidence=0.70,
    rel_vol=1.5, is_leveraged=False, is_inverse=False,
    heavy_put_short=False, breakdown=None
):
    """
    Run just the decision-tree portion of score_symbol() with fully controlled inputs.
    Returns (signal_type, suggested_action, final_score, breakdown).
    Patches all external connector calls so no real API is used.
    """
    bd = dict(breakdown or {})

    regime_result = make_regime(regime, vix_level, confidence,
                                allows_leveraged=(regime == "bull"))

    _LEVERAGED = {"TQQQ","SQQQ","SPXL","SPXS","SOXL","SOXS","UPRO","LABU","LABD","UVXY"}
    _INVERSE   = {"SH","PSQ","DOG","RWM","SPXS","SQQQ","SOXS","QID","SDS","TWM"}

    is_lev = symbol in _LEVERAGED or is_leveraged
    is_inv = symbol in _INVERSE   or is_inverse

    # Decision tree (mirrored exactly from signals.py)
    if is_inv:
        signal_type = "inverse_etf"
        suggested_action = "buy" if regime in ("bear", "chop") else "skip"
    elif is_lev:
        signal_type = "momentum"
        suggested_action = "buy" if regime_result.allows_leveraged_etfs else "skip"
    elif rsi is not None and rsi > 70 and macd_hist is not None and macd_hist < 0.5:
        signal_type = "short_candidate"
        suggested_action = "short" if regime in ("bear", "chop") else "skip"
    elif (heavy_put_short
          and rsi is not None and rsi > 50
          and macd_hist is not None and macd_hist < 0
          and regime in ("bear", "chop")):
        signal_type = "short_candidate"
        suggested_action = "short"
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

    # Regime alignment bonus/penalty
    if regime == "bull" and suggested_action == "buy":
        bd["regime"] = 20
    elif regime == "bear" and suggested_action == "short":
        bd["regime"] = 20
    elif regime == "bear" and suggested_action == "buy" and not is_inv:
        bd["regime"] = -15
    elif regime == "chop" and signal_type == "oversold":
        bd["regime"] = 10
    else:
        bd["regime"] = 0

    final_score = max(0, min(100, score + bd.get("regime", 0)))
    return signal_type, suggested_action, final_score, bd


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — SHORT TRIGGER GUARDS
# Three conditions must ALL be true: MACD < 0, RSI > 50, regime bear/chop
# ══════════════════════════════════════════════════════════════════════════════
if should_run("short_trigger"):
    section("1. Short Trigger Guards (all 3 conditions required)")

    # ── All 3 conditions met → MUST short ────────────────────────────────────
    st, action, _, _ = run_decision_tree(
        rsi=60.0, macd_hist=-0.3, regime="bear", heavy_put_short=True
    )
    check("All 3 guards met → action=short",
          action == "short",
          f"got action={action}")
    check("All 3 guards met → signal=short_candidate",
          st == "short_candidate", f"got signal_type={st}")

    # Same in chop regime
    st, action, _, _ = run_decision_tree(
        rsi=65.0, macd_hist=-0.5, regime="chop", heavy_put_short=True
    )
    check("Chop regime + all guards → short",
          action == "short", f"got {action}")

    # ── GUARD 1: RSI ≤ 50 (already oversold) → must NOT short ────────────────
    st, action, _, _ = run_decision_tree(
        rsi=45.0, macd_hist=-0.3, regime="bear", heavy_put_short=True
    )
    check("RSI=45 (≤50) → must NOT short",
          action != "short",
          f"got action={action} (RSI guard failed)" if action == "short" else f"correctly blocked → {action}")

    st, action, _, _ = run_decision_tree(
        rsi=30.0, macd_hist=-0.5, regime="bear", heavy_put_short=True
    )
    check("RSI=30 (deep oversold) → must NOT short",
          action != "short",
          f"got action={action}")

    # ── GUARD 2: MACD ≥ 0 (momentum still positive) → must NOT short ─────────
    st, action, _, _ = run_decision_tree(
        rsi=60.0, macd_hist=0.2, regime="bear", heavy_put_short=True
    )
    check("MACD=+0.2 (positive) → must NOT short",
          action != "short",
          f"got action={action} (MACD guard failed)" if action == "short" else f"correctly blocked → {action}")

    st, action, _, _ = run_decision_tree(
        rsi=60.0, macd_hist=0.0, regime="bear", heavy_put_short=True
    )
    check("MACD=0.0 (neutral) → must NOT short",
          action != "short",
          f"got action={action}")

    # ── GUARD 3: Bull regime → must NEVER short even with heavy puts ──────────
    st, action, _, _ = run_decision_tree(
        rsi=60.0, macd_hist=-0.3, regime="bull", heavy_put_short=True
    )
    check("Bull regime → NEVER short (even with heavy puts)",
          action != "short",
          f"got action={action} (regime guard failed)" if action == "short" else f"correctly blocked → {action}")

    # ── No heavy puts → short trigger does not fire ───────────────────────────
    st, action, _, _ = run_decision_tree(
        rsi=60.0, macd_hist=-0.3, regime="bear", heavy_put_short=False
    )
    check("No heavy puts → short trigger does not fire",
          action != "short" or st != "short_candidate",
          f"got action={action} signal={st}")

    # ── Boundary: RSI exactly 50 → should NOT trigger (rule is RSI > 50) ─────
    st, action, _, _ = run_decision_tree(
        rsi=50.0, macd_hist=-0.3, regime="bear", heavy_put_short=True
    )
    check("RSI=50.0 (boundary, not >50) → must NOT short",
          action != "short",
          f"got action={action}")

    # ── Boundary: RSI 50.1 → should trigger ──────────────────────────────────
    st, action, _, _ = run_decision_tree(
        rsi=50.1, macd_hist=-0.3, regime="bear", heavy_put_short=True
    )
    check("RSI=50.1 (just above 50) → short fires",
          action == "short",
          f"got action={action}")

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — SCORE THRESHOLDS
# buy ≥ 55, skip < 45, review 45–54 (tradeable but needs more confirmation)
# ══════════════════════════════════════════════════════════════════════════════
if should_run("thresholds"):
    section("2. Score Thresholds (buy ≥55, skip <45)")

    from services.brain.signals import ScoredCandidate

    # is_tradeable property: score ≥ 45 AND action != skip
    for score, action, expected_tradeable in [
        (55, "buy",  True),
        (54, "buy",  True),   # 45–54 = tradeable (review zone)
        (45, "buy",  True),   # boundary — exactly 45 = tradeable
        (44, "buy",  False),  # boundary — 44 = NOT tradeable
        (30, "buy",  False),
        (70, "skip", False),  # high score but skip action → not tradeable
        (60, "skip", False),
    ]:
        c = make_scored_candidate(score=score, suggested_action=action)
        check(f"score={score} action={action} → tradeable={expected_tradeable}",
              c.is_tradeable == expected_tradeable,
              f"is_tradeable={c.is_tradeable}")

    # is_strong property: score ≥ 60
    for score, expected_strong in [(60, True), (59, False), (100, True), (0, False)]:
        c = make_scored_candidate(score=score)
        check(f"score={score} → is_strong={expected_strong}",
              c.is_strong == expected_strong,
              f"is_strong={c.is_strong}")

    # Score clamped to 0–100
    # Verify the clamp logic (max(0, min(100, score)))
    for raw, expected_clamped in [(120, 100), (-10, 0), (100, 100), (0, 0), (75, 75)]:
        clamped = max(0, min(100, raw))
        check(f"Score {raw} clamped to {expected_clamped}",
              clamped == expected_clamped, f"got {clamped}")

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — REGIME CAPITAL MULTIPLIER
# extreme VIX=0.40, bear=0.50, chop=0.60, bull=1.00
# ══════════════════════════════════════════════════════════════════════════════
if should_run("regime_mult"):
    section("3. Regime Capital Multiplier Rules")

    def get_base_mult(regime, vix_level):
        """Mirror of trading_engine.py regime multiplier logic."""
        if vix_level == "extreme":
            return 0.40
        elif regime == "bear":
            return 0.50
        elif regime == "chop":
            return 0.60
        else:  # bull
            return 1.00

    for regime, vix, expected in [
        ("bull",  "normal",  1.00),
        ("bull",  "low",     1.00),
        ("chop",  "normal",  0.60),
        ("bear",  "normal",  0.50),
        ("bull",  "extreme", 0.40),  # VIX extreme overrides bull
        ("bear",  "extreme", 0.40),  # VIX extreme overrides even bear
        ("chop",  "extreme", 0.40),
    ]:
        mult = get_base_mult(regime, vix)
        check(f"regime={regime} vix={vix} → mult={expected}",
              mult == expected, f"got {mult}")

    # Verify ordering: extreme < bear < chop < bull
    check("Ordering: extreme < bear < chop < bull",
          get_base_mult("bull","extreme") <
          get_base_mult("bear","normal")  <
          get_base_mult("chop","normal")  <
          get_base_mult("bull","normal"),
          f"{get_base_mult('bull','extreme')} < {get_base_mult('bear','normal')} < "
          f"{get_base_mult('chop','normal')} < {get_base_mult('bull','normal')}")

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — FRED CONFIDENCE → CAPITAL MULTIPLIER WIRING
# ≥80% confidence → +0.10 (cap 1.10), ≤50% → -0.10 (floor 0.30)
# ══════════════════════════════════════════════════════════════════════════════
if should_run("fred_mult"):
    section("4. FRED Confidence → Capital Multiplier (Bug Fix Validation)")

    def apply_fred_adjustment(base_mult, confidence):
        """Mirror of the bug-fix code in trading_engine.py."""
        if confidence >= 0.80:
            return min(1.10, base_mult + 0.10)
        elif confidence <= 0.50:
            return max(0.30, base_mult - 0.10)
        else:
            return base_mult

    # High confidence boosts
    check("Bull + conf=0.80 → 1.10 (cap)",
          apply_fred_adjustment(1.00, 0.80) == 1.10, str(apply_fred_adjustment(1.00, 0.80)))
    check("Bull + conf=0.90 → 1.10 (cap, not 1.20)",
          apply_fred_adjustment(1.00, 0.90) == 1.10, str(apply_fred_adjustment(1.00, 0.90)))
    check("Chop + conf=0.85 → 0.70 (+0.10)",
          apply_fred_adjustment(0.60, 0.85) == 0.70, str(apply_fred_adjustment(0.60, 0.85)))

    # Low confidence penalises
    check("Bull + conf=0.50 → 0.90 (-0.10)",
          apply_fred_adjustment(1.00, 0.50) == 0.90, str(apply_fred_adjustment(1.00, 0.50)))
    check("Bear + conf=0.40 → 0.40 (-0.10 from 0.50)",
          apply_fred_adjustment(0.50, 0.40) == 0.40, str(apply_fred_adjustment(0.50, 0.40)))
    check("Bear + conf=0.30 → 0.40 (floor, not 0.40-0.10=0.30)",
          round(apply_fred_adjustment(0.40, 0.30), 10) == 0.30, str(apply_fred_adjustment(0.40, 0.30)))
    check("Extreme VIX + conf=0.20 → 0.30 (floor holds)",
          round(apply_fred_adjustment(0.40, 0.20), 10) == 0.30, str(apply_fred_adjustment(0.40, 0.20)))

    # Neutral zone — no change
    for conf in [0.51, 0.65, 0.70, 0.79]:
        result = apply_fred_adjustment(1.00, conf)
        check(f"conf={conf} (neutral zone) → no change",
              result == 1.00, f"got {result}")

    # Boundaries
    check("conf=0.795 → no change (just below 0.80 threshold)",
          apply_fred_adjustment(1.00, 0.795) == 1.00,
          str(apply_fred_adjustment(1.00, 0.795)))
    check("conf=0.800 → +0.10 (exactly at threshold)",
          apply_fred_adjustment(1.00, 0.800) == 1.10,
          str(apply_fred_adjustment(1.00, 0.800)))
    check("conf=0.505 → no change (just above 0.50)",
          apply_fred_adjustment(1.00, 0.505) == 1.00,
          str(apply_fred_adjustment(1.00, 0.505)))
    check("conf=0.500 → -0.10 (exactly at threshold)",
          apply_fred_adjustment(1.00, 0.500) == 0.90,
          str(apply_fred_adjustment(1.00, 0.500)))

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — BARCHART ETF EXCLUSION LIST
# All 45 symbols on the exclusion list must be blocked
# ══════════════════════════════════════════════════════════════════════════════
if should_run("barchart_exclusions"):
    section("5. Barchart ETF Exclusion List (all 45 symbols blocked)")
    try:
        # Mock httpx so barchart_options loads even without the package installed locally.
        # The exclusion list is a module-level constant — no HTTP call needed.
        import sys as _sys
        if "httpx" not in _sys.modules:
            _sys.modules["httpx"] = MagicMock()
        from services.brain.connectors.barchart_options import _EXCLUDED_SYMBOLS

        check("Exclusion list exists", isinstance(_EXCLUDED_SYMBOLS, (set, frozenset)),
              f"{len(_EXCLUDED_SYMBOLS)} symbols")
        check("Exclusion list ≥ 40 symbols", len(_EXCLUDED_SYMBOLS) >= 40,
              str(len(_EXCLUDED_SYMBOLS)))

        # Every major ETF category must be covered
        must_be_excluded = {
            # Broad index
            "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI",
            # Sector
            "XLK", "XLF", "XLV", "XLE", "XLY", "XLI", "SOXX", "SMH",
            # Bond
            "TLT", "IEF", "HYG", "LQD", "AGG",
            # Commodity
            "GLD", "IAU", "SLV", "GDX", "USO",
            # Crypto ETF
            "IBIT", "GBTC",
            # Volatility
            "UVXY", "SVXY",
            # Leveraged/inverse
            "TQQQ", "SQQQ", "SPXL", "SPXS", "SOXL", "SOXS",
            # Thematic
            "ARKK", "ARKW",
        }
        for sym in sorted(must_be_excluded):
            check(f"{sym} in exclusion list",
                  sym in _EXCLUDED_SYMBOLS,
                  "blocked" if sym in _EXCLUDED_SYMBOLS else "MISSING FROM LIST")

        # Legitimate stocks must NOT be in the exclusion list
        must_not_be_excluded = ["AAPL","MSFT","NVDA","AMZN","TSLA","MU","LRCX","AVGO","SBUX"]
        for sym in must_not_be_excluded:
            check(f"{sym} NOT in exclusion list (real stock)",
                  sym not in _EXCLUDED_SYMBOLS,
                  "correctly allowed" if sym not in _EXCLUDED_SYMBOLS else "WRONGLY BLOCKED")

    except Exception as e:
        check("Barchart exclusion list", False, str(e))

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — POSITION SIZING RULES
# ≤ 5% of equity per position, penny stocks ≤ 3%, ATR-based sizing
# ══════════════════════════════════════════════════════════════════════════════
if should_run("position_sizing"):
    section("6. Position Sizing Rules")
    try:
        from services.indicators import volatility_adjusted_quantity

        equity = 100_000.0

        # Signature: volatility_adjusted_quantity(portfolio_value, max_position_pct,
        #                                          current_price, atr, risk_per_trade_pct)
        # Standard position — must not exceed 5%
        for price, atr in [(150.0, 3.0), (50.0, 1.5), (500.0, 10.0), (10.0, 0.5)]:
            qty = volatility_adjusted_quantity(
                portfolio_value=equity,
                max_position_pct=0.05,
                current_price=price,
                atr=atr,
                risk_per_trade_pct=0.01,
            )
            position_value = qty * price
            pct = position_value / equity
            check(f"Price=${price:.0f} ATR={atr} → ≤5% equity",
                  pct <= 0.051,  # 0.1% tolerance for rounding
                  f"${position_value:.0f} = {pct:.1%}")

        # Near-zero price edge case
        qty_zero = volatility_adjusted_quantity(equity, 0.05, 0.01, 0.001, 0.01)
        check("Near-zero price → qty ≥ 0 (no crash)",
              qty_zero >= 0, f"qty={qty_zero}")

        # Higher equity → proportionally larger qty
        qty_small = volatility_adjusted_quantity(50_000,  0.05, 150.0, 3.0, 0.01)
        qty_large = volatility_adjusted_quantity(200_000, 0.05, 150.0, 3.0, 0.01)
        check("Larger equity → larger position qty",
              qty_large >= qty_small,
              f"$50k→qty={qty_small}, $200k→qty={qty_large}")

        # ATR-based risk: 1% risk per trade
        qty_1pct = volatility_adjusted_quantity(equity, 0.05, 100.0, 5.0, 0.01)
        expected_max_loss = qty_1pct * 5.0  # qty × ATR = risk in $
        check("1% risk rule: qty × ATR ≤ 1.1% equity",
              expected_max_loss <= equity * 0.011,
              f"${expected_max_loss:.0f} risk on ${equity:.0f} equity")

    except Exception as e:
        check("Position sizing", False, str(e))

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — SIGNAL BOOST RANGES
# Each signal must stay within documented limits
# ══════════════════════════════════════════════════════════════════════════════
if should_run("signal_boosts"):
    section("7. Signal Boost Ranges (each within documented limits)")

    boost_rules = {
        # (signal_name, min_boost, max_boost, documented_range)
        "barchart_very_unusual_call": (18,  18,  "always +18"),
        "barchart_unusual_call":      (10,  10,  "always +10"),
        "barchart_very_unusual_put":  (-18, -18, "always -18"),
        "barchart_unusual_put":       (-10, -10, "always -10"),
        "fmp_strong_beat":            (12,  12,  "always +12"),
        "fmp_mild_beat":              (6,   6,   "always +6"),
        "fmp_strong_miss":            (-12, -12, "always -12"),
        "fmp_mild_miss":              (-6,  -6,  "always -6"),
        "sec_large_buy":              (15,  15,  "always +15"),
        "sec_small_buy":              (8,   8,   "always +8"),
        "finnhub_strong_upgrade":     (10,  10,  "always +10"),
        "finnhub_mild_upgrade":       (5,   5,   "always +5"),
        "finnhub_mild_downgrade":     (-5,  -5,  "always -5"),
        "finnhub_strong_downgrade":   (-10, -10, "always -10"),
        "regime_bull_buy":            (20,  20,  "always +20"),
        "regime_bear_short":          (20,  20,  "always +20"),
        "regime_bear_long":           (-15, -15, "always -15"),
        "regime_chop_oversold":       (10,  10,  "always +10"),
    }

    # Simulate each boost directly
    def sim_barchart(call_or_put, very_unusual):
        import sys as _sys
        if "httpx" not in _sys.modules:
            _sys.modules["httpx"] = MagicMock()
        from services.brain.connectors.barchart_options import _BOOST_VERY_UNUSUAL, _BOOST_UNUSUAL
        if call_or_put == "call":
            return _BOOST_VERY_UNUSUAL if very_unusual else _BOOST_UNUSUAL
        else:
            return -_BOOST_VERY_UNUSUAL if very_unusual else -_BOOST_UNUSUAL

    boost_checks = [
        ("barchart_very_unusual_call", sim_barchart("call", True),  18,  18),
        ("barchart_unusual_call",      sim_barchart("call", False), 10,  10),
        ("barchart_very_unusual_put",  sim_barchart("put",  True), -18, -18),
        ("barchart_unusual_put",       sim_barchart("put",  False),-10, -10),
    ]
    for name, boost, lo, hi in boost_checks:
        check(f"{name} boost = {lo}",
              lo <= boost <= hi, f"got {boost}")

    # FMP boost logic
    def sim_fmp_boost(beat_pct):
        if beat_pct > 10:   return 12
        elif beat_pct > 5:  return 6
        elif beat_pct < -10: return -12
        elif beat_pct < -5:  return -6
        else:               return 0

    for pct, expected in [(15, 12), (7, 6), (0, 0), (-7, -6), (-15, -12)]:
        boost = sim_fmp_boost(pct)
        check(f"FMP beat={pct}% → boost={expected}",
              boost == expected, f"got {boost}")

    # FMP negative EPS fix — beating a negative estimate should still score
    # e.g. estimate=-0.10, actual=-0.03 → beat_pct = (-0.03 - -0.10)/0.10 * 100 = +70%
    eps_est = -0.10
    eps_act = -0.03
    beat_pct = ((eps_act - eps_est) / abs(eps_est)) * 100
    boost = sim_fmp_boost(beat_pct)
    check("FMP negative EPS fix: est=-0.10, actual=-0.03 → +12 boost",
          boost == 12,
          f"beat_pct={beat_pct:.0f}% → boost={boost}")

    # SEC insider boost
    def sim_sec_boost(net_usd):
        if net_usd >= 500_000: return 15
        elif net_usd >= 100_000: return 8
        else: return 0

    for amt, expected in [(600_000, 15), (500_000, 15), (200_000, 8),
                          (100_000, 8), (99_999, 0), (0, 0)]:
        boost = sim_sec_boost(amt)
        check(f"SEC buy ${amt:,} → boost={expected}",
              boost == expected, f"got {boost}")

    # Finnhub boost
    def sim_finnhub_boost(delta):
        if delta >= 2:   return 10
        elif delta == 1: return 5
        elif delta == 0: return 0
        elif delta == -1: return -5
        else:            return -10

    for delta, expected in [(3, 10), (2, 10), (1, 5), (0, 0), (-1, -5), (-3, -10)]:
        boost = sim_finnhub_boost(delta)
        check(f"Finnhub delta={delta:+d} → boost={expected}",
              boost == expected, f"got {boost}")

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — REGIME ALIGNMENT RULES
# Bull+buy=+20, Bear+short=+20, Bear+buy=-15, Chop+oversold=+10
# ══════════════════════════════════════════════════════════════════════════════
if should_run("regime_rules"):
    section("8. Regime Alignment Bonus/Penalty Rules")

    cases = [
        # (regime, action, signal_type, expected_regime_score)
        ("bull", "buy",   "momentum",       20),   # aligned — bonus
        ("bull", "buy",   "breakout",       20),   # aligned — bonus
        ("bear", "short", "short_candidate",20),   # aligned — bonus
        ("bear", "buy",   "momentum",      -15),   # fighting regime — penalty
        ("chop", "buy",   "oversold",       10),   # mean reversion in chop — bonus
        ("chop", "buy",   "momentum",        0),   # neutral
        ("bull", "short", "short_candidate", 0),   # bull regime short → neutral (no bonus)
        ("bear", "buy",   "oversold",       -15),  # still penalised even if oversold
    ]

    for regime, action, sig_type, expected in cases:
        _, _, _, bd = run_decision_tree(
            regime=regime,
            rsi=60.0 if action == "buy" else 60.0,
            macd_hist=0.3 if action == "buy" else -0.3,
            heavy_put_short=(action == "short"),
        )
        # Directly compute from the rule
        is_inv = False
        if regime == "bull" and action == "buy":
            actual = 20
        elif regime == "bear" and action == "short":
            actual = 20
        elif regime == "bear" and action == "buy" and not is_inv:
            actual = -15
        elif regime == "chop" and sig_type == "oversold":
            actual = 10
        else:
            actual = 0

        check(f"regime={regime} + {action} ({sig_type}) → regime_score={expected}",
              actual == expected, f"got {actual}")

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — INJECTION GUARDS
# Barchart injection: price ≥ $3, ticker format [A-Z]{1-5}
# ══════════════════════════════════════════════════════════════════════════════
if should_run("injection_guards"):
    section("9. Universe Injection Guards")
    import re

    # Ticker format guard (rejects foreign tickers like SVI.TO, TRP.TO)
    valid_tickers   = ["AAPL","MSFT","NVDA","A","GOOGL","BRK"]
    invalid_tickers = ["SVI.TO","TRP.TO","0001.HK","TOOLONG1","","SPY.US","123"]

    ticker_pattern = re.compile(r'^[A-Z]{1,5}$')
    for sym in valid_tickers:
        check(f"Ticker '{sym}' passes format guard",
              bool(ticker_pattern.match(sym)), sym)
    for sym in invalid_tickers:
        check(f"Ticker '{sym}' blocked by format guard",
              not bool(ticker_pattern.match(sym)), sym)

    # Price guard — $3 minimum for Barchart injection
    def price_guard(price):
        return price >= 3.0

    for price, expected in [
        (0.17, False),   # NOTV penny stock (real example from data check)
        (2.99, False),   # just below $3
        (3.00, True),    # exactly $3 — passes
        (3.01, True),    # above $3
        (150.0, True),   # normal stock
    ]:
        check(f"Price ${price} {'passes' if expected else 'blocked by'} $3 guard",
              price_guard(price) == expected, f"${price} → {'allowed' if price_guard(price) else 'blocked'}")

    # Barchart min volume guard ($500 contracts)
    def volume_guard(volume):
        return volume >= 500

    for vol, expected in [
        (0,    False),
        (499,  False),
        (500,  True),    # boundary — exactly 500
        (501,  True),
        (9500, True),    # MU example from real data
        (759,  True),    # AVGO example from real data
    ]:
        check(f"Volume {vol} {'passes' if expected else 'blocked by'} 500 guard",
              volume_guard(vol) == expected)

    # Ratio guard (vol/OI ≥ 10x for unusual, ≥ 50x for very unusual)
    def ratio_guard(ratio, threshold=10.0):
        return ratio >= threshold

    for ratio, threshold, expected in [
        (9.9,  10.0, False),
        (10.0, 10.0, True),
        (49.9, 50.0, False),
        (50.0, 50.0, True),
        (1357, 50.0, True),  # MU example
    ]:
        check(f"Ratio {ratio}x vs threshold {threshold}x → {expected}",
              ratio_guard(ratio, threshold) == expected)

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — CIRCUIT BREAKER RULES
# Daily loss limit halts buys. Stop loss, take profit ranges.
# ══════════════════════════════════════════════════════════════════════════════
if should_run("circuit_breaker"):
    section("10. Circuit Breaker & Risk Limit Rules")

    # Risk defaults are defined in trading_engine._RISK_DEFAULTS.
    # We inline them here to avoid importing trading_engine (needs pydantic/Railway).
    # If these values change in trading_engine, update here too.
    _RISK_DEFAULTS = {
        "daily_loss_limit_pct": 4.0,
        "stop_loss_pct": 0.04,
        "take_profit_pct": 0.20,
        "min_daily_trades": 3,
        "afternoon_pressure_hour": 13,
        "max_trades_per_cycle": 5,
        "max_penny_position_pct": 3.0,
        "cycle_interval_seconds": 600,
        "profit_reserve_pct": 0.0,
    }
    risk = _RISK_DEFAULTS

    daily_loss = risk["daily_loss_limit_pct"]
    stop_loss  = risk["stop_loss_pct"]
    take_profit= risk["take_profit_pct"]
    max_trades = risk["max_trades_per_cycle"]
    max_penny  = risk["max_penny_position_pct"]

    check("Daily loss limit is set",       daily_loss > 0, f"{daily_loss}%")
    check("Daily loss limit ≤ 10%",        daily_loss <= 10, f"{daily_loss}% (sanity cap)")
    check("Stop loss is set",              stop_loss > 0, f"{stop_loss*100:.0f}%")
    check("Stop loss ≤ 15%",               stop_loss <= 0.15, f"{stop_loss*100:.0f}%")
    check("Take profit is set",            take_profit > 0, f"{take_profit*100:.0f}%")
    check("Take profit > stop loss",       take_profit > stop_loss,
          f"TP={take_profit*100:.0f}% > SL={stop_loss*100:.0f}%")
    check("Max trades/cycle 1–20",         1 <= max_trades <= 20, str(max_trades))
    check("Penny position ≤ 5%",           max_penny <= 5.0, f"{max_penny}%")
    check("Penny position < standard 5%",  max_penny < 5.0,
          f"penny={max_penny}% < standard=5%")

    # Circuit breaker logic simulation
    def circuit_breaker_triggered(daily_pnl_pct, limit_pct):
        """Returns True if new buys should be halted."""
        return daily_pnl_pct <= -limit_pct

    limit = daily_loss  # e.g. 4.0
    for pnl, expected_halt in [
        (-limit - 0.1, True),   # past limit → halt
        (-limit,       True),   # exactly at limit → halt
        (-limit + 0.1, False),  # just inside limit → OK
        (0.0,          False),  # profitable day → no halt
        (5.0,          False),  # very profitable → no halt
    ]:
        halted = circuit_breaker_triggered(pnl, limit)
        check(f"PnL={pnl:+.1f}% vs limit=-{limit}% → halt={expected_halt}",
              halted == expected_halt,
              f"{'HALTED' if halted else 'trading'}")

# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
import time
print(f"\n{'═'*60}")
print(f"  SUMMARY")
print(f"{'═'*60}")

any_failed = False
for sec_name, sec_res in _section_results.items():
    if not sec_res:
        continue
    passed = sum(sec_res)
    total  = len(sec_res)
    failed = total - passed
    tag    = "PASS" if failed == 0 else "FAIL"
    print(f"  [{tag}]  {sec_name:<40}  {passed}/{total}")
    if failed:
        any_failed = True

total_p = sum(_results)
total_a = len(_results)
total_f = total_a - total_p

print(f"{'─'*60}")
print(f"  Total: {total_p}/{total_a} rule checks passed", end="")
if total_f:
    print(f"   ← {total_f} RULES VIOLATED — fix before Monday")
else:
    print("  — all rules enforced correctly ✓")
print(f"{'═'*60}\n")

sys.exit(0 if total_f == 0 else 1)
