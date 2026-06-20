"""
Pure AI trade analysis — run via: railway run python3 backend/scripts/pureai_analysis.py
"""
import os, psycopg2, decimal
from datetime import datetime

DB_URL = os.environ.get("DATABASE_URL")
if not DB_URL:
    raise SystemExit("DATABASE_URL not set — run via: railway run python3 backend/scripts/pureai_analysis.py")

conn = psycopg2.connect(DB_URL)

def q(sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        cols = [d[0] for d in cur.description]
        rows = []
        for row in cur.fetchall():
            r = {}
            for k, v in zip(cols, row):
                r[k] = float(v) if isinstance(v, decimal.Decimal) else (v.isoformat() if hasattr(v, "isoformat") else v)
            rows.append(r)
        return rows

print("\n" + "="*60)
print("PURE AI ANALYSIS —", datetime.now().strftime("%Y-%m-%d %H:%M"))
print("="*60)

# Summary
summary = q("""
    SELECT COUNT(*) AS total,
           SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END) AS wins,
           SUM(CASE WHEN realized_pl <= 0 THEN 1 ELSE 0 END) AS losses,
           ROUND(AVG(CASE WHEN realized_pl > 0 THEN realized_pl END)::numeric,2) AS avg_win,
           ROUND(AVG(CASE WHEN realized_pl <= 0 THEN realized_pl END)::numeric,2) AS avg_loss,
           ROUND(SUM(realized_pl)::numeric,2) AS total_pl,
           ROUND(AVG(realized_pl_pct)::numeric,2) AS avg_pl_pct
    FROM pureai_positions WHERE exit_time IS NOT NULL
""")
s = summary[0] if summary else {}
print(f"\n--- SUMMARY ---")
print(f"  Closed trades : {s.get('total',0)}")
print(f"  Win rate      : {s.get('wins',0)}/{s.get('total',0)}")
print(f"  Avg win       : ${s.get('avg_win') or 0:.2f}")
print(f"  Avg loss      : ${s.get('avg_loss') or 0:.2f}")
print(f"  Total P&L     : ${s.get('total_pl') or 0:.2f}")
print(f"  Avg P&L %     : {s.get('avg_pl_pct') or 0:.2f}%")

# All closed positions with thesis
closed = q("""
    SELECT symbol,
           entry_time AT TIME ZONE 'America/New_York' AS entry_et,
           exit_time  AT TIME ZONE 'America/New_York' AS exit_et,
           entry_price, exit_price, quantity,
           realized_pl, realized_pl_pct, open_thesis, close_reason
    FROM pureai_positions
    WHERE exit_time IS NOT NULL
    ORDER BY exit_time ASC
""")
print(f"\n--- CLOSED TRADES ---")
for r in closed:
    won = "WIN " if (r['realized_pl'] or 0) > 0 else "LOSS"
    print(f"  [{won}] {r['symbol']}")
    print(f"    entry=${r['entry_price']:.2f}  exit=${r['exit_price']:.2f}  qty={r['quantity']:.0f}sh")
    print(f"    P&L=${r['realized_pl']:.2f} ({r['realized_pl_pct']:.1f}%)")
    print(f"    thesis : {r['open_thesis']}")
    print(f"    reason : {r['close_reason']}")
    print()

# Open positions
open_pos = q("""
    SELECT symbol,
           entry_time AT TIME ZONE 'America/New_York' AS entry_et,
           entry_price, quantity, open_thesis
    FROM pureai_positions
    WHERE exit_time IS NULL
    ORDER BY entry_time ASC
""")
print(f"--- OPEN POSITIONS ---")
if not open_pos:
    print("  (none)")
for r in open_pos:
    print(f"  {r['symbol']}  entry=${r['entry_price']:.2f}  qty={r['quantity']:.0f}sh")
    print(f"    thesis: {r['open_thesis']}")
    print()

# Recent decisions (last 10 cycles)
decisions = q("""
    SELECT timestamp AT TIME ZONE 'America/New_York' AS ts_et,
           model, decision, error,
           jsonb_array_length(COALESCE(searches, '[]'::jsonb)) AS n_searches
    FROM pureai_decisions
    ORDER BY timestamp DESC LIMIT 10
""")
print(f"--- RECENT CYCLES (last 10) ---")
for r in decisions:
    dec = r['decision'] or {}
    buys  = len(dec.get('buys', []))  if isinstance(dec, dict) else 0
    sells = len(dec.get('sells', [])) if isinstance(dec, dict) else 0
    adds  = len(dec.get('adds', []))  if isinstance(dec, dict) else 0
    view  = dec.get('market_view', '') if isinstance(dec, dict) else ''
    err   = r['error'] or ''
    ts    = (r['ts_et'] or '')[:16]
    print(f"  {ts}  searches={r['n_searches']}  buys={buys} sells={sells} adds={adds}{'  ERROR' if err else ''}")
    if view: print(f"    view: {view[:120]}")
    if err:  print(f"    err : {err[:100]}")

conn.close()
print("\n" + "="*60)
