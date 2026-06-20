"""
Live trade analysis — runs against production DB via railway run.
Usage: railway run python backend/scripts/live_analysis.py
"""
import os, json, psycopg2, decimal
from datetime import datetime

DB_URL = os.environ.get("DATABASE_URL")
if not DB_URL:
    raise SystemExit("DATABASE_URL not set — run via: railway run python backend/scripts/live_analysis.py")

conn = psycopg2.connect(DB_URL)

def q(sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        cols = [d[0] for d in cur.description]
        rows = []
        for row in cur.fetchall():
            r = {}
            for k, v in zip(cols, row):
                if isinstance(v, decimal.Decimal):
                    r[k] = float(v)
                elif hasattr(v, "isoformat"):
                    r[k] = v.isoformat()
                else:
                    r[k] = v
            rows.append(r)
        return rows

print("\n" + "="*60)
print("KOVA LIVE ANALYSIS —", datetime.now().strftime("%Y-%m-%d %H:%M"))
print("="*60)

# 1. Overall summary
summary = q("""
    SELECT
        COUNT(*) AS total_trades,
        SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END) AS wins,
        SUM(CASE WHEN realized_pl <= 0 THEN 1 ELSE 0 END) AS losses,
        ROUND(AVG(CASE WHEN realized_pl > 0 THEN realized_pl END)::numeric, 2) AS avg_win,
        ROUND(AVG(CASE WHEN realized_pl <= 0 THEN realized_pl END)::numeric, 2) AS avg_loss,
        ROUND(AVG(realized_pl)::numeric, 2) AS avg_pl,
        ROUND(SUM(realized_pl)::numeric, 2) AS total_pl,
        ROUND(AVG(realized_pl_pct)::numeric, 3) AS avg_pl_pct,
        ROUND(AVG(hold_duration_mins)::numeric, 0) AS avg_hold_mins
    FROM position_log WHERE exit_price IS NOT NULL
""")
print("\n--- OVERALL SUMMARY ---")
for k, v in (summary[0] if summary else {}).items():
    print(f"  {k}: {v}")

# 2. By signal type / strategy
by_signal = q("""
    SELECT
        COALESCE(signal_type, 'unknown') AS signal_type,
        COUNT(*) AS trades,
        SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END) AS wins,
        ROUND(AVG(realized_pl)::numeric, 2) AS avg_pl,
        ROUND(SUM(realized_pl)::numeric, 2) AS total_pl,
        ROUND(AVG(realized_pl_pct)::numeric, 3) AS avg_pct
    FROM position_log WHERE exit_price IS NOT NULL
    GROUP BY signal_type ORDER BY total_pl DESC
""")
print("\n--- BY SIGNAL TYPE ---")
for r in by_signal:
    win_rate = f"{r['wins']}/{r['trades']}" if r['trades'] else "0/0"
    print(f"  {r['signal_type']:<30} trades={r['trades']}  wins={win_rate}  avg_pl=${r['avg_pl']}  total=${r['total_pl']}")

# 3. By regime
by_regime = q("""
    SELECT
        COALESCE(market_regime, 'unknown') AS regime,
        COUNT(*) AS trades,
        SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END) AS wins,
        ROUND(AVG(realized_pl)::numeric, 2) AS avg_pl,
        ROUND(SUM(realized_pl)::numeric, 2) AS total_pl
    FROM position_log WHERE exit_price IS NOT NULL
    GROUP BY market_regime ORDER BY count DESC
""")
print("\n--- BY MARKET REGIME ---")
for r in by_regime:
    print(f"  {r['regime']:<15} trades={r['trades']}  wins={r['wins']}  avg_pl=${r['avg_pl']}  total=${r['total_pl']}")

# 4. By exit reason
by_exit = q("""
    SELECT
        COALESCE(exit_reason, 'unknown') AS exit_reason,
        COUNT(*) AS count,
        ROUND(AVG(realized_pl)::numeric, 2) AS avg_pl,
        ROUND(SUM(realized_pl)::numeric, 2) AS total_pl,
        SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END) AS wins,
        ROUND(MIN(realized_pl)::numeric, 2) AS worst,
        ROUND(MAX(realized_pl)::numeric, 2) AS best
    FROM position_log WHERE exit_price IS NOT NULL
    GROUP BY exit_reason ORDER BY total_pl ASC
""")
print("\n--- BY EXIT REASON ---")
for r in by_exit:
    print(f"  {r['exit_reason']:<25} count={r['count']}  wins={r['wins']}  avg=${r['avg_pl']}  total=${r['total_pl']}  worst=${r['worst']}  best=${r['best']}")

# 5. By symbol (worst offenders)
by_symbol = q("""
    SELECT
        symbol,
        COUNT(*) AS trades,
        ROUND(SUM(realized_pl)::numeric, 2) AS total_pl,
        ROUND(AVG(realized_pl)::numeric, 2) AS avg_pl,
        SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END) AS wins,
        STRING_AGG(DISTINCT COALESCE(exit_reason,'?'), ', ') AS exits,
        STRING_AGG(DISTINCT COALESCE(signal_type,'?'), ', ') AS signals
    FROM position_log WHERE exit_price IS NOT NULL
    GROUP BY symbol ORDER BY total_pl ASC
""")
print("\n--- BY SYMBOL (worst first) ---")
for r in by_symbol:
    print(f"  {r['symbol']:<8} trades={r['trades']}  wins={r['wins']}  total=${r['total_pl']}  avg=${r['avg_pl']}  exits=[{r['exits']}]  signals=[{r['signals']}]")

# 6. TP/SL analysis — are targets reachable?
tp_sl = q("""
    SELECT
        ROUND(AVG(take_profit_pct)::numeric, 2) AS avg_tp_pct,
        ROUND(AVG(stop_loss_pct)::numeric, 2) AS avg_sl_pct,
        ROUND(AVG(take_profit_pct / NULLIF(stop_loss_pct,0))::numeric, 2) AS avg_rr,
        SUM(CASE WHEN take_profit_pct > 9 THEN 1 ELSE 0 END) AS tp_above_9pct,
        SUM(CASE WHEN take_profit_pct <= 9 THEN 1 ELSE 0 END) AS tp_at_or_below_9pct,
        COUNT(*) AS total_buys
    FROM trade_log
    WHERE action = 'buy' AND take_profit_pct IS NOT NULL AND stop_loss_pct IS NOT NULL
""")
print("\n--- TP/SL AVERAGES (all buy trades) ---")
for k, v in (tp_sl[0] if tp_sl else {}).items():
    print(f"  {k}: {v}")

# 7. Daily P&L trend
daily = q("""
    SELECT date::text, portfolio_value, day_pl, day_pl_pct, spy_close
    FROM daily_summary ORDER BY date DESC LIMIT 21
""")
print("\n--- DAILY P&L (last 21 days) ---")
for r in daily:
    bar = "+" if (r['day_pl'] or 0) >= 0 else "-"
    print(f"  {r['date']}  pf=${r['portfolio_value']}  day_pl=${r['day_pl']}  ({r['day_pl_pct']}%)  SPY={r['spy_close']}  {bar}")

# 8. Open positions right now
open_pos = q("""
    SELECT symbol, COALESCE(signal_type,'?') AS signal, entry_price,
           entry_time::text AS entered,
           ROUND(((NOW() - entry_time) / INTERVAL '1 minute')::numeric, 0) AS mins_held
    FROM position_log
    WHERE exit_price IS NULL
    ORDER BY entry_time ASC
""")
print("\n--- CURRENTLY OPEN POSITIONS ---")
if not open_pos:
    print("  (none)")
for r in open_pos:
    print(f"  {r['symbol']:<8} signal={r['signal']:<25} entry=${r['entry_price']}  held={r['mins_held']}m  since={r['entered']}")

# 9. Bot events last 72h (condensed)
events = q("""
    SELECT
        (timestamp AT TIME ZONE 'America/New_York')::text AS ts_et,
        event_type, symbol, message
    FROM bot_activity_log
    WHERE timestamp > NOW() - INTERVAL '72 hours'
      AND event_type IN ('entry_rejected','earnings_block','circuit_breaker',
                         'trade_cap','stop_loss','take_profit','error',
                         'momentum_decay','eod_flat')
    ORDER BY timestamp DESC LIMIT 50
""")
print("\n--- BOT EVENTS (last 72h) ---")
if not events:
    print("  (none)")
for r in events:
    print(f"  [{r['ts_et'][:16]}] {r['event_type']:<20} {r['symbol'] or '':<8} {(r['message'] or '')[:80]}")

# 10. Wheel status
wheel = q("""
    SELECT symbol, phase, put_strike, put_expiry, put_premium,
           call_strike, call_expiry, call_premium,
           total_premium_collected, status, notes,
           opened_at::date AS opened
    FROM wheel_positions ORDER BY opened_at DESC LIMIT 10
""")
print("\n--- WHEEL POSITIONS ---")
if not wheel:
    print("  (none)")
for r in wheel:
    print(f"  {r['symbol']:<6} phase={r['phase']:<10} status={r['status']:<12} put={r['put_strike']}@{r['put_expiry']}  premium_total=${r['total_premium_collected']}  opened={r['opened']}")

conn.close()
print("\n" + "="*60)
