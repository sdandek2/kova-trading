"""
diagnose.py — Temporary internal diagnostic endpoint.
Returns Kova + Wheel DB stats as JSON. Remove after use.
"""

import decimal
from fastapi import APIRouter
from services.db import _get_conn

router = APIRouter(prefix="/internal", tags=["diagnose"])


def _coerce(val):
    if isinstance(val, decimal.Decimal):
        return float(val)
    if hasattr(val, "isoformat"):  # date / datetime
        return val.isoformat()
    return val


def _q(sql, params=None):
    conn = _get_conn()
    if not conn:
        return []
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        cols = [d[0] for d in cur.description]
        return [
            {k: _coerce(v) for k, v in zip(cols, row)}
            for row in cur.fetchall()
        ]


@router.get("/diagnose")
def diagnose():
    # 1. Overall Kova R/R
    rr = _q("""
        SELECT
            COUNT(*) AS total_trades,
            SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN realized_pl <= 0 THEN 1 ELSE 0 END) AS losses,
            ROUND(AVG(CASE WHEN realized_pl > 0 THEN realized_pl END)::numeric, 2) AS avg_win,
            ROUND(AVG(CASE WHEN realized_pl <= 0 THEN realized_pl END)::numeric, 2) AS avg_loss,
            ROUND(AVG(realized_pl)::numeric, 2) AS avg_pl,
            ROUND(AVG(realized_pl_pct)::numeric, 3) AS avg_pl_pct,
            ROUND(AVG(hold_duration_mins)::numeric, 0) AS avg_hold_mins
        FROM position_log WHERE exit_price IS NOT NULL
    """)

    # 2. By exit reason
    by_exit = _q("""
        SELECT
            COALESCE(exit_reason, 'unknown') AS exit_reason,
            COUNT(*) AS count,
            ROUND(AVG(realized_pl)::numeric, 2) AS avg_pl,
            ROUND(AVG(realized_pl_pct)::numeric, 3) AS avg_pct,
            SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN realized_pl <= 0 THEN 1 ELSE 0 END) AS losses,
            ROUND(MIN(realized_pl)::numeric, 2) AS worst,
            ROUND(MAX(realized_pl)::numeric, 2) AS best
        FROM position_log WHERE exit_price IS NOT NULL
        GROUP BY exit_reason ORDER BY count DESC
    """)

    # 3. TP/SL values the AI set
    tp_sl = _q("""
        SELECT
            symbol, take_profit_pct, stop_loss_pct,
            CASE WHEN stop_loss_pct > 0 THEN ROUND((take_profit_pct / stop_loss_pct)::numeric, 2) END AS rr_ratio,
            confidence, market_regime,
            timestamp::date AS date
        FROM trade_log
        WHERE action = 'buy' AND take_profit_pct IS NOT NULL AND stop_loss_pct IS NOT NULL
        ORDER BY timestamp DESC LIMIT 30
    """)
    tp_sl_avg = _q("""
        SELECT
            ROUND(AVG(take_profit_pct)::numeric, 2) AS avg_tp,
            ROUND(AVG(stop_loss_pct)::numeric, 2) AS avg_sl,
            ROUND(AVG(take_profit_pct / NULLIF(stop_loss_pct, 0))::numeric, 2) AS avg_rr,
            SUM(CASE WHEN take_profit_pct / NULLIF(stop_loss_pct, 0) < 1.5 THEN 1 ELSE 0 END) AS sub15_count,
            COUNT(*) AS total
        FROM trade_log
        WHERE action = 'buy' AND take_profit_pct IS NOT NULL AND stop_loss_pct IS NOT NULL
    """)

    # 4. By market regime
    by_regime = _q("""
        SELECT
            COALESCE(market_regime, 'unknown') AS regime,
            COUNT(*) AS count,
            SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END) AS wins,
            ROUND(AVG(realized_pl)::numeric, 2) AS avg_pl,
            ROUND(SUM(realized_pl)::numeric, 2) AS total_pl
        FROM position_log WHERE exit_price IS NOT NULL
        GROUP BY market_regime ORDER BY count DESC
    """)

    # 5. By symbol
    by_symbol = _q("""
        SELECT
            symbol, COUNT(*) AS trades,
            ROUND(SUM(realized_pl)::numeric, 2) AS total_pl,
            ROUND(AVG(realized_pl)::numeric, 2) AS avg_pl,
            SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END) AS wins,
            STRING_AGG(DISTINCT exit_reason, ', ') AS exit_reasons
        FROM position_log WHERE exit_price IS NOT NULL
        GROUP BY symbol ORDER BY total_pl ASC
    """)

    # 6. Momentum decay trades specifically
    momentum_exits = _q("""
        SELECT symbol, realized_pl, realized_pl_pct, hold_duration_mins,
               entry_price, exit_price, exit_reason
        FROM position_log
        WHERE exit_reason ILIKE '%momentum%' OR exit_reason ILIKE '%macd%' OR exit_reason ILIKE '%decay%'
        ORDER BY exit_time DESC LIMIT 20
    """)

    # 7. Wheel active positions
    wheel_positions = _q("""
        SELECT symbol, phase, put_strike, put_expiry, put_premium,
               call_strike, call_expiry, call_premium,
               total_premium_collected, regime_at_open,
               opened_at::date AS opened, notes
        FROM wheel_positions WHERE status = 'active'
        ORDER BY opened_at ASC
    """)

    # 8. Wheel universe
    wheel_universe = _q("""
        SELECT symbol, score, iv_profile, price_range, ai_reason,
               last_refreshed::date AS refreshed
        FROM wheel_universe WHERE active = TRUE
        ORDER BY score DESC
    """)

    # 9. Recent key bot events (last 72h)
    bot_events = _q("""
        SELECT
            (timestamp AT TIME ZONE 'America/New_York')::text AS ts_et,
            event_type, symbol, message
        FROM bot_activity_log
        WHERE timestamp > NOW() - INTERVAL '72 hours'
          AND event_type IN ('entry_rejected','earnings_block','circuit_breaker',
                             'trade_cap','stale_exit','stop_loss','take_profit',
                             'error','momentum_decay')
        ORDER BY timestamp DESC LIMIT 40
    """)

    # 10. Daily summary (last 14 days)
    daily = _q("""
        SELECT date::text, portfolio_value, day_pl, day_pl_pct,
               buy_decisions, sell_decisions, spy_close
        FROM daily_summary
        ORDER BY date DESC LIMIT 14
    """)

    return {
        "kova": {
            "summary": rr[0] if rr else {},
            "by_exit_reason": by_exit,
            "tp_sl_per_trade": tp_sl,
            "tp_sl_averages": tp_sl_avg[0] if tp_sl_avg else {},
            "by_regime": by_regime,
            "by_symbol": by_symbol,
            "momentum_decay_exits": momentum_exits,
        },
        "wheel": {
            "active_positions": wheel_positions,
            "universe": wheel_universe,
        },
        "bot_events_72h": bot_events,
        "daily_summary_14d": daily,
    }
