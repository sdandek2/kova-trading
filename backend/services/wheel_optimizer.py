"""
wheel_optimizer.py — Self-improvement engine for the Wheel Bot

Runs every Friday after market close (4:30 PM ET).

What it does:
  1. Aggregates closed wheel_positions into wheel_symbol_stats
  2. Penalizes underperformers (win rate < 40%) — removes from universe
  3. Rewards top performers (win rate > 70%) — boosts score in universe
  4. Logs a weekly optimizer report (visible in iOS Wheel tab)
  5. Adjusts scanner confidence thresholds based on recent fill rates

Self-improvement is DATA-DRIVEN not AI-driven:
  Math is more reliable than LLM for "did SOFI make money last 10 cycles?"
  AI is only used for weekly summary narrative (optional).

Golden rule: optimizer NEVER blocks a trade. It adjusts universe scores
and logs suggestions. The scheduler still runs on schedule regardless.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# ── Stats aggregation ─────────────────────────────────────────────────────────

def _aggregate_symbol_stats():
    """
    Read all completed wheel_positions, calculate per-symbol stats,
    upsert into wheel_symbol_stats.
    """
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return

        with conn.cursor() as cur:
            # Get stats grouped by symbol from completed positions
            cur.execute("""
                SELECT
                    symbol,
                    COUNT(*) as total_cycles,
                    COUNT(*) FILTER (WHERE realized_pl > 0) as winning_cycles,
                    COALESCE(AVG(total_premium_collected / NULLIF(put_strike * 100, 0)), 0) as avg_premium_yield,
                    COALESCE(AVG(realized_pl), 0) as avg_realized_pl,
                    COALESCE(SUM(total_premium_collected), 0) as total_premium
                FROM wheel_positions
                WHERE status = 'completed'
                GROUP BY symbol
            """)
            rows = cur.fetchall()

        now = datetime.now(timezone.utc)
        with conn.cursor() as cur:
            for row in rows:
                symbol, total, winning, avg_yield, avg_pl, total_premium = row
                win_rate = winning / total if total > 0 else 0
                cur.execute("""
                    INSERT INTO wheel_symbol_stats
                        (symbol, total_cycles, winning_cycles, win_rate,
                         avg_premium_yield, avg_realized_pl, total_premium, last_updated)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (symbol) DO UPDATE SET
                        total_cycles = EXCLUDED.total_cycles,
                        winning_cycles = EXCLUDED.winning_cycles,
                        win_rate = EXCLUDED.win_rate,
                        avg_premium_yield = EXCLUDED.avg_premium_yield,
                        avg_realized_pl = EXCLUDED.avg_realized_pl,
                        total_premium = EXCLUDED.total_premium,
                        last_updated = EXCLUDED.last_updated
                """, (symbol, total, winning, win_rate, avg_yield, avg_pl,
                      total_premium, now))

        logger.info(f"Wheel optimizer: stats aggregated for {len(rows)} symbols")
        return rows

    except Exception as e:
        logger.error(f"Wheel optimizer aggregate_stats error: {e}")
        return []


def _adjust_universe_scores():
    """
    Adjust universe scores based on actual performance.
    Poor performers get score cut (may drop from next AI universe pick).
    Strong performers get score boosted.
    """
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return

        with conn.cursor() as cur:
            # Boost: win rate > 70% AND at least 3 cycles → increase score by 10
            cur.execute("""
                UPDATE wheel_universe u
                SET score = LEAST(score + 10, 100)
                FROM wheel_symbol_stats s
                WHERE u.symbol = s.symbol
                  AND s.win_rate > 0.70
                  AND s.total_cycles >= 3
                  AND u.active = TRUE
            """)
            boosted = cur.rowcount
            logger.info(f"Wheel optimizer: boosted {boosted} symbols (win rate > 70%)")

            # Penalize: win rate < 40% AND at least 3 cycles → decrease score by 15
            cur.execute("""
                UPDATE wheel_universe u
                SET score = GREATEST(score - 15, 0)
                FROM wheel_symbol_stats s
                WHERE u.symbol = s.symbol
                  AND s.win_rate < 0.40
                  AND s.total_cycles >= 3
                  AND u.active = TRUE
            """)
            penalized = cur.rowcount
            logger.info(f"Wheel optimizer: penalized {penalized} symbols (win rate < 40%)")

            # Deactivate: score dropped to 0 — remove from universe entirely
            cur.execute("""
                UPDATE wheel_universe
                SET active = FALSE
                WHERE score <= 0 AND active = TRUE
            """)
            removed = cur.rowcount
            if removed > 0:
                logger.info(f"Wheel optimizer: removed {removed} symbols (score hit 0)")

    except Exception as e:
        logger.error(f"Wheel optimizer adjust_scores error: {e}")


# ── Weekly report ─────────────────────────────────────────────────────────────

def _build_weekly_report(stats_rows) -> dict:
    """Build a structured weekly performance report."""
    if not stats_rows:
        return {"status": "no_data", "message": "No completed cycles yet"}

    total_cycles = sum(r[1] for r in stats_rows)
    total_winning = sum(r[2] for r in stats_rows)
    total_premium = sum(r[5] for r in stats_rows)
    overall_win_rate = (total_winning / total_cycles * 100) if total_cycles > 0 else 0

    top_performers = sorted(
        [r for r in stats_rows if r[1] >= 2],
        key=lambda x: x[3],  # avg_realized_pl
        reverse=True
    )[:3]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_completed_cycles": total_cycles,
        "overall_win_rate_pct": round(overall_win_rate, 1),
        "total_premium_collected": round(total_premium, 2),
        "symbols_tracked": len(stats_rows),
        "top_performers": [
            {
                "symbol": r[0],
                "cycles": r[1],
                "win_rate_pct": round(r[3] * 100, 1) if r[1] > 0 else 0,
                "avg_pl": round(r[4], 2),
            }
            for r in top_performers
        ],
    }


# ── Self-adjusting execution parameters ──────────────────────────────────────
#
# These three parameters live in the same wheel:adaptive_thresholds cache as
# the universe filters. wheel_engine.py reads them each cycle.
#
#   limit_price_bias   "bid" | "ask"   — fill-rate adaptive pricing
#   otm_depth          float 0.88-0.97 — strike range upper bound (delta proxy)
#   contracts_override int   1-3       — position sizing

_ADAPTIVE_CACHE_KEY = "wheel:adaptive_thresholds"

def _self_adjust_execution_params(report: dict) -> dict:
    """
    Called every Friday after aggregate/score pass.

    Fill-rate adaptive pricing:
      fill_rate < 70%  → use ask price as limit (aggressive, guarantees fill)
      fill_rate ≥ 70%  → use bid price as limit (patient, captures spread)

    Delta / OTM depth (strike range upper bound = strike / stock_price):
      win_rate < 50%   → 0.92 (further OTM — fewer assignments, protect capital)
      50–79%           → 0.95 (standard)
      win_rate ≥ 80%   → 0.97 (closer to ATM — more premium, doing well)

    Position sizing (contracts per trade):
      win_rate < 60%               → 1  (capital preservation mode)
      60–79%, or fill_rate unknown → 2  (standard)
      win_rate ≥ 80% AND fill ≥ 80 → 3  (scaling up after proven performance)
    """
    try:
        from services.db import cache_get, cache_set
        params = cache_get(_ADAPTIVE_CACHE_KEY)
        if not isinstance(params, dict):
            params = {}

        # Gather metrics
        win_rate_pct  = report.get("overall_win_rate_pct")   # float | None
        fill_rate_pct = None
        try:
            from services.db import _get_conn
            conn = _get_conn()
            if conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT COUNT(*) as total,
                               COUNT(*) FILTER (WHERE status != 'order_pending') as filled
                        FROM wheel_positions
                        WHERE opened_at > NOW() - INTERVAL '14 days'
                    """)
                    row = cur.fetchone()
                    t, f = (row[0] or 0), (row[1] or 0)
                    if t >= 3:
                        fill_rate_pct = round(f / t * 100, 1)
        except Exception as _e:
            logger.warning(f"Wheel optimizer fill_rate fetch: {_e}")

        adjustments = {}

        # ── 1. Fill-rate adaptive pricing ─────────────────────────────────────
        if fill_rate_pct is not None:
            new_bias = "ask" if fill_rate_pct < 70 else "bid"
            if params.get("limit_price_bias") != new_bias:
                logger.info(
                    f"Wheel self-adjust: limit_price_bias → {new_bias} "
                    f"(fill_rate={fill_rate_pct}%)"
                )
            params["limit_price_bias"] = new_bias
            adjustments["limit_price_bias"] = new_bias
        else:
            params.setdefault("limit_price_bias", "bid")

        # ── 2. OTM depth (strike selection range) ─────────────────────────────
        if win_rate_pct is not None:
            if win_rate_pct < 50:
                new_otm = 0.92   # further OTM — fewer assignments
            elif win_rate_pct >= 80:
                new_otm = 0.97   # closer to ATM — more premium
            else:
                new_otm = 0.95   # standard
            old_otm = params.get("otm_depth", 0.95)
            if old_otm != new_otm:
                logger.info(
                    f"Wheel self-adjust: otm_depth {old_otm} → {new_otm} "
                    f"(win_rate={win_rate_pct}%)"
                )
            params["otm_depth"] = new_otm
            adjustments["otm_depth"] = new_otm
        else:
            params.setdefault("otm_depth", 0.95)

        # ── 3. Position sizing ─────────────────────────────────────────────────
        if win_rate_pct is not None:
            if win_rate_pct < 60:
                new_contracts = 1
            elif win_rate_pct >= 80 and (fill_rate_pct or 0) >= 80:
                new_contracts = 3
            else:
                new_contracts = 2
            old_contracts = params.get("contracts_override", 2)
            if old_contracts != new_contracts:
                logger.info(
                    f"Wheel self-adjust: contracts_override {old_contracts} → {new_contracts} "
                    f"(win_rate={win_rate_pct}%, fill_rate={fill_rate_pct}%)"
                )
            params["contracts_override"] = new_contracts
            adjustments["contracts_override"] = new_contracts
        else:
            params.setdefault("contracts_override", 2)

        params["last_updated"] = datetime.now(timezone.utc).isoformat()
        cache_set(_ADAPTIVE_CACHE_KEY, params, 30 * 24 * 3600)

        if adjustments:
            logger.info(f"Wheel self-adjust complete: {adjustments}")
        else:
            logger.info("Wheel self-adjust: no parameter changes (defaults held)")

        return adjustments

    except Exception as e:
        logger.error(f"Wheel self_adjust_execution_params error: {e}")
        return {}


# ── Main optimizer run ────────────────────────────────────────────────────────

def run_optimizer() -> dict:
    """
    Full optimizer cycle. Called every Friday 4:30 PM ET.
    1. Aggregate stats
    2. Adjust universe scores
    3. Self-adjust execution params (fill rate, delta, position sizing)
    4. Return weekly report
    """
    logger.info("Wheel optimizer: starting weekly run...")

    # 1. Aggregate
    stats_rows = _aggregate_symbol_stats()

    # 2. Adjust universe scores (only if we have enough data)
    if stats_rows:
        _adjust_universe_scores()

    # 3. Build report
    report = _build_weekly_report(stats_rows)

    # 4. Self-adjust execution parameters based on this week's performance
    adjustments = _self_adjust_execution_params(report)
    if adjustments:
        report["self_adjustments"] = adjustments

    logger.info(
        f"Wheel optimizer complete: {report.get('total_completed_cycles', 0)} cycles, "
        f"{report.get('overall_win_rate_pct', 0)}% win rate, "
        f"${report.get('total_premium_collected', 0)} total premium"
    )
    return report


def get_optimizer_stats() -> dict:
    """Return current symbol stats for iOS display."""
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return {}
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, total_cycles, winning_cycles, win_rate,
                       avg_premium_yield, avg_realized_pl, total_premium, last_updated
                FROM wheel_symbol_stats
                ORDER BY win_rate DESC, total_cycles DESC
            """)
            cols = [d[0] for d in cur.description]
            rows = []
            for row in cur.fetchall():
                d = dict(zip(cols, row))
                if hasattr(d.get("last_updated"), "isoformat"):
                    d["last_updated"] = d["last_updated"].isoformat()
                d["win_rate_pct"] = round(float(d["win_rate"] or 0) * 100, 1)
                rows.append(d)
            return {"stats": rows, "count": len(rows)}
    except Exception as e:
        logger.error(f"Wheel optimizer get_stats error: {e}")
        return {}
