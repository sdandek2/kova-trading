from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from services import strategy as strategy_service

router = APIRouter(prefix="/api/strategy", tags=["strategy"])


@router.get("/")
def get_strategy():
    s = strategy_service.get_strategy()
    # Return only string-safe fields so Swift [String:String] decode doesn't fail
    return {"key": s["key"], "name": s["name"]}


@router.get("/all")
def get_all_strategies():
    return strategy_service.get_all_strategies()


@router.post("/set/{key}")
def set_strategy(key: str):
    if not strategy_service.set_strategy(key):
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {key}. Choose from: conservative, balanced, aggressive")
    from services.db import log_strategy_change
    log_strategy_change(key)
    return {"message": f"Strategy set to {key}"}


@router.get("/signal-weights")
def get_signal_weights():
    """
    Returns current signal weights with history (last 8 weeks).
    Used by iOS dashboard to show weight trends.
    """
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return []

        with conn.cursor() as cur:
            # Current weights + defaults
            cur.execute("""
                SELECT signal_name, current_weight, default_weight,
                       last_adjusted, adjustment_reason
                FROM signal_weights
                ORDER BY default_weight DESC
            """)
            weights = cur.fetchall()

            # Last 8 weekly history entries per signal
            cur.execute("""
                SELECT signal_name, old_weight, new_weight, direction,
                       win_rate, sample_count, adjusted_at
                FROM signal_weight_history
                WHERE adjusted_at >= CURRENT_DATE - INTERVAL '56 days'
                ORDER BY signal_name, adjusted_at ASC
            """)
            history_rows = cur.fetchall()

        # Group history by signal
        history_map: dict = {}
        for row in history_rows:
            sig = row[0]
            if sig not in history_map:
                history_map[sig] = []
            history_map[sig].append({
                "old_weight": row[1],
                "new_weight": row[2],
                "direction": row[3],
                "win_rate": float(row[4]) if row[4] is not None else None,
                "sample_count": row[5],
                "adjusted_at": row[6].isoformat() if row[6] else None,
            })

        result = []
        for row in weights:
            sig_name, current, default, last_adj, reason = row
            history = history_map.get(sig_name, [])
            # Determine current trend from last 2 history entries
            trend = "flat"
            if len(history) >= 2:
                if history[-1]["new_weight"] > history[-2]["new_weight"]:
                    trend = "up"
                elif history[-1]["new_weight"] < history[-2]["new_weight"]:
                    trend = "down"
            elif len(history) == 1:
                trend = history[0]["direction"] or "flat"

            result.append({
                "signal_name": sig_name,
                "current_weight": current,
                "default_weight": default,
                "pct_of_default": round(current / default * 100) if default else 100,
                "trend": trend,
                "last_adjusted": last_adj.isoformat() if last_adj else None,
                "adjustment_reason": reason,
                "history": history,
            })

        return result

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
