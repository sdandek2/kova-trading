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
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Phase 1 audition (2026-06) ────────────────────────────────────────────────
# Until momentum-long + oversold-bounce prove positive expectancy over 30+
# trades, other setup types are benched: each setup needs its own trade sample
# to validate, and spreading ~3 trades/day across 7 setup types starves the
# Kelly/ML learning loops. Re-enable one at a time via env once core edge is
# proven. Inverse ETFs in bear regime stay on (the defensive "long" play).
_ALLOW_SHORTS    = os.environ.get("KOVA_ALLOW_SHORTS", "0") == "1"
_ALLOW_LEVERAGED = os.environ.get("KOVA_ALLOW_LEVERAGED", "0") == "1"


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
    weights: dict = None,   # signal_weights from DB — {signal_name: current_weight}
) -> ScoredCandidate:
    weights = weights or {}
    prices = data.get("closing_prices", [])
    high_prices = data.get("high_prices", [])
    low_prices = data.get("low_prices", [])
    price = data.get("current_price") or (prices[-1] if prices else 0)
    rel_vol = data.get("relative_volume", 1.0)
    news_count = (sentiment or {}).get(symbol, 0)

    # ── Liquidity + penny stock gate ─────────────────────────────────────────
    # Skip if price < $5 (penny stocks: wide spreads, manipulation risk).
    # Skip if neither avg nor today's dollar volume clears the floor:
    #   avg_dollar_vol < $5M  AND  today_dollar_vol < $2M
    # A catalyst-day mover (today_dollar_vol >= $2M) always passes even if
    # its historical average is thin — that's the trade we want to catch.
    if price > 0:
        _avg_vol   = data.get("avg_volume", 0) or 0
        _today_vol = data.get("volume", 0) or 0
        _avg_dv    = _avg_vol   * price
        _today_dv  = _today_vol * price
        if price < 5.0:
            return ScoredCandidate(
                symbol=symbol, score=0, signal_type="skip",
                suggested_action="skip", price=price, rsi=None,
                macd_hist=None, rs_percentile=0, rel_volume=rel_vol,
                regime_aligned=False, score_breakdown={},
                notes=f"Penny stock (${price:.2f})",
            )
        if _avg_dv < 5_000_000 and _today_dv < 2_000_000:
            return ScoredCandidate(
                symbol=symbol, score=0, signal_type="skip",
                suggested_action="skip", price=price, rsi=None,
                macd_hist=None, rs_percentile=0, rel_volume=rel_vol,
                regime_aligned=False, score_breakdown={},
                notes=f"Low liquidity (avg ${_avg_dv/1e6:.1f}M, today ${_today_dv/1e6:.1f}M)",
            )

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
    # news_count is now a signed net score: +N bullish articles, -N bearish
    breakdown["news"] = max(-15, min(20, news_count * 5))
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
            # RSI > 75: distinguish exhaustion from momentum burst.
            # High volume confirms buyers are still in control — continuation likely.
            # Normal volume at RSI > 75 = stretched without fuel — reversal likely.
            if rel_vol >= 3.0:
                breakdown["rsi"] = 5    # momentum burst — high RSI + volume = continuation
            else:
                breakdown["rsi"] = -10  # overbought without conviction — risky long entry
    else:
        breakdown["rsi"] = 0
    score += breakdown["rsi"]

    # ── Momentum burst ────────────────────────────────────────────────────────
    # Stock up >5% today + volume 3x+ = strong directional move with conviction.
    # These are the stocks that become top gainers by end of day.
    # Separate from the plain volume signal — requires price confirmation too.
    _prev_close = prices[-2] if len(prices) >= 2 and prices[-2] else None
    _today_pct = ((price - _prev_close) / _prev_close * 100) if _prev_close and _prev_close > 0 else 0
    if _today_pct >= 10.0 and rel_vol >= 3.0:
        breakdown["momentum_burst"] = 20   # strong catalyst confirmed by volume
    elif _today_pct >= 5.0 and rel_vol >= 3.0:
        breakdown["momentum_burst"] = 12   # moderate burst with volume
    else:
        breakdown["momentum_burst"] = 0
    score += breakdown["momentum_burst"]

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
        # Session 6: Barchart unusual options flow (vol/OI ratio filter)
        # Falls back to Alpaca-based unusual_whales if Barchart fails
        from services.brain.connectors.barchart_options import get_options_flow as _bc_flow
        flow = _bc_flow(symbol)
        if flow.get("signal") not in ("unavailable", "neutral"):
            raw = flow.get("conviction_boost", 0)
            # Bullish call flow (+) contradicts a short setup (RSI>70 + MACD fading).
            # Don't boost a stock we're about to short on overbought signals —
            # the calls and the short thesis can't both be right.
            _short_setup = (rsi is not None and rsi > 70
                            and macd_hist is not None and macd_hist < 0.5)
            if raw > 0 and _short_setup:
                pts = 0   # conflicting signals — neutral, don't add
            elif raw > 0:
                db_key = "barchart_very_unusual" if raw >= 15 else "barchart_unusual"
                pts = weights.get(db_key, raw)
                breakdown[db_key] = pts  # specific key so weekly weight adjuster can track wins
            else:
                pts = raw  # negative (put flow) always applies
                breakdown["options_flow"] = pts
            score += pts

    except Exception:
        try:
            from services.brain.connectors.unusual_whales import get_options_flow
            flow = get_options_flow(symbol)
            if flow.get("signal") not in ("unavailable", "neutral"):
                raw = flow.get("conviction_boost", 0)
                _short_setup = (rsi is not None and rsi > 70
                                and macd_hist is not None and macd_hist < 0.5)
                if raw > 0 and _short_setup:
                    pts = 0
                elif raw > 0:
                    pts = weights.get("options_flow_fallback", raw)
                else:
                    pts = raw
                breakdown["options_flow"] = pts
                score += pts
        except Exception:
            pass

    # fmp.py (analyst price targets) disabled — FMP free tier only covers a subset
    # of symbols creating scoring bias. Re-enable once subscribed to FMP Starter.
    # try:
    #     from services.brain.connectors.fmp import get_estimate_revision
    #     rev = get_estimate_revision(symbol, current_price=price)
    #     if rev.get("signal") not in ("unavailable", "unchanged"):
    #         breakdown["earnings_rev"] = rev.get("conviction_boost", 0)
    #         score += breakdown["earnings_rev"]
    # except Exception:
    #     pass

    # Trend strength: MA50/MA200/52w-high from snapshot (Alpaca bars, all symbols, no quota)
    # Replaces quiver.py FMP connector which only covered FMP-whitelisted symbols.
    try:
        _ma50      = data.get("ma50")
        _ma200     = data.get("ma200")
        _year_high = data.get("year_high")
        if _ma50 and price > 0 and _year_high:
            _above_50   = price > _ma50
            _above_200  = _ma200 and price > _ma200
            _golden     = _ma200 and _ma50 > _ma200
            _near_high  = (price - _year_high) / _year_high >= -0.05
            if _near_high and _above_50 and _above_200:
                breakdown["darkpool"] = 8
            elif _above_50 and _golden:
                breakdown["darkpool"] = 4
            score += breakdown.get("darkpool", 0)
    except Exception:
        pass

    # ── Session 6: FMP Earnings Surprise (+12/-12) ────────────────────────────
    try:
        from services.brain.connectors.fmp_earnings import get_earnings_signal
        esurp = get_earnings_signal(symbol)
        if esurp.get("signal") not in ("unavailable", "neutral"):
            raw = esurp.get("conviction_boost", 0)
            if raw > 0:
                db_key = "earnings_surprise_strong" if raw >= 10 else "earnings_surprise_mild"
                pts = weights.get(db_key, raw)
                breakdown[db_key] = pts  # specific key so weekly weight adjuster can track wins
            else:
                pts = raw  # negative surprise keeps hardcoded penalty
                breakdown["earnings_surprise"] = pts
            score += pts
    except Exception:
        pass

    # ── Session 6: SEC Form 4 Insider Buys (+15 >$500K / +8 >$100K) ──────────
    try:
        from services.brain.connectors.sec_insider import get_insider_signal
        insider = get_insider_signal(symbol)
        if insider.get("signal") not in ("unavailable", "neutral"):
            raw = insider.get("conviction_boost", 0)
            if raw > 0:
                db_key = "insider_buy_large" if raw >= 12 else "insider_buy_small"
                pts = weights.get(db_key, raw)
                breakdown[db_key] = pts  # specific key so weekly weight adjuster can track wins
            else:
                pts = raw
                breakdown["insider_buy"] = pts
            score += pts
    except Exception:
        pass

    # ── Session 6: Finnhub Analyst Recommendation Trends (+10/-10) ───────────
    try:
        from services.brain.connectors.finnhub import get_recommendation_signal
        rec = get_recommendation_signal(symbol)
        if rec.get("signal") not in ("unavailable", "neutral"):
            raw = rec.get("conviction_boost", 0)
            if raw > 0:
                pts = weights.get("analyst_revision", raw)
            else:
                pts = raw  # negative revision keeps hardcoded penalty
            breakdown["analyst_revision"] = pts
            score += pts
    except Exception:
        pass

    # ── Earnings proximity scoring (yfinance, free) ───────────────────────────
    # +10 if earnings in 2-5 days AND suggested action is bullish (pre-earnings run)
    # -10 if earnings tomorrow AND action is uncertain (binary event risk)
    # Skip ETFs — they have no earnings calendar and yfinance throws 404 for them.
    _is_etf = symbol in _LEVERAGED_ETFS or symbol in _INVERSE_ETFS or symbol in _BROAD_ETFS
    try:
        if not _is_etf:
            import yfinance as _yf
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            _ticker = _yf.Ticker(symbol)
            _cal = _ticker.calendar
            if _cal is not None and not _cal.empty:
                _earnings_col = None
                for _col in ("Earnings Date", "Earnings Date (Start)", "Event"):
                    if _col in _cal.columns:
                        _earnings_col = _col
                        break
                if _earnings_col:
                    _ed = _cal[_earnings_col].iloc[0] if hasattr(_cal[_earnings_col], "iloc") else None
                    if _ed is not None:
                        import pandas as _pd
                        _ed_dt = _pd.Timestamp(_ed).to_pydatetime()
                        _days_until = (_ed_dt.date() - _dt.now(_tz.utc).date()).days
                        if 2 <= _days_until <= 5:
                            breakdown["earnings_proximity"] = 10
                        elif _days_until == 1:
                            breakdown["earnings_proximity"] = -10
                        else:
                            breakdown["earnings_proximity"] = 0
                        score += breakdown.get("earnings_proximity", 0)
    except Exception:
        pass

    # ── Determine signal type and action ─────────────────────────────────────
    regime = regime_result.regime if regime_result else "chop"
    is_leveraged = symbol in _LEVERAGED_ETFS
    is_inverse = symbol in _INVERSE_ETFS

    # Check for heavy institutional put flow — this overrides the normal action
    # determination if the stock is already showing weakness (not in a strong uptrend)
    _heavy_put_short = False
    try:
        from services.brain.connectors.barchart_options import get_short_candidates as _bc_sc
        _heavy_put_short = symbol.upper() in _bc_sc()
    except Exception:
        pass

    if is_inverse:
        signal_type = "inverse_etf"
        suggested_action = "buy" if regime == "bear" else "skip"
    elif is_leveraged:
        signal_type = "momentum"
        suggested_action = "buy" if (_ALLOW_LEVERAGED and regime_result
                                     and regime_result.allows_leveraged_etfs) else "skip"
    elif rsi is not None and rsi > 70 and macd_hist is not None and macd_hist <= 0:
        # MACD must be negative — don't classify as short while momentum is still positive.
        # 0.146 is not "fading" — it's still bullish. Only cross-confirmed reversals qualify.
        signal_type = "short_candidate"
        suggested_action = "short" if (_ALLOW_SHORTS and regime in ("bear", "chop")) else "skip"
    elif (_ALLOW_SHORTS and _heavy_put_short
          and rsi is not None and rsi > 50          # not already oversold
          and macd_hist is not None and macd_hist < 0  # momentum already turning down
          and regime in ("bear", "chop")):
        # Heavy institutional put flow (≥5,000 contracts, ≥50× ratio) on a stock where:
        #   - RSI > 50: not oversold yet, room left to fall
        #   - MACD < 0: momentum already turning negative (confirms direction)
        #   - Bear/chop regime only: never fight a bull regime with a short
        # MACD guard is critical — without it we'd short stocks with positive momentum
        # that happen to have put flow, which is the wrong call if the stock is rising.
        signal_type = "short_candidate"
        suggested_action = "short"
    elif rsi is not None and rsi < 35:
        signal_type = "oversold"
        suggested_action = "buy" if regime in ("bull", "chop") else "skip"
    elif macd_hist is not None and macd_hist > 0.05 and rel_vol >= 1.5:
        signal_type = "breakout"
        suggested_action = "buy" if regime != "bear" else "skip"
    elif macd_hist is not None and macd_hist > 0:
        signal_type = "momentum"
        suggested_action = "buy" if regime != "bear" else "skip"
    else:
        signal_type = "reversal"
        suggested_action = "skip"

    # ── Short-context signal corrections ─────────────────────────────────────
    # RSI, MACD, RS, and Volume were scored from a long perspective above.
    # For short candidates, flip each to short-appropriate values.
    if signal_type == "short_candidate":
        # RS correction: high relative strength = strong stock = terrible short target.
        # Market leaders rarely roll over cleanly. Weak RS = easier short.
        if rs_pct >= 80:
            short_rs = -20   # market leader — fights the short hard
        elif rs_pct >= 60:
            short_rs = -10   # above-average — some resistance to shorting
        elif rs_pct >= 40:
            short_rs = 5     # average RS — neutral short candidate
        else:
            short_rs = 20    # weak RS = stock already underperforming = prime short
        score += short_rs - breakdown["rs"]
        breakdown["rs"] = short_rs

        # Volume correction: 8× volume on a strong mover = BUYERS in control, not exhaustion.
        # Shorts on runaway volume moves get squeezed. Low volume = less conviction = cleaner fade.
        if rel_vol >= 3.0:
            short_vol = -15  # extreme volume momentum = fight the tape, avoid
        elif rel_vol >= 2.0:
            short_vol = -8
        elif rel_vol >= 1.5:
            short_vol = 0
        else:
            short_vol = 8    # low volume move = easier to reverse
        score += short_vol - breakdown["volume"]
        breakdown["volume"] = short_vol

        if rsi is not None:
            if rsi >= 75:
                short_rsi = 12    # deeply overbought = prime short entry
            elif rsi >= 65:
                short_rsi = 6     # stretched = decent setup
            elif rsi >= 50:
                short_rsi = 0
            else:
                short_rsi = -10   # oversold = bad short entry
            score += short_rsi - breakdown["rsi"]
            breakdown["rsi"] = short_rsi

        if macd_hist is not None:
            if macd_hist < -0.10:
                short_macd = 15   # momentum broken down = strong confirmation
            elif macd_hist < 0:
                short_macd = 8    # turning negative = early entry signal
            elif macd_hist < 0.5:
                short_macd = 0    # still positive but fading
            else:
                short_macd = -10  # strongly bullish MACD = fight the trend
            score += short_macd - breakdown["macd"]
            breakdown["macd"] = short_macd

        # Flip news sentiment: bearish news confirms the short, bullish contradicts it
        if breakdown["news"] != 0:
            short_news = -breakdown["news"]
            score += short_news - breakdown["news"]
            breakdown["news"] = short_news

    # ── Regime alignment bonus/penalty ───────────────────────────────────────
    regime_aligned = False
    if regime == "bull" and suggested_action == "buy":
        breakdown["regime"] = 20
        regime_aligned = True
    elif regime == "bear" and suggested_action == "short":
        breakdown["regime"] = 20
        regime_aligned = True
    elif regime == "bear" and is_inverse and suggested_action == "buy":
        breakdown["regime"] = 25  # inverse ETF is the ideal bear-regime instrument
        regime_aligned = True
    elif regime == "bear" and suggested_action == "buy" and not is_inverse:
        breakdown["regime"] = -15  # buying longs in bear market
    elif regime == "chop" and is_inverse:
        breakdown["regime"] = -15  # inverse ETFs decay in sideways markets
    elif regime == "chop" and signal_type == "oversold":
        breakdown["regime"] = 10   # oversold bounce — mean reversion works in chop
        regime_aligned = True
    elif regime == "chop" and signal_type == "short_candidate":
        breakdown["regime"] = 8    # overbought fade — mirror of oversold bounce
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
# Broad/sector ETFs that have no earnings calendar — skip yfinance calendar call for these
_BROAD_ETFS = {
    "SPY","QQQ","IWM","DIA","GLD","SLV","TLT","HYG","LQD","IEF",
    "XLF","XLK","XLE","XLV","XLI","XLY","XLB","XLP","XLU","XLRE",
    "SMH","XBI","XME","GDX","USO","IAU","IBIT","GBTC","VOO","VTI",
    "AGG","SOXX","ARKK","ARKW","ARKG","ARKF","ARKE","SCO","OIH",
    "EWY","FXI","EEM","EFA","VWO","UVXY","SVXY","SNDQ","IUXX",
}


def score_universe(
    universe_snapshot: dict,
    regime_result,
    rs_map: dict,
    sentiment: dict,
    news_headlines: list,
    top_n: int = 12,
    min_score: int = 60,
) -> list[ScoredCandidate]:
    """
    Score every symbol in universe_snapshot and return top_n candidates.
    Only returns candidates with score >= min_score.
    """
    # Load adaptive weights from DB once per scoring cycle.
    # Falls back gracefully to {} (connector defaults) if DB is unavailable.
    try:
        from services.db import get_signal_weights as _get_weights
        _weights = _get_weights()
    except Exception:
        _weights = {}

    candidates = []
    for symbol, data in universe_snapshot.items():
        rs_score = (rs_map or {}).get(symbol)
        try:
            c = _score_symbol(symbol, data, regime_result, rs_score, sentiment, news_headlines, weights=_weights)
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
        logger.info(f"Signal scoring: no candidates above min_score={min_score} — holding this cycle")

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
