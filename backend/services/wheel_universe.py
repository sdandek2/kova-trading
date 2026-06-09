"""
wheel_universe.py — Quantitative scoring + AI universe discovery

Flow every Sunday 8 PM ET:
  1. Pull ~90 curated candidates (price/volume filter)
  2. Batch-fetch 6-month daily bars → HV30 + max drawdown per symbol
  3. Load IV rank from wheel_iv_history (tracked live by engine)
  4. Score each: IV_rank(30%) + IV/HV_ratio(30%) + stability(20%) + liquidity(20%)
  5. Auto-adapt thresholds if too few candidates pass (never returns 0)
  6. Pass top 20 scored stocks to AI → final 8-12 picks with reasoning
  7. Fallback: top 8 by score if AI fails
  8. Emergency fallback: hardcoded safe list if everything fails

Self-adjusting:
  - Filter thresholds relax automatically if <MIN_CANDIDATES pass
  - Win rate > 85% → tighten IV/HV threshold (be pickier, more premium)
  - Win rate < 70% → widen drawdown threshold (more stable stocks)
  - Fill rate < 70% → noted in adaptive params for engine to use
  - All params stored in cache DB, updated weekly after refresh

Sprint planning:
  - get_wheel_sprint_report() returns last week's key metrics
  - Run every Sunday before refresh to see what's working

Zero-error design:
  - Every external call has try/except with meaningful default
  - Scoring failure → score=50 (neutral, still considered)
  - AI failure → use top 8 by quantitative score
  - DB failure → use hardcoded SAFE_FALLBACK_UNIVERSE
  - Everything fails → SAFE_FALLBACK_UNIVERSE always runs
"""

import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Price / volume config (Railway env-var overridable) ───────────────────────
MIN_PRICE  = float(os.environ.get("WHEEL_MIN_PRICE",  "5.0"))
MAX_PRICE  = float(os.environ.get("WHEEL_MAX_PRICE",  "120.0"))
MIN_VOLUME = int(os.environ.get("WHEEL_MIN_VOLUME",   "500000"))

# ── Adaptive threshold defaults (overridden by DB cache if available) ─────────
_DEFAULT_IV_HV_MIN     = 1.0   # options must be at least as expensive as realized vol
_DEFAULT_DRAWDOWN_MAX  = 35.0  # max 35% drawdown over 6 months
_DEFAULT_IV_RANK_MIN   = 20.0  # minimum IV rank (0-100)
_DEFAULT_SCORE_MIN     = 40.0  # minimum composite score
_MIN_CANDIDATES        = 6     # if fewer pass, relax thresholds

# ── Hardcoded safe fallback — used only if ALL else fails ─────────────────────
# These stocks: liquid options (OI>2000), stable price, survive bear markets
SAFE_FALLBACK_UNIVERSE = [
    {"symbol": "T",   "score": 70, "reason": "Telecom defensive, high IV/HV ratio, dividend floor limits downside", "iv_profile": "moderate-high"},
    {"symbol": "VZ",  "score": 68, "reason": "Utility-like telecom, consistent IV, very low assignment risk",       "iv_profile": "moderate"},
    {"symbol": "BAC", "score": 72, "reason": "Large-cap bank, highly liquid options, elevated IV in bear markets",  "iv_profile": "high"},
    {"symbol": "WFC", "score": 69, "reason": "Large-cap bank, liquid options chain, stable vs smaller banks",       "iv_profile": "moderate-high"},
    {"symbol": "F",   "score": 65, "reason": "High option volume, low price = low collateral, auto sector premium", "iv_profile": "moderate"},
    {"symbol": "WBA", "score": 62, "reason": "Defensive retail pharma, elevated IV from sector pressure",           "iv_profile": "high"},
    {"symbol": "CVS", "score": 64, "reason": "Healthcare/pharma retail, stable cash flows, liquid options",         "iv_profile": "moderate"},
    {"symbol": "JPM", "score": 71, "reason": "Largest US bank, deep options market, premium IV in bear regime",     "iv_profile": "high"},
]

# ── ETF exclusions ────────────────────────────────────────────────────────────
_WHEEL_EXCLUSIONS = {
    "SPY","QQQ","IWM","DIA","VOO","VTI","GLD","SLV","USO","TLT","IEF",
    "LQD","HYG","AGG","XLF","XLK","XLE","XLV","XLY","XLI","SMH","SOXX",
    "SOXL","SOXS","TQQQ","SQQQ","SPXL","SPXS","UVXY","SVXY","GBTC",
    "IBIT","ARKK","ARKW","GDX","GDXJ","IAU",
}

# ── Candidate pool — 90 stocks across all regimes ────────────────────────────
# Intentionally diverse: AI + scoring will narrow to best 8-12
_ALL_CANDIDATES = [
    # Defensive / regime-neutral (stable, liquid, survive bear)
    "T", "VZ", "KO", "WMT", "MCD", "PG", "JNJ", "PEP", "COST", "HD",
    # Large-cap financials (liquid options, elevated IV)
    "JPM", "BAC", "WFC", "C", "GS", "MS",
    # Healthcare / pharma (defensive, high IV)
    "CVS", "WBA", "TDOC", "PFE", "ABBV", "MRK",
    # Energy (high IV, cyclical premium)
    "XOM", "CVX", "OXY", "SLB",
    # Industrials (moderate IV, stable)
    "CAT", "GE", "MMM", "HON",
    # Auto (high IV, liquid)
    "F", "GM",
    # Telecom / media (defensive yield)
    "CMCSA", "DIS",
    # Tech (high IV — only large-cap stable ones)
    "AAPL", "MSFT", "GOOGL", "META", "AMZN",
    # Semis (high IV — only when regime allows)
    "INTC", "QCOM", "MU", "AMD", "NVDA", "AVGO",
    # Fintech / consumer (high IV)
    "PYPL", "SQ", "SOFI",
    # Growth (high IV — regime dependent)
    "NFLX", "UBER", "ABNB", "SNAP",
    # Retail
    "MELI", "BABA",
    # Commodities
    "FCX", "AA", "CLF",
]


# ── Adaptive threshold management ────────────────────────────────────────────

_ADAPTIVE_CACHE_KEY = "wheel:adaptive_thresholds"


def _load_adaptive_thresholds() -> dict:
    """Load self-adjusted thresholds from DB cache. Falls back to defaults."""
    try:
        from services.db import cache_get
        cached = cache_get(_ADAPTIVE_CACHE_KEY)
        if isinstance(cached, dict):
            return cached
    except Exception:
        pass
    return {
        "iv_hv_min":    _DEFAULT_IV_HV_MIN,
        "drawdown_max": _DEFAULT_DRAWDOWN_MAX,
        "iv_rank_min":  _DEFAULT_IV_RANK_MIN,
        "score_min":    _DEFAULT_SCORE_MIN,
        "last_updated": None,
        "win_rate":     None,
        "fill_rate":    None,
    }


def _save_adaptive_thresholds(params: dict):
    """Persist adjusted thresholds to DB cache (30-day TTL)."""
    try:
        from services.db import cache_set
        params["last_updated"] = datetime.now(timezone.utc).isoformat()
        cache_set(_ADAPTIVE_CACHE_KEY, params, 30 * 24 * 3600)
        logger.info(f"Wheel adaptive thresholds saved: {params}")
    except Exception as e:
        logger.warning(f"Wheel adaptive threshold save failed: {e}")


def _update_adaptive_thresholds():
    """
    Self-adjustment logic — called after each Sunday refresh.
    Reads last week's performance and tightens/relaxes thresholds accordingly.

    Rules:
      win_rate > 85%  → tighten IV/HV min by 0.1 (be pickier, more premium)
      win_rate < 70%  → widen drawdown max by 5% (pick more stable stocks)
      fill_rate < 70% → log warning (engine handles limit price adjustment)
      Everything stays within hard bounds to prevent runaway adjustment.
    """
    params = _load_adaptive_thresholds()

    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return params

        with conn.cursor() as cur:
            # Win rate: % of positions that expired worthless or closed at profit
            cur.execute("""
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE status = 'completed' AND realized_pl > 0) as wins
                FROM wheel_positions
                WHERE opened_at > NOW() - INTERVAL '30 days'
                  AND status = 'completed'
            """)
            row = cur.fetchone()
            total, wins = (row[0] or 0), (row[1] or 0)
            win_rate = (wins / total) if total >= 5 else None

            # Fill rate: % of order_pending that became active
            cur.execute("""
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE status != 'order_pending') as filled
                FROM wheel_positions
                WHERE opened_at > NOW() - INTERVAL '14 days'
            """)
            row2 = cur.fetchone()
            t2, filled = (row2[0] or 0), (row2[1] or 0)
            fill_rate = (filled / t2) if t2 >= 3 else None

        params["win_rate"]  = round(win_rate * 100, 1) if win_rate else None
        params["fill_rate"] = round(fill_rate * 100, 1) if fill_rate else None

        if win_rate is not None:
            if win_rate > 0.85:
                # Doing great — tighten to get better stocks (more premium)
                params["iv_hv_min"] = min(params["iv_hv_min"] + 0.1, 1.8)
                params["iv_rank_min"] = min(params["iv_rank_min"] + 2, 40)
                logger.info(f"Wheel adaptive: win_rate={win_rate:.0%} > 85% → tightening thresholds")
            elif win_rate < 0.70:
                # Too many assignments — pick more stable stocks
                params["drawdown_max"] = max(params["drawdown_max"] - 5, 20)
                params["iv_hv_min"]    = max(params["iv_hv_min"] - 0.1, 0.8)
                logger.info(f"Wheel adaptive: win_rate={win_rate:.0%} < 70% → relaxing for stability")

        if fill_rate is not None and fill_rate < 0.70:
            logger.warning(
                f"Wheel adaptive: fill_rate={fill_rate:.0%} < 70% — "
                f"orders not filling. Engine should increase limit price."
            )

    except Exception as e:
        logger.warning(f"Wheel adaptive threshold update error: {e}")

    _save_adaptive_thresholds(params)
    return params


# ── Price metrics (batch) ─────────────────────────────────────────────────────

def _get_price_metrics_batch(symbols: list[str], data_client) -> dict:
    """
    Batch-fetch 6-month daily bars for all symbols in ONE API call.
    Returns {symbol: {"hv30": float, "max_drawdown": float}} dict.
    Much faster than one call per symbol.
    """
    result = {}
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        end   = datetime.now(timezone.utc)
        start = end - timedelta(days=190)  # 6 months + buffer

        bars_resp = data_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        ))

        for sym in symbols:
            try:
                bars = bars_resp.get(sym, [])
                if not bars or len(bars) < 10:
                    result[sym] = {"hv30": 0.0, "max_drawdown": 100.0}
                    continue

                closes = [float(b.close) for b in bars]

                # HV30 — annualized 30-day realized volatility
                recent = closes[-31:]
                if len(recent) >= 5:
                    returns = [math.log(recent[i] / recent[i-1])
                               for i in range(1, len(recent)) if recent[i-1] > 0]
                    if returns:
                        mean = sum(returns) / len(returns)
                        variance = sum((r - mean) ** 2 for r in returns) / max(len(returns) - 1, 1)
                        hv30 = math.sqrt(variance * 252)
                    else:
                        hv30 = 0.0
                else:
                    hv30 = 0.0

                # Max drawdown over full 6-month window
                peak   = closes[0]
                max_dd = 0.0
                for c in closes:
                    if c > peak:
                        peak = c
                    dd = (peak - c) / peak if peak > 0 else 0
                    if dd > max_dd:
                        max_dd = dd

                result[sym] = {
                    "hv30":         round(hv30 * 100, 1),
                    "max_drawdown": round(max_dd * 100, 1),
                }
            except Exception:
                result[sym] = {"hv30": 0.0, "max_drawdown": 100.0}

    except Exception as e:
        logger.warning(f"Wheel price metrics batch error: {e}")
        for sym in symbols:
            result.setdefault(sym, {"hv30": 0.0, "max_drawdown": 100.0})

    return result


# ── IV rank loader ────────────────────────────────────────────────────────────

def _get_iv_ranks(symbols: list[str]) -> dict:
    """
    Load IV rank for each symbol from wheel_iv_history table.
    IV rank = current IV percentile vs last 30 daily readings.
    Returns {symbol: {"iv_rank": float, "iv_current": float}}.
    """
    result = {s: {"iv_rank": 50.0, "iv_current": 0.0} for s in symbols}
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return result
        with conn.cursor() as cur:
            for sym in symbols:
                try:
                    cur.execute("""
                        SELECT iv_value FROM wheel_iv_history
                        WHERE symbol = %s
                        ORDER BY recorded_at DESC
                        LIMIT 30
                    """, (sym,))
                    rows = [float(r[0]) for r in cur.fetchall()]
                    if rows:
                        current = rows[0]
                        iv_min, iv_max = min(rows), max(rows)
                        rank = ((current - iv_min) / (iv_max - iv_min) * 100
                                if iv_max > iv_min else 50.0)
                        result[sym] = {"iv_rank": round(rank, 1), "iv_current": round(current, 4)}
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Wheel IV rank load error: {e}")
    return result


# ── Composite scorer ──────────────────────────────────────────────────────────

def _score_candidates(candidates: list[dict], data_client) -> list[dict]:
    """
    Score all candidates quantitatively.

    Score = IV_rank(30%) + IV/HV_ratio(30%) + stability(20%) + liquidity(20%)

    IV/HV ratio is the key alpha signal:
      > 1.5x = options significantly overpriced vs actual moves → SELL premium
      < 1.0x = options cheap vs realized vol → avoid (stock moves more than priced)

    Returns candidates list with score + metrics added, sorted best first.
    """
    symbols = [c["symbol"] for c in candidates]

    # Batch fetch — one API call for all price metrics
    price_metrics = _get_price_metrics_batch(symbols, data_client)

    # IV ranks from DB
    iv_data = _get_iv_ranks(symbols)

    # Historical performance context
    perf = _get_symbol_performance_context()

    scored = []
    for c in candidates:
        sym = c["symbol"]
        pm  = price_metrics.get(sym, {"hv30": 0.0, "max_drawdown": 100.0})
        iv  = iv_data.get(sym, {"iv_rank": 50.0, "iv_current": 0.0})

        hv30         = pm["hv30"]         # realized vol % annualized
        max_drawdown = pm["max_drawdown"] # worst 6-month drop %
        iv_rank      = iv["iv_rank"]      # 0-100 percentile
        iv_current   = iv["iv_current"]   # raw IV (0-1 scale)

        # IV/HV ratio — core metric
        # iv_current is on 0-1 scale, hv30 is on % scale → normalize
        iv_pct = iv_current * 100  # convert to % for comparison
        iv_hv_ratio = (iv_pct / hv30) if hv30 > 5 else 1.0  # avoid div by zero on very stable stocks

        # Component scores (all 0-100)
        iv_rank_score   = min(iv_rank, 100)
        iv_hv_score     = min(iv_hv_ratio * 40, 100)    # 2.5x ratio = perfect 100
        stability_score = max(0, 100 - (max_drawdown * 2.5))  # 40% drawdown = 0
        liquidity_score = min(c.get("volume_m", 0) * 10, 100) # 10M vol = 100

        # Performance bonus: if we've traded this before and won
        perf_bonus = 0.0
        if sym in perf:
            wr = perf[sym].get("win_rate", 50)
            if wr > 80:
                perf_bonus = 5.0
            elif wr < 60:
                perf_bonus = -5.0

        total = (
            iv_rank_score   * 0.30 +
            iv_hv_score     * 0.30 +
            stability_score * 0.20 +
            liquidity_score * 0.20 +
            perf_bonus
        )

        scored.append({
            **c,
            "iv_rank":        round(iv_rank, 1),
            "iv_current_pct": round(iv_pct, 1),
            "hv30":           round(hv30, 1),
            "iv_hv_ratio":    round(iv_hv_ratio, 2),
            "max_drawdown":   round(max_drawdown, 1),
            "stability_score":round(stability_score, 1),
            "score":          round(min(total, 100), 1),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


# ── Candidate pool ────────────────────────────────────────────────────────────

def _get_candidate_pool(data_client, limit: int = 80) -> list[dict]:
    """
    Filter _ALL_CANDIDATES by price and volume. Returns list of valid candidates.
    Uses batch snapshots. Includes error recovery per batch.
    """
    try:
        from alpaca.data.requests import StockSnapshotRequest

        filtered = []
        batch_size = 20

        for i in range(0, len(_ALL_CANDIDATES), batch_size):
            batch = [s for s in _ALL_CANDIDATES[i:i+batch_size] if s not in _WHEEL_EXCLUSIONS]
            if not batch:
                continue
            try:
                snaps = data_client.get_stock_snapshot(
                    StockSnapshotRequest(symbol_or_symbols=batch)
                )
                for sym, snap in snaps.items():
                    try:
                        price = float(snap.latest_trade.price) if snap.latest_trade else 0
                        if not (MIN_PRICE <= price <= MAX_PRICE):
                            continue
                        vol = float(snap.daily_bar.volume) if snap.daily_bar else 0
                        if vol < MIN_VOLUME:
                            continue
                        filtered.append({
                            "symbol":   sym,
                            "price":    round(price, 2),
                            "volume_m": round(vol / 1_000_000, 1),
                        })
                    except Exception:
                        continue
            except Exception as e:
                logger.warning(f"Wheel candidate batch {i//batch_size}: {e}")
                continue

        filtered.sort(key=lambda x: x["volume_m"], reverse=True)
        logger.info(f"Wheel candidate pool: {len(filtered)} passed price/volume filter")
        return filtered[:limit]

    except Exception as e:
        logger.error(f"Wheel candidate pool error: {e}")
        return []


# ── Historical performance context ───────────────────────────────────────────

def _get_symbol_performance_context() -> dict:
    """Load past wheel win rates per symbol from DB."""
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return {}
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, win_rate, avg_premium_yield, total_cycles
                FROM wheel_symbol_stats
                WHERE total_cycles >= 2
                ORDER BY win_rate DESC
            """)
            return {
                row[0]: {"win_rate": float(row[1] or 0) * 100,
                         "avg_yield": float(row[2] or 0) * 100,
                         "cycles": row[3]}
                for row in cur.fetchall()
            }
    except Exception:
        return {}


# ── DB persistence ────────────────────────────────────────────────────────────

def _save_universe(stocks: list[dict], source: str = "iv_hv_scored"):
    """Persist universe to wheel_universe table."""
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            logger.warning("Wheel universe: no DB connection — skipping save")
            return
        now = datetime.now(timezone.utc)
        with conn.cursor() as cur:
            cur.execute("UPDATE wheel_universe SET active = FALSE")
            for stock in stocks:
                reason = stock.get("reason") or stock.get("ai_reason", "")
                cur.execute("""
                    INSERT INTO wheel_universe
                        (symbol, score, ai_reason, iv_profile, price_range,
                         added_by, active, added_at, last_refreshed)
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s, %s)
                    ON CONFLICT (symbol) DO UPDATE SET
                        score = EXCLUDED.score,
                        ai_reason = EXCLUDED.ai_reason,
                        iv_profile = EXCLUDED.iv_profile,
                        active = TRUE,
                        last_refreshed = EXCLUDED.last_refreshed
                """, (
                    stock["symbol"],
                    int(stock.get("score", 50)),
                    reason,
                    stock.get("iv_profile", f"IV/HV={stock.get('iv_hv_ratio','?')}x"),
                    stock.get("price_range", f"${stock.get('price','?')}"),
                    source,
                    now, now,
                ))
        conn.commit()
        logger.info(f"Wheel universe: saved {len(stocks)} stocks (source={source})")
    except Exception as e:
        logger.error(f"Wheel universe save error: {e}")


def get_active_universe() -> list[str]:
    """Return active universe symbols for wheel_engine."""
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return [s["symbol"] for s in SAFE_FALLBACK_UNIVERSE]
        with conn.cursor() as cur:
            cur.execute("SELECT symbol FROM wheel_universe WHERE active = TRUE ORDER BY score DESC")
            result = [row[0] for row in cur.fetchall()]
        return result if result else [s["symbol"] for s in SAFE_FALLBACK_UNIVERSE]
    except Exception as e:
        logger.error(f"Wheel get_active_universe error: {e}")
        return [s["symbol"] for s in SAFE_FALLBACK_UNIVERSE]


def get_universe_details() -> list[dict]:
    """Return full universe with scores and reasons (for iOS display)."""
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return []
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, score, ai_reason, iv_profile, price_range,
                       added_by, last_refreshed
                FROM wheel_universe
                WHERE active = TRUE
                ORDER BY score DESC
            """)
            rows = []
            for row in cur.fetchall():
                cols = [d[0] for d in cur.description]
                d = dict(zip(cols, row))
                d["score"]    = int(d.get("score") or 0)
                d["reason"]   = d.pop("ai_reason", None)
                d["is_active"] = True
                lr = d.pop("last_refreshed", None)
                d["added_at"] = lr.isoformat() if hasattr(lr, "isoformat") else lr
                rows.append(d)
            return rows
    except Exception as e:
        logger.error(f"Wheel get_universe_details error: {e}")
        return []


# ── AI ranking step ───────────────────────────────────────────────────────────

def _ai_rank_universe(top_candidates: list[dict], regime: str) -> list[dict]:
    """
    Pass pre-scored top candidates to AI for final selection.
    AI gets full quantitative data — no blind guessing.
    Returns list of picks with ai_reason added.
    Falls back to top 8 by score if AI fails.
    """
    candidate_text = "\n".join([
        f"{c['symbol']}: score={c['score']:.0f} | "
        f"IV_rank={c.get('iv_rank', 0):.0f}% | "
        f"IV/HV={c.get('iv_hv_ratio', 0):.2f}x | "
        f"drawdown={c.get('max_drawdown', 0):.0f}% | "
        f"HV30={c.get('hv30', 0):.0f}% | "
        f"price=${c.get('price', 0):.2f} | "
        f"vol={c.get('volume_m', 0):.1f}M"
        for c in top_candidates
    ])

    prompt = f"""You are finalizing a wheel options strategy universe.

Current market regime: {regime.upper()}
Strategy: Sell 45-DTE cash-secured puts at ~0.25 delta. Collect premium via theta decay.

Pre-scored candidates (quantitative filters already applied):
{candidate_text}

Key metrics explained:
- IV/HV ratio: options price vs actual moves. >1.5x = options overpriced = IDEAL for selling premium
- IV rank: 0-100 percentile. Higher = more expensive options = more income
- Drawdown: worst 6-month drop. Lower = safer for assignment
- HV30: how much stock actually moves. Lower = less assignment risk

Select 8-10 stocks. Rules:
- Max 2 per sector (telecom, banks, tech, healthcare, auto, etc.)
- In BEAR regime: drawdown < 25% preferred, weight stability
- In BULL regime: IV/HV ratio and premium weight more
- No more than 2 stocks with drawdown > 25%
- Prefer stocks where IV/HV > 1.3x (options genuinely overpriced)

Return ONLY valid JSON array:
[{{"symbol":"T","score":85,"reason":"Telecom defensive, IV/HV 1.8x means options 80% overpriced vs realized vol, drawdown only 12% makes assignment safe","iv_profile":"moderate-high"}}]"""

    try:
        from services.ai_client import ask_ai, parse_ai_json
        raw    = ask_ai(prompt, max_tokens=1000)
        picks  = parse_ai_json(raw)

        if not isinstance(picks, list) or len(picks) < 3:
            raise ValueError(f"AI returned invalid picks: {type(picks)}")

        # Merge AI picks with quantitative data
        score_map = {c["symbol"]: c for c in top_candidates}
        result = []
        for pick in picks:
            sym = str(pick.get("symbol", "")).upper()
            if sym in score_map:
                merged = {
                    **score_map[sym],
                    "reason":   pick.get("reason", "AI selected"),
                    "ai_score": pick.get("score", score_map[sym]["score"]),
                    # Use max of AI score and quant score — both signal quality
                    "score": max(pick.get("score", 0), score_map[sym]["score"]),
                }
                result.append(merged)

        if len(result) >= 5:
            logger.info(f"Wheel universe: AI selected {len(result)} stocks from {len(top_candidates)} candidates")
            return result

        raise ValueError(f"Too few valid AI picks: {len(result)}")

    except Exception as e:
        logger.warning(f"Wheel universe AI ranking failed ({e}) — using top scored candidates")
        # Fallback: add generic reason to top candidates
        fallback = []
        for c in top_candidates[:8]:
            c["reason"] = (
                f"Quant score {c['score']:.0f}: "
                f"IV/HV {c.get('iv_hv_ratio',0):.1f}x, "
                f"IV rank {c.get('iv_rank',0):.0f}%, "
                f"drawdown {c.get('max_drawdown',0):.0f}%"
            )
            fallback.append(c)
        return fallback


# ── Main entry point ──────────────────────────────────────────────────────────

def refresh_universe() -> list[dict]:
    """
    Full universe refresh. Called every Sunday 8 PM ET by scheduler.

    Self-adjusting: reads adaptive thresholds from DB, updates them
    based on last week's win rate and fill rate before selecting new universe.

    Returns new universe list. NEVER returns empty — falls back to
    SAFE_FALLBACK_UNIVERSE if all else fails.
    """
    logger.info("Wheel universe: starting quantitative + AI discovery...")

    # Step 1: Update adaptive thresholds based on last week's performance
    params = _update_adaptive_thresholds()
    iv_hv_min    = params.get("iv_hv_min",    _DEFAULT_IV_HV_MIN)
    drawdown_max = params.get("drawdown_max",  _DEFAULT_DRAWDOWN_MAX)
    iv_rank_min  = params.get("iv_rank_min",   _DEFAULT_IV_RANK_MIN)
    score_min    = params.get("score_min",     _DEFAULT_SCORE_MIN)

    logger.info(
        f"Wheel universe: thresholds — "
        f"IV/HV≥{iv_hv_min}, drawdown≤{drawdown_max}%, "
        f"IV_rank≥{iv_rank_min}, score≥{score_min} "
        f"(win_rate={params.get('win_rate','?')}%, fill_rate={params.get('fill_rate','?')}%)"
    )

    # Step 2: Get regime
    try:
        from services.wheel_engine import _get_current_regime
        regime = _get_current_regime()
    except Exception:
        regime = "neutral"

    # Step 3: Get candidate pool
    try:
        from services.wheel_engine import _get_wheel_data_client
        data_client = _get_wheel_data_client()
    except Exception as e:
        logger.error(f"Wheel universe: cannot get data client ({e}) — using safe fallback")
        _save_universe(SAFE_FALLBACK_UNIVERSE, source="emergency_fallback")
        return SAFE_FALLBACK_UNIVERSE

    candidates = _get_candidate_pool(data_client, limit=80)
    if not candidates:
        logger.error("Wheel universe: empty candidate pool — using safe fallback")
        _save_universe(SAFE_FALLBACK_UNIVERSE, source="emergency_fallback")
        return SAFE_FALLBACK_UNIVERSE

    # Step 4: Score all candidates
    logger.info(f"Wheel universe: scoring {len(candidates)} candidates...")
    scored = _score_candidates(candidates, data_client)

    # Step 5: Apply adaptive filters
    qualified = [
        s for s in scored
        if s.get("score", 0)        >= score_min
        and s.get("iv_hv_ratio", 0) >= iv_hv_min
        and s.get("max_drawdown", 100) <= drawdown_max
        and s.get("iv_rank", 0)     >= iv_rank_min
    ]

    logger.info(
        f"Wheel universe: {len(qualified)}/{len(scored)} passed adaptive filters "
        f"(IV/HV≥{iv_hv_min}, drawdown≤{drawdown_max}%, IV_rank≥{iv_rank_min})"
    )

    # Step 6: Auto-relax if too few candidates (self-adjusting)
    relax_iterations = 0
    while len(qualified) < _MIN_CANDIDATES and relax_iterations < 4:
        iv_hv_min    = max(iv_hv_min    - 0.15, 0.5)
        drawdown_max = min(drawdown_max + 5,    50.0)
        iv_rank_min  = max(iv_rank_min  - 5,     0.0)
        score_min    = max(score_min    - 5,    20.0)
        relax_iterations += 1

        qualified = [
            s for s in scored
            if s.get("score", 0)           >= score_min
            and s.get("iv_hv_ratio", 0)    >= iv_hv_min
            and s.get("max_drawdown", 100) <= drawdown_max
            and s.get("iv_rank", 0)        >= iv_rank_min
        ]
        logger.info(
            f"Wheel universe: relaxed thresholds (iter {relax_iterations}) → "
            f"{len(qualified)} candidates | IV/HV≥{iv_hv_min:.2f}, drawdown≤{drawdown_max}"
        )

    # Step 7: Ultimate fallback — just use top scored regardless of thresholds
    if len(qualified) < _MIN_CANDIDATES:
        logger.warning(
            f"Wheel universe: only {len(qualified)} after relaxing — "
            f"using top {_MIN_CANDIDATES} by score"
        )
        qualified = scored[:_MIN_CANDIDATES]

    # Step 8: Pass top 20 to AI for final selection
    top20 = qualified[:20]
    universe = _ai_rank_universe(top20, regime)

    if not universe:
        logger.error("Wheel universe: AI + fallback both failed — using SAFE_FALLBACK_UNIVERSE")
        universe = SAFE_FALLBACK_UNIVERSE

    # Step 9: Save and return
    _save_universe(universe, source="iv_hv_scored")
    logger.info(
        f"Wheel universe refresh complete: {len(universe)} stocks | "
        f"top: {', '.join(s['symbol'] for s in universe[:5])}"
    )
    return universe


# ── Sprint planning report ────────────────────────────────────────────────────

def get_wheel_sprint_report() -> dict:
    """
    Weekly performance report for Sunday sprint review.
    Returns all key metrics needed to evaluate last week and plan next week.
    Run this before refresh_universe() to see what's working.
    """
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return {"error": "DB unavailable"}

        report = {}

        with conn.cursor() as cur:
            # 1. Overall summary
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE status = 'active')        as active,
                    COUNT(*) FILTER (WHERE status = 'order_pending') as pending,
                    COUNT(*) FILTER (WHERE status = 'completed')     as completed,
                    COALESCE(SUM(total_premium_collected) FILTER (WHERE status = 'active'), 0)    as active_premium,
                    COALESCE(SUM(total_premium_collected) FILTER (WHERE status = 'completed'), 0) as collected_premium,
                    COALESCE(SUM(realized_pl) FILTER (WHERE status = 'completed'), 0)             as realized_pl
                FROM wheel_positions
            """)
            cols = [d[0] for d in cur.description]
            row  = cur.fetchone()
            report["summary"] = dict(zip(cols, row))

            # 2. Win rate (last 30 days)
            cur.execute("""
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE realized_pl > 0) as wins,
                    AVG(realized_pl) as avg_pl
                FROM wheel_positions
                WHERE status = 'completed'
                  AND opened_at > NOW() - INTERVAL '30 days'
            """)
            row2 = cur.fetchone()
            total, wins, avg_pl = row2
            report["win_rate_30d"] = {
                "total": total,
                "wins":  wins,
                "win_rate_pct": round(wins / total * 100, 1) if total else None,
                "avg_pl": round(float(avg_pl or 0), 2),
            }

            # 3. Fill rate (last 14 days)
            cur.execute("""
                SELECT
                    COUNT(*) as placed,
                    COUNT(*) FILTER (WHERE status != 'order_pending') as filled,
                    COUNT(*) FILTER (WHERE status = 'order_pending') as still_pending
                FROM wheel_positions
                WHERE opened_at > NOW() - INTERVAL '14 days'
            """)
            row3 = cur.fetchone()
            placed, filled, still_pending = row3
            report["fill_rate_14d"] = {
                "placed":        placed,
                "filled":        filled,
                "still_pending": still_pending,
                "fill_rate_pct": round(filled / placed * 100, 1) if placed else None,
            }

            # 4. Per-symbol performance
            cur.execute("""
                SELECT symbol,
                       COUNT(*) as cycles,
                       COUNT(*) FILTER (WHERE realized_pl > 0) as wins,
                       COALESCE(SUM(total_premium_collected), 0) as total_premium,
                       COALESCE(SUM(realized_pl), 0) as total_pl
                FROM wheel_positions
                WHERE status = 'completed'
                GROUP BY symbol
                ORDER BY total_premium DESC
            """)
            rows = cur.fetchall()
            report["by_symbol"] = [
                {
                    "symbol":        r[0],
                    "cycles":        r[1],
                    "win_rate":      round(r[2] / r[1] * 100, 1) if r[1] else 0,
                    "total_premium": round(float(r[3]), 2),
                    "total_pl":      round(float(r[4]), 2),
                }
                for r in rows
            ]

            # 5. Current adaptive thresholds
            report["adaptive_thresholds"] = _load_adaptive_thresholds()

        return report

    except Exception as e:
        logger.error(f"Wheel sprint report error: {e}")
        return {"error": str(e)}
