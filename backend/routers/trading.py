from fastapi import APIRouter, HTTPException, Query
from models.trade import TradingStatus, AIAnalysis
from services import trading_engine

router = APIRouter(prefix="/api/trading", tags=["trading"])


@router.get("/activity")
def get_bot_activity(limit: int = Query(50, ge=1, le=200)):
    """
    Return recent bot activity events: scans, approvals, rejections,
    earnings blocks, circuit breakers, trailing stop triggers.
    Powers the real-time activity log in the iOS app.
    """
    from services.db import get_bot_activity_log
    return get_bot_activity_log(limit=limit)


@router.get("/status", response_model=TradingStatus)
def get_status():
    return trading_engine.get_status()


@router.post("/start")
async def start_trading():
    trading_engine.start()
    return {"message": "Trading bot started"}


@router.post("/stop")
async def stop_trading():
    trading_engine.stop()
    return {"message": "Trading bot stopped"}


@router.get("/analysis", response_model=AIAnalysis)
def get_analysis():
    analysis = trading_engine.get_latest_analysis()
    if not analysis:
        return AIAnalysis(reasoning="Waiting for first analysis cycle...")
    return analysis


@router.get("/analysis/recent")
def get_recent_ai_decisions(limit: int = Query(20, ge=1, le=100)):
    """
    Return the most recent AI decisions (approved trades + entry rejections).
    Each item includes: timestamp, symbol, event_type, message (full reasoning).
    Powers the 'Recent AI Decisions' list in the iOS app.
    """
    from services.db import get_bot_activity_log
    all_events = get_bot_activity_log(limit=200)
    decisions = [
        e for e in all_events
        if e.get("event_type") in (
            "approved", "order_placed", "entry_rejected",
            "earnings_block", "circuit_breaker",
            "news_trigger",
        )
    ]
    return decisions[:limit]


@router.get("/history")
def get_trade_history(limit: int = Query(50, ge=1, le=500)):
    """Return the most recent AI trade decisions from the trade_log table."""
    from services.db import _get_conn
    conn = _get_conn()
    if not conn:
        raise HTTPException(status_code=503, detail="Database unavailable")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, timestamp, action, symbol, quantity,
                       reasoning, confidence, market_regime, geo_risk
                FROM trade_log
                ORDER BY timestamp DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        columns = ["id", "timestamp", "action", "symbol", "quantity",
                   "reasoning", "confidence", "market_regime", "geo_risk"]
        # Bug fix: psycopg2 returns timestamp as a datetime object which FastAPI
        # cannot JSON-serialize from a plain dict. Convert to ISO string explicitly.
        result = []
        for row in rows:
            d = dict(zip(columns, row))
            if d.get("timestamp") and hasattr(d["timestamp"], "isoformat"):
                d["timestamp"] = d["timestamp"].isoformat()
            result.append(d)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch trade history: {e}")


@router.post("/eod/run")
async def trigger_eod_analysis():
    """
    Manually trigger the EOD snapshot + AI analysis.
    Use this when the daily report didn't run (e.g. after a server restart post-close).
    """
    import asyncio
    try:
        from services.trading_engine import _save_eod_snapshot
        await _save_eod_snapshot()
        from services.eod_analysis_service import run_eod_analysis as _run_eod
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, _run_eod)
        future.add_done_callback(
            lambda f: None if not f.exception() else None
        )
        return {"message": "EOD analysis triggered — report will be ready in ~30 seconds"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"EOD trigger failed: {e}")


@router.get("/connectors/health")
def get_connectors_health():
    """
    Live connector health check — tests each data source and reports status.
    Use this to verify yfinance, Alpaca, news APIs are all reachable after deploy.
    """
    import time
    results = {}

    # yfinance / quiver
    t0 = time.time()
    try:
        import yfinance as yf
        version = getattr(yf, "__version__", "unknown")
        ticker = yf.Ticker("AAPL")
        info = ticker.info or {}
        inst_pct = float(info.get("institutionsPercentHeld") or 0) * 100
        results["yfinance"] = {
            "status": "ok",
            "version": version,
            "detail": f"AAPL institutional hold: {inst_pct:.0f}%",
            "latency_ms": round((time.time() - t0) * 1000),
        }
    except ImportError as e:
        results["yfinance"] = {"status": "import_error", "detail": str(e)}
    except Exception as e:
        import yfinance as yf
        results["yfinance"] = {
            "status": "error",
            "version": getattr(yf, "__version__", "unknown"),
            "detail": f"{type(e).__name__}: {e}",
            "latency_ms": round((time.time() - t0) * 1000),
        }

    # Alpaca market data
    t0 = time.time()
    try:
        from services.alpaca_service import trading_client
        acct = trading_client.get_account()
        results["alpaca"] = {
            "status": "ok",
            "detail": f"account equity=${float(acct.equity):,.0f}",
            "latency_ms": round((time.time() - t0) * 1000),
        }
    except Exception as e:
        results["alpaca"] = {"status": "error", "detail": str(e)}

    # DB
    t0 = time.time()
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            results["database"] = {
                "status": "ok",
                "latency_ms": round((time.time() - t0) * 1000),
            }
        else:
            results["database"] = {"status": "unavailable", "detail": "no connection"}
    except Exception as e:
        results["database"] = {"status": "error", "detail": str(e)}

    # Connector failure rates from DB
    try:
        from services.db import get_connector_health
        rows = get_connector_health(hours=1)
        for row in rows:
            name = row["connector_name"]
            if name not in results:
                results[name] = {
                    "status": "ok" if row["failure_pct"] < 50 else "degraded",
                    "failure_pct_1h": row["failure_pct"],
                    "calls_1h": row["total_calls"],
                }
    except Exception:
        pass

    overall = "ok" if all(v.get("status") == "ok" for v in results.values()) else "degraded"
    return {"overall": overall, "connectors": results}


@router.post("/reset-cycle")
def reset_cycle():
    """
    Clear all Lakshmi trade/position records from the previous account cycle.
    Call this after resetting the Alpaca paper account so metrics start fresh.
    Does NOT touch wheel, experiment, or connector_health tables.
    """
    from services.db import _get_conn
    from datetime import datetime, timezone
    conn = _get_conn()
    if not conn:
        raise HTTPException(status_code=503, detail="Database unavailable")
    tables = [
        "trade_log",
        "position_log",
        "daily_summary",
        "circuit_breaker_log",
        "bot_activity_log",
        "blocked_trades",
        "trade_slippage_log",
        "daily_universe_log",
        "near_miss_log",
        "signal_performance_log",
        "sprint_review_daily",
    ]
    counts = {}
    try:
        with conn.cursor() as cur:
            for table in tables:
                try:
                    cur.execute(f"DELETE FROM {table}")
                    counts[table] = cur.rowcount
                except Exception:
                    counts[table] = "skipped"
        conn.commit()
        return {
            "status": "ok",
            "reset_at": datetime.now(timezone.utc).isoformat(),
            "rows_deleted": counts,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/connectors/reset-quiver")
def reset_quiver_health():
    """
    Clear stale quiver/yfinance connector_health_log records.
    Use once after fixing a connector to stop false connector_critical alerts.
    """
    from services.db import _get_conn
    conn = _get_conn()
    if not conn:
        raise HTTPException(status_code=503, detail="Database unavailable")
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM connector_health_log WHERE connector_name = 'quiver'")
            deleted = cur.rowcount
        conn.commit()
        return {"status": "ok", "deleted_records": deleted, "message": "Quiver health history cleared — fresh start"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/macro")
def get_macro():
    from services.macro import get_macro_context, get_sector_rotation
    macro = get_macro_context()
    macro["sector_rotation"] = get_sector_rotation()
    return macro


@router.get("/premarket")
def get_premarket():
    """
    Return the latest pre-market scan result.
    Runs automatically 9:00-9:30 AM EST each trading day.
    Returns top stocks by news sentiment + macro regime + headlines.
    """
    from services.db import cache_get
    data = cache_get("premarket_scan")
    if not data:
        return {
            "available": False,
            "message": "Pre-market scan runs at 9:00-9:30 AM EST before market open.",
            "scanned_at": None,
            "macro_regime": None,
            "top_stocks": [],
            "headlines": [],
        }

    # Sort sentiment dict into ranked list
    sentiment = data.get("sentiment", {})
    top_stocks = sorted(
        [{"symbol": sym, "mentions": cnt} for sym, cnt in sentiment.items()],
        key=lambda x: x["mentions"],
        reverse=True,
    )[:10]

    return {
        "available": True,
        "scanned_at": data.get("scanned_at"),
        "macro_regime": data.get("macro_regime"),
        "top_stocks": top_stocks,
        "headlines": data.get("headlines", [])[:10],
    }
