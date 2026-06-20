"""
Database health audit — shows what's in the DB, flags bad data, and
lists what's safe to clean so ML training isn't contaminated.

Does NOT delete anything automatically. Shows DELETE commands for you to confirm.

Run:  railway run python3 backend/scripts/check_db_health.py
      railway run python3 backend/scripts/check_db_health.py --clean   ← actually deletes
"""
import os, sys, logging
logging.getLogger("services.db").setLevel(logging.CRITICAL)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DRY_RUN = "--clean" not in sys.argv

from services.db import _get_conn

conn = _get_conn()
if not conn:
    print("ERROR: Could not connect to DB. Run via 'railway run'")
    sys.exit(1)

def _q(sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()

def _exec(sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())

def _header(t): print(f"\n{'='*65}\n  {t}\n{'='*65}")
def _ok(m):     print(f"  ✓  {m}")
def _warn(m):   print(f"  ⚠  {m}")
def _bad(m):    print(f"  ✗  {m}")

mode = "DRY-RUN (add --clean to actually delete)" if DRY_RUN else "CLEAN MODE — will delete"
print(f"\n{'='*65}")
print(f"  DB HEALTH AUDIT — {mode}")
print(f"{'='*65}")

# ── 1. position_log ───────────────────────────────────────────────────────────
_header("1. position_log  (ML training data — most critical)")

total = _q("SELECT COUNT(*) FROM position_log")[0][0]
closed = _q("SELECT COUNT(*) FROM position_log WHERE exit_price IS NOT NULL")[0][0]
open_p = total - closed
_ok(f"Total rows: {total}  |  closed (ML trainable): {closed}  |  open: {open_p}")

# Check for data quality issues
null_side = _q("SELECT COUNT(*) FROM position_log WHERE side IS NULL")[0][0]
null_entry_time = _q("SELECT COUNT(*) FROM position_log WHERE entry_time IS NULL")[0][0]
null_entry_price = _q("SELECT COUNT(*) FROM position_log WHERE entry_price IS NULL OR entry_price = 0")[0][0]
closed_no_exit_time = _q("""
    SELECT COUNT(*) FROM position_log
    WHERE exit_price IS NOT NULL AND exit_time IS NULL
""")[0][0]
closed_no_reason = _q("""
    SELECT COUNT(*) FROM position_log
    WHERE exit_price IS NOT NULL AND (exit_reason IS NULL OR exit_reason = '')
""")[0][0]

if null_side:         _bad(f"{null_side} rows with NULL side (ML will skip these)")
else:                 _ok("All rows have side (buy/short)")
if null_entry_time:   _bad(f"{null_entry_time} rows with NULL entry_time (ML will fail on these)")
else:                 _ok("All rows have entry_time")
if null_entry_price:  _bad(f"{null_entry_price} rows with 0/NULL entry_price")
else:                 _ok("All rows have valid entry_price")
if closed_no_exit_time: _warn(f"{closed_no_exit_time} closed rows missing exit_time")
else:                 _ok("All closed rows have exit_time")
if closed_no_reason:  _warn(f"{closed_no_reason} closed rows missing exit_reason")

# Show all closed trades for review
rows = _q("""
    SELECT symbol, side, entry_price, exit_price, exit_reason,
           entry_time::date, exit_time::date,
           CASE WHEN exit_price > 0 AND entry_price > 0
                THEN ROUND((exit_price - entry_price) / entry_price * 100, 2)
                ELSE NULL END AS pl_pct,
           regime
    FROM position_log
    WHERE exit_price IS NOT NULL
    ORDER BY exit_time DESC
    LIMIT 30
""")

if rows:
    print(f"\n  Closed trades (most recent first):\n")
    print(f"  {'Symbol':<8} {'Side':<6} {'Entry':>8} {'Exit':>8} {'PL%':>7}  {'Exit Reason':<20} {'Regime':<6}  Entry→Exit")
    print(f"  {'-'*90}")
    for r in rows:
        sym, side, ep, xp, reason, edate, xdate, pl, reg = r
        pl_str   = f"{pl:+.1f}%" if pl is not None else "open"
        reg_str  = (reg or "?")[:5]
        reason_s = (reason or "?")[:20]
        print(f"  {sym:<8} {(side or '?'):<6} {ep or 0:>8.2f} {xp or 0:>8.2f} {pl_str:>7}  {reason_s:<20} {reg_str:<6}  {edate}→{xdate}")
else:
    _warn("No closed trades yet — ML has nothing to train on")

# Bad data that should be cleaned
phantom_closed = _q("""
    SELECT COUNT(*) FROM position_log
    WHERE exit_price IS NOT NULL
      AND (entry_price IS NULL OR entry_price = 0)
""")[0][0]
if phantom_closed:
    _bad(f"{phantom_closed} rows with exit_price but no entry_price — corrupted, safe to delete")
    if not DRY_RUN:
        _exec("DELETE FROM position_log WHERE exit_price IS NOT NULL AND (entry_price IS NULL OR entry_price = 0)")
        _ok("Deleted corrupted rows")
    else:
        print(f"    → Would run: DELETE FROM position_log WHERE exit_price IS NOT NULL AND (entry_price IS NULL OR entry_price = 0)")

# ── 2. signal_weights ─────────────────────────────────────────────────────────
_header("2. signal_weights  (adaptive scoring — checked weekly by sprint_review)")

sw_rows = _q("SELECT signal_name, current_weight, default_weight, win_rate_30d, sample_count_30d, last_adjusted FROM signal_weights ORDER BY signal_name")
if not sw_rows:
    _warn("signal_weights table is empty — needs to be seeded (run app once on Railway)")
else:
    print(f"\n  {'Signal':<32} {'Cur':>4} {'Def':>4}  {'Win%':>5}  {'N':>4}  {'Adjusted'}")
    print(f"  {'-'*70}")
    has_changes = False
    for r in sw_rows:
        name, cur, dflt, wr, n, adj = r
        wr_s = f"{float(wr)*100:.0f}%" if wr else " n/a"
        n_s  = str(n) if n else "  0"
        adj_s = str(adj) if adj else "never"
        changed = " ← CHANGED" if cur != dflt else ""
        has_changes = has_changes or bool(changed)
        print(f"  {name:<32} {cur:>4} {dflt:>4}  {wr_s:>5}  {n_s:>4}  {adj_s}{changed}")
    if not has_changes:
        _warn("No weights changed yet — sprint_review needs 15+ trades per signal")
    else:
        _ok("sprint_review has adjusted some weights")

# ── 3. sprint_review_daily ────────────────────────────────────────────────────
_header("3. sprint_review_daily  (weekly performance reviews)")

sr_rows = _q("""
    SELECT review_date, opportunity_capture_rate, actual_kova_pnl, flagged_signals
    FROM sprint_review_daily ORDER BY review_date DESC LIMIT 10
""")
if not sr_rows:
    _warn("No sprint reviews on record yet")
else:
    for r in sr_rows:
        dt, opp, pnl, flags = r
        _ok(f"{dt}  capture={float(opp)*100:.0f}% if opp else n/a  pnl={pnl}  flagged={flags}")

# ── 4. ai_cache ───────────────────────────────────────────────────────────────
_header("4. ai_cache  (stale LLM responses — safe to flush)")

try:
    cache_total = _q("SELECT COUNT(*) FROM ai_cache")[0][0]
    cache_stale = _q("SELECT COUNT(*) FROM ai_cache WHERE expires_at < NOW()")[0][0]
    _ok(f"Total cached: {cache_total}  |  expired: {cache_stale}")
    if cache_stale > 0:
        if not DRY_RUN:
            _exec("DELETE FROM ai_cache WHERE expires_at < NOW()")
            _ok(f"Deleted {cache_stale} expired cache entries")
        else:
            print(f"    → Would run: DELETE FROM ai_cache WHERE expires_at < NOW()")
except Exception as e:
    _warn(f"ai_cache check failed: {e}")

# ── 5. connector_health_log ───────────────────────────────────────────────────
_header("5. connector_health_log  (API failures — last 7 days)")

try:
    ch_total = _q("SELECT COUNT(*) FROM connector_health_log")[0][0]
    ch_recent = _q("""
        SELECT connector_name, status, COUNT(*) as n,
               MAX(called_at)::date as last_seen
        FROM connector_health_log
        WHERE called_at > NOW() - INTERVAL '7 days'
        GROUP BY connector_name, status
        ORDER BY connector_name, status
    """)
    _ok(f"Total log entries: {ch_total}")
    if ch_recent:
        print(f"\n  {'Connector':<25} {'Status':<12} {'Count':>6}  Last seen")
        print(f"  {'-'*60}")
        for r in ch_recent:
            name, status, n, last = r
            icon = "✓" if status == "ok" else "✗"
            print(f"  {icon} {name:<24} {status:<12} {n:>6}  {last}")

    # Old logs (>14 days) are pure noise
    old_count = _q("SELECT COUNT(*) FROM connector_health_log WHERE called_at < NOW() - INTERVAL '14 days'")[0][0]
    if old_count > 1000:
        if not DRY_RUN:
            _exec("DELETE FROM connector_health_log WHERE called_at < NOW() - INTERVAL '14 days'")
            _ok(f"Deleted {old_count} old connector log entries (>14 days)")
        else:
            print(f"\n    → Would delete {old_count} entries older than 14 days (safe)")
except Exception as e:
    _warn(f"connector_health check failed: {e}")

# ── 6. bot_activity_log ───────────────────────────────────────────────────────
_header("6. bot_activity_log  (recent bot events)")

try:
    ba_total = _q("SELECT COUNT(*) FROM bot_activity_log")[0][0]
    ba_recent = _q("""
        SELECT event_type, COUNT(*) as n
        FROM bot_activity_log
        WHERE created_at > NOW() - INTERVAL '7 days'
        GROUP BY event_type
        ORDER BY n DESC
        LIMIT 10
    """)
    _ok(f"Total entries: {ba_total}")
    if ba_recent:
        for event, n in ba_recent:
            print(f"    {event:<35} {n:>5}x")
    old_ba = _q("SELECT COUNT(*) FROM bot_activity_log WHERE created_at < NOW() - INTERVAL '30 days'")[0][0]
    if old_ba > 500:
        if not DRY_RUN:
            _exec("DELETE FROM bot_activity_log WHERE created_at < NOW() - INTERVAL '30 days'")
            _ok(f"Deleted {old_ba} old bot activity entries")
        else:
            print(f"    → Would delete {old_ba} entries older than 30 days (safe)")
except Exception as e:
    _warn(f"bot_activity check failed: {e}")

# ── 7. blocked_trades ─────────────────────────────────────────────────────────
_header("7. blocked_trades  (trades the bot rejected and why)")

try:
    bt_rows = _q("""
        SELECT symbol, block_reason, signal_score, blocked_at::date
        FROM blocked_trades
        ORDER BY blocked_at DESC LIMIT 20
    """)
    if not bt_rows:
        print("  No blocked trades on record.")
    else:
        print(f"\n  {'Symbol':<8} {'Score':>5}  {'Date':<12}  Reason")
        print(f"  {'-'*70}")
        for r in bt_rows:
            sym, reason, score, dt = r
            print(f"  {sym:<8} {score or 0:>5.0f}  {str(dt):<12}  {(reason or '')[:45]}")
except Exception as e:
    _warn(f"blocked_trades check failed: {e}")

# ── 8. near_miss_log (if exists) ─────────────────────────────────────────────
_header("8. near_miss_log  (candidates that almost traded)")
try:
    nm_total = _q("SELECT COUNT(*) FROM near_miss_log")[0][0]
    nm_old = _q("SELECT COUNT(*) FROM near_miss_log WHERE logged_at < NOW() - INTERVAL '30 days'")[0][0]
    _ok(f"Total: {nm_total}  |  >30 days old: {nm_old}")
    if nm_old > 0:
        if not DRY_RUN:
            _exec("DELETE FROM near_miss_log WHERE logged_at < NOW() - INTERVAL '30 days'")
            _ok(f"Deleted {nm_old} old near-miss entries")
        else:
            print(f"    → Would delete {nm_old} entries older than 30 days")
except Exception:
    print("  Table doesn't exist yet (created when near-misses are first logged)")

# ── Final summary ─────────────────────────────────────────────────────────────
_header("SUMMARY")
if DRY_RUN:
    print("  This was a DRY RUN — nothing was deleted.")
    print("  To apply safe cleanups, run:")
    print("    railway run python3 backend/scripts/check_db_health.py --clean")
else:
    _ok("Cleanup complete.")

print()
