"""
Shows current signal_weights from DB and any adjustment history.
Tells you if sprint_review has ever changed weights from their defaults.

Run:  railway run python3 backend/scripts/check_signal_weights.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
logging.getLogger("services.db").setLevel(logging.CRITICAL)

from services.db import _get_conn

print("=" * 65)
print("SIGNAL WEIGHTS — current vs default")
print("=" * 65)

conn = _get_conn()
if not conn:
    print("ERROR: Could not connect to DB (run via 'railway run')")
    sys.exit(1)

with conn.cursor() as cur:
    cur.execute("""
        SELECT signal_name, current_weight, default_weight,
               win_rate_30d, sample_count_30d, last_adjusted, adjustment_reason
        FROM signal_weights
        ORDER BY signal_name
    """)
    rows = cur.fetchall()

if not rows:
    print("\n  No rows found — signal_weights table may not exist yet.")
    print("  Run the app once on Railway to seed it.\n")
else:
    changed = []
    unchanged = []
    for r in rows:
        name, cur_w, def_w, wr, samples, adj_date, reason = r
        if cur_w != def_w:
            changed.append(r)
        else:
            unchanged.append(r)

    if changed:
        print(f"\n  ADJUSTED ({len(changed)} signals changed by sprint_review):\n")
        for r in changed:
            name, cur_w, def_w, wr, samples, adj_date, reason = r
            direction = "▲" if cur_w > def_w else "▼"
            wr_str = f"{float(wr)*100:.0f}%" if wr else "n/a"
            samples_str = str(samples) if samples else "0"
            print(f"  {direction} {name:<30}  {def_w} → {cur_w}  "
                  f"win_rate={wr_str}  n={samples_str}")
            if reason:
                print(f"    reason: {reason}")
            if adj_date:
                print(f"    date  : {adj_date}")
    else:
        print("\n  No weights changed yet — all still at defaults.")
        print("  sprint_review needs 15+ trades per signal to adjust weights.\n")

    print(f"\n  UNCHANGED ({len(unchanged)} signals at default):\n")
    for r in unchanged:
        name, cur_w, def_w, wr, samples, adj_date, reason = r
        wr_str = f"{float(wr)*100:.0f}%" if wr else "n/a"
        samples_str = str(samples) if samples else "0"
        print(f"    {name:<30}  weight={cur_w}  win_rate={wr_str}  n={samples_str}")

print()

# ── Adjustment history ────────────────────────────────────────────────────────
print("=" * 65)
print("ADJUSTMENT HISTORY (last 20 changes)")
print("=" * 65)
with conn.cursor() as cur:
    cur.execute("""
        SELECT signal_name, old_weight, new_weight, direction,
               win_rate, sample_count, adjusted_at
        FROM signal_weight_history
        ORDER BY adjusted_at DESC, id DESC
        LIMIT 20
    """)
    hist = cur.fetchall()

if not hist:
    print("\n  No history yet — sprint_review hasn't adjusted anything.\n")
else:
    print()
    for h in hist:
        name, old_w, new_w, direction, wr, n, dt = h
        wr_str = f"{float(wr)*100:.0f}%" if wr else "n/a"
        print(f"  {dt}  {name:<30}  {old_w} → {new_w}  win={wr_str}  n={n}")

print()

# ── Sprint review schedule ────────────────────────────────────────────────────
print("=" * 65)
print("SPRINT REVIEW — last run dates")
print("=" * 65)
with conn.cursor() as cur:
    cur.execute("""
        SELECT review_date, opportunity_capture_rate, actual_kova_pnl,
               flagged_signals
        FROM sprint_review_daily
        ORDER BY review_date DESC
        LIMIT 10
    """)
    reviews = cur.fetchall()

if not reviews:
    print("\n  No sprint reviews on record yet.\n")
else:
    print()
    for rv in reviews:
        dt, opp, pnl, flags = rv
        opp_str = f"{float(opp)*100:.0f}%" if opp else "n/a"
        pnl_str = f"${float(pnl):.2f}" if pnl else "n/a"
        flags_str = ", ".join(flags) if flags else "none"
        print(f"  {dt}  opportunity_capture={opp_str}  kova_pnl={pnl_str}  flagged={flags_str}")

print()
