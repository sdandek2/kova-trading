"""
sec_intel.py — API endpoints for SEC Intelligence strategy.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/sec-intel", tags=["sec-intel"])


@router.get("/status")
def get_status():
    from services.sec_intel_engine import status
    return status()


@router.get("/signals")
def get_signals(limit: int = 50):
    """Latest actionable signals synced from DuckDB."""
    from services.db import _get_conn
    conn = _get_conn()
    if not conn:
        return {"signals": [], "error": "DB unavailable"}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT institution, ticker, action, quarter, filed_date,
                   score, is_whitelist, hold_days, investment_type
            FROM sec_intel_signals
            WHERE filed_date >= CURRENT_DATE - INTERVAL '180 days'
            ORDER BY is_whitelist DESC, score DESC, filed_date DESC
            LIMIT %s
        """, [limit])
        rows = cur.fetchall()
    return {"signals": [
        {
            "institution": r[0], "ticker": r[1], "action": r[2],
            "quarter": r[3], "filed_date": str(r[4]), "score": r[5],
            "is_whitelist": r[6], "hold_days": r[7], "type": r[8],
        }
        for r in rows
    ]}


@router.get("/positions")
def get_positions():
    """All open SEC Intel positions."""
    from services.db import _get_conn
    conn = _get_conn()
    if not conn:
        return {"positions": []}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, ticker, institution, signal_quarter, entry_price, shares,
                   position_size_usd, stop_price, peak_price,
                   sold_25pct_at, sold_50pct_at, trail_stop_price,
                   entry_date, max_hold_date, status, notes
            FROM sec_intel_trade_log
            WHERE status = 'open'
            ORDER BY entry_date DESC
        """)
        rows = cur.fetchall()
    return {"positions": [
        {
            "id": r[0], "ticker": r[1], "institution": r[2], "quarter": r[3],
            "entry_price": r[4], "shares": r[5], "size_usd": r[6],
            "stop": r[7], "peak": r[8],
            "l1_exit": r[9], "l2_exit": r[10], "trail_stop": r[11],
            "entry_date": str(r[12]) if r[12] else None,
            "max_hold": str(r[13]) if r[13] else None,
            "status": r[14], "notes": r[15],
        }
        for r in rows
    ]}


@router.get("/history")
def get_history(limit: int = 50):
    """Closed trade history with P&L."""
    from services.db import _get_conn
    conn = _get_conn()
    if not conn:
        return {"trades": []}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ticker, institution, signal_quarter, entry_price, exit_price,
                   shares, realized_pl, realized_pl_pct, exit_reason,
                   entry_date, exit_date, hold_days_actual
            FROM sec_intel_trade_log
            WHERE status = 'closed'
            ORDER BY exit_date DESC
            LIMIT %s
        """, [limit])
        rows = cur.fetchall()
    return {"trades": [
        {
            "ticker": r[0], "institution": r[1], "quarter": r[2],
            "entry": r[3], "exit": r[4], "shares": r[5],
            "pl": r[6], "pl_pct": r[7], "reason": r[8],
            "entry_date": str(r[9]) if r[9] else None,
            "exit_date": str(r[10]) if r[10] else None,
            "hold_days": r[11],
        }
        for r in rows
    ]}


@router.post("/sync-signals")
def sync_signals(duckdb_path: str = "/app/data/sec_intelligence.duckdb"):
    """
    Sync signals from a local DuckDB file.
    Run locally via sync_to_railway.py — DuckDB is not installed on Railway.
    """
    try:
        import duckdb  # noqa: F401
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="DuckDB not available on Railway. Run sync_to_railway.py locally instead."
        )
    from services.sec_intel_engine import sync_signals_from_duckdb
    count = sync_signals_from_duckdb(duckdb_path)
    return {"synced": count}


@router.post("/process-signals")
def process_signals():
    """Manually trigger signal processing and trade entries."""
    from services.sec_intel_engine import process_new_signals
    process_new_signals()  # bear regime check is inside process_new_signals
    return {"status": "ok"}


@router.post("/manage-positions")
def manage_positions():
    """Manually trigger position management (exit ladder, stops)."""
    from services.sec_intel_engine import manage_positions
    manage_positions()
    return {"status": "ok"}


@router.get("/performance")
def get_performance():
    """Win rate, total P&L, best/worst trade — computed from trade log."""
    from services.db import _get_conn
    conn = _get_conn()
    if not conn:
        return {"error": "DB unavailable"}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                COUNT(*)                                                       AS total_trades,
                COUNT(*) FILTER (WHERE total_pl > 0)                           AS wins,
                COUNT(*) FILTER (WHERE total_pl <= 0)                          AS losses,
                COALESCE(SUM(total_pl), 0)                                     AS net_pl,
                COALESCE(AVG(realized_pl_pct), 0)                              AS avg_pl_pct,
                COALESCE(MAX(total_pl), 0)                                     AS best_trade,
                COALESCE(MIN(total_pl), 0)                                     AS worst_trade,
                COALESCE(AVG(hold_days_actual), 0)                             AS avg_hold_days
            FROM (
                SELECT
                    realized_pl_pct,
                    hold_days_actual,
                    COALESCE(realized_pl, 0)
                        + COALESCE(sold_25pct_pl, 0)
                        + COALESCE(sold_50pct_pl, 0) AS total_pl
                FROM sec_intel_trade_log
                WHERE status = 'closed'
            ) t
        """)
        row = cur.fetchone()
    if not row or row[0] == 0:
        return {"total_trades": 0, "message": "No closed trades yet"}
    total, wins, losses, net_pl, avg_pct, best, worst, avg_hold = row
    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / total * 100, 1) if total else 0,
        "net_pl": round(float(net_pl), 2),
        "avg_pl_pct": round(float(avg_pct), 2),
        "best_trade": round(float(best), 2),
        "worst_trade": round(float(worst), 2),
        "avg_hold_days": round(float(avg_hold), 1),
    }


class ScoreRequest(BaseModel):
    institution: str
    action: str
    convergence_count: int = 0


@router.post("/score")
def score_signal(req: ScoreRequest):
    """Preview score for a given signal."""
    from services.sec_intel_engine import score_signal
    score, is_whitelist = score_signal(req.institution, req.action, req.convergence_count)
    return {
        "institution": req.institution,
        "action": req.action,
        "convergence_count": req.convergence_count,
        "score": score,
        "is_whitelist": is_whitelist,
        "auto_trade": is_whitelist or score >= 6,
    }
