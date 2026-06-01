"""
db.py — PostgreSQL-backed cache for Kova.

Provides a simple key/value cache with TTL stored in Postgres.
Falls back silently to in-memory if DATABASE_URL is not set or connection fails,
so local development without Postgres still works fine.
"""

import json
import logging
import re
import threading
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

# In-memory fallback (used when Postgres is unavailable)
_memory_cache: dict[str, tuple[Any, datetime]] = {}

_conn = None  # module-level connection (lazy)
_conn_lock = threading.Lock()


def _get_conn():
    """Return a live psycopg2 connection, or None if DB is unavailable."""
    global _conn
    with _conn_lock:
        from config import settings

        if not settings.database_url:
            return None

        # Re-use existing connection if still open
        try:
            if _conn and not _conn.closed:
                with _conn.cursor() as _hc:
                    _hc.execute("SELECT 1")
                return _conn
        except Exception:
            _conn = None

        try:
            import psycopg2
            _conn = psycopg2.connect(settings.database_url)
            _conn.autocommit = True
            logger.info("Connected to Postgres.")
            _ensure_table(_conn)
            return _conn
        except Exception as e:
            logger.warning(f"Postgres unavailable — using in-memory cache. ({e})")
            return None


def _ensure_table(conn):
    """Create required tables if they don't exist."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_cache (
                key         TEXT PRIMARY KEY,
                data        JSONB NOT NULL,
                cached_at   TIMESTAMPTZ NOT NULL,
                expires_at  TIMESTAMPTZ NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_log (
                id              SERIAL PRIMARY KEY,
                timestamp       TIMESTAMPTZ NOT NULL,
                action          TEXT NOT NULL,
                symbol          TEXT,
                quantity        INTEGER,
                reasoning       TEXT,
                confidence      TEXT,
                market_regime   TEXT,
                geo_risk        TEXT,
                take_profit_pct FLOAT,
                stop_loss_pct   FLOAT,
                partial_exit    BOOLEAN DEFAULT FALSE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_summary (
                id              SERIAL PRIMARY KEY,
                date            DATE UNIQUE NOT NULL,
                portfolio_value FLOAT NOT NULL,
                cash            FLOAT,
                day_pl          FLOAT,
                day_pl_pct      FLOAT,
                total_decisions INTEGER DEFAULT 0,
                buy_decisions   INTEGER DEFAULT 0,
                sell_decisions  INTEGER DEFAULT 0,
                hold_decisions  INTEGER DEFAULT 0,
                strategy        TEXT,
                spy_close       FLOAT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS strategy_log (
                id        SERIAL PRIMARY KEY,
                timestamp TIMESTAMPTZ NOT NULL,
                strategy  TEXT NOT NULL
            )
        """)
        # ── NEW: position_log ───────────────────────────────────────────────
        # Tracks full round-trip of each position: open → close with realized P&L.
        # This is the ground truth for "did this trade actually make money?"
        cur.execute("""
            CREATE TABLE IF NOT EXISTS position_log (
                id                   SERIAL PRIMARY KEY,
                symbol               TEXT NOT NULL,
                side                 VARCHAR(10) DEFAULT 'long',
                entry_time           TIMESTAMPTZ,
                exit_time            TIMESTAMPTZ,
                entry_price          FLOAT,
                exit_price           FLOAT,
                quantity             INTEGER,
                entry_rsi            FLOAT,
                entry_macd_hist_pct  FLOAT,
                entry_score          FLOAT,
                realized_pl          FLOAT,
                realized_pl_pct      FLOAT,
                hold_duration_mins   INTEGER,
                exit_reason          TEXT,
                strategy             TEXT,
                claude_reasoning     TEXT,
                market_regime        TEXT
            )
        """)
        # Migration: add side column to existing tables that predate this column
        cur.execute("""
            ALTER TABLE position_log
            ADD COLUMN IF NOT EXISTS side VARCHAR(10) DEFAULT 'long'
        """)
        cur.execute("""ALTER TABLE position_log ADD COLUMN IF NOT EXISTS entry_rsi FLOAT""")
        cur.execute("""ALTER TABLE position_log ADD COLUMN IF NOT EXISTS entry_macd_hist_pct FLOAT""")
        cur.execute("""ALTER TABLE position_log ADD COLUMN IF NOT EXISTS entry_score FLOAT""")
        cur.execute("""ALTER TABLE position_log ADD COLUMN IF NOT EXISTS exit_reason_inferred BOOLEAN DEFAULT FALSE""")
        # ── NEW: circuit_breaker_log ────────────────────────────────────────
        # Every time the daily loss limit fires, record when and why.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS circuit_breaker_log (
                id              SERIAL PRIMARY KEY,
                triggered_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                day_pl_percent  FLOAT,
                portfolio_value FLOAT,
                limit_pct       FLOAT
            )
        """)
        # ── NEW: bot_activity_log ───────────────────────────────────────────
        # Per-cycle audit trail: what did the bot scan, approve, reject, and why.
        # Powers the real-time activity feed in the iOS app.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_activity_log (
                id          SERIAL PRIMARY KEY,
                timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                cycle_id    TEXT,
                event_type  TEXT NOT NULL,
                symbol      TEXT,
                message     TEXT NOT NULL
            )
        """)
        # ── NEW: app_settings ───────────────────────────────────────────────
        # Persistent user preferences (trading_budget, prompt_override, etc.)
        # Unlike ai_cache, these have no TTL and never expire.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key        TEXT PRIMARY KEY,
                value      JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)


# ── Public API ─────────────────────────────────────────────────────────────


def cache_get(key: str) -> Optional[Any]:
    """
    Return cached value for *key* if it exists and hasn't expired.
    Returns None on miss or expiry.
    """
    conn = _get_conn()
    now = datetime.now(timezone.utc)

    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT data FROM ai_cache WHERE key = %s AND expires_at > %s",
                    (key, now),
                )
                row = cur.fetchone()
                if row:
                    logger.debug(f"cache_get HIT (pg): {key}")
                    return row[0]  # psycopg2 returns JSONB as dict already
        except Exception as e:
            logger.warning(f"cache_get pg error ({e}), falling back to memory.")

    # In-memory fallback
    entry = _memory_cache.get(key)
    if entry:
        value, expires_at = entry
        if expires_at > now:
            logger.debug(f"cache_get HIT (memory): {key}")
            return value
        else:
            del _memory_cache[key]

    return None


def cache_set(key: str, value: Any, ttl_seconds: int) -> None:
    """
    Store *value* under *key* with a TTL of *ttl_seconds*.
    Writes to Postgres if available, always writes to in-memory fallback.
    """
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=ttl_seconds)

    # Always keep in-memory copy as a safety net
    _memory_cache[key] = (value, expires_at)

    conn = _get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ai_cache (key, data, cached_at, expires_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (key) DO UPDATE
                        SET data       = EXCLUDED.data,
                            cached_at  = EXCLUDED.cached_at,
                            expires_at = EXCLUDED.expires_at
                    """,
                    (key, json.dumps(value), now, expires_at),
                )
            logger.debug(f"cache_set (pg): {key} ttl={ttl_seconds}s")
        except Exception as e:
            logger.warning(f"cache_set pg error ({e}), stored in memory only.")


def cache_delete(key: str) -> None:
    """Remove a cache entry from both memory and Postgres."""
    _memory_cache.pop(key, None)
    conn = _get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM ai_cache WHERE key = %s", (key,))
            logger.debug(f"cache_delete: {key}")
        except Exception as e:
            logger.warning(f"cache_delete pg error ({e})")


def get_recent_trade_outcomes(limit: int = 10) -> list[dict]:
    """
    Read the most recent AI trade decisions from trade_log.
    Returns list of dicts with keys: action, symbol, quantity, reasoning, confidence, timestamp, market_regime
    Used to inject trade history into AI prompts so Claude learns from past decisions.
    """
    try:
        conn = _get_conn()
        if not conn:
            return []

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT action, symbol, quantity, reasoning, confidence, market_regime, timestamp
                FROM trade_log
                ORDER BY timestamp DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()

        results = []
        for row in rows:
            action, symbol, quantity, reasoning, confidence, market_regime, timestamp = row
            results.append({
                "action": action,
                "symbol": symbol,
                "quantity": quantity,
                "reasoning": reasoning,
                "confidence": confidence,
                "market_regime": market_regime,
                "timestamp": timestamp.isoformat() if timestamp else None,
            })
        return results
    except Exception as e:
        logger.warning(f"get_recent_trade_outcomes failed ({e}), returning empty list.")
        return []


def log_trade_decision(decision_data: dict) -> None:
    """
    Insert a record of an AI trade decision into the trade_log table.
    Never raises — if the DB is unavailable, logs a warning and continues.
    """
    try:
        conn = _get_conn()
        if not conn:
            logger.warning("log_trade_decision: no DB connection, skipping log.")
            return

        with conn.cursor() as cur:
            # Bug fix: DDL (CREATE TABLE + ALTER TABLE) was running on every call — every
            # trade decision, 3+ times per cycle. Moved to _ensure_tables() which runs
            # once at startup. These lines are now a no-op guard kept only for safety.
            cur.execute(
                """
                INSERT INTO trade_log
                    (timestamp, action, symbol, quantity, reasoning, confidence,
                     market_regime, geo_risk, take_profit_pct, stop_loss_pct, partial_exit)
                VALUES
                    (%(timestamp)s, %(action)s, %(symbol)s, %(quantity)s,
                     %(reasoning)s, %(confidence)s, %(market_regime)s, %(geo_risk)s,
                     %(take_profit_pct)s, %(stop_loss_pct)s, %(partial_exit)s)
                """,
                {
                    "timestamp":       decision_data.get("timestamp", datetime.now(timezone.utc)),
                    "action":          decision_data.get("action", "hold"),
                    "symbol":          decision_data.get("symbol"),
                    "quantity":        decision_data.get("quantity"),
                    "reasoning":       decision_data.get("reasoning"),
                    "confidence":      decision_data.get("confidence"),
                    "market_regime":   decision_data.get("market_regime"),
                    "geo_risk":        decision_data.get("geo_risk"),
                    "take_profit_pct": decision_data.get("take_profit_pct"),
                    "stop_loss_pct":   decision_data.get("stop_loss_pct"),
                    "partial_exit":    decision_data.get("partial_exit", False),
                },
            )
        logger.info(f"log_trade_decision: logged action={decision_data.get('action')} symbol={decision_data.get('symbol')}")
    except Exception as e:
        logger.warning(f"log_trade_decision failed ({e}), continuing without logging.")


def save_daily_summary(data: dict) -> None:
    """
    Upsert an end-of-day performance snapshot into daily_summary.
    Called once per day when the market closes.
    Never raises.
    """
    try:
        conn = _get_conn()
        if not conn:
            logger.warning("save_daily_summary: no DB connection, skipping.")
            return
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO daily_summary
                    (date, portfolio_value, cash, day_pl, day_pl_pct,
                     total_decisions, buy_decisions, sell_decisions, hold_decisions,
                     strategy, spy_close)
                VALUES
                    (%(date)s, %(portfolio_value)s, %(cash)s, %(day_pl)s, %(day_pl_pct)s,
                     %(total_decisions)s, %(buy_decisions)s, %(sell_decisions)s, %(hold_decisions)s,
                     %(strategy)s, %(spy_close)s)
                ON CONFLICT (date) DO UPDATE SET
                    portfolio_value = EXCLUDED.portfolio_value,
                    cash            = EXCLUDED.cash,
                    day_pl          = EXCLUDED.day_pl,
                    day_pl_pct      = EXCLUDED.day_pl_pct,
                    total_decisions = EXCLUDED.total_decisions,
                    buy_decisions   = EXCLUDED.buy_decisions,
                    sell_decisions  = EXCLUDED.sell_decisions,
                    hold_decisions  = EXCLUDED.hold_decisions,
                    strategy        = EXCLUDED.strategy,
                    spy_close       = EXCLUDED.spy_close
            """, {
                "date":             data.get("date"),
                "portfolio_value":  data.get("portfolio_value"),
                "cash":             data.get("cash"),
                "day_pl":           data.get("day_pl"),
                "day_pl_pct":       data.get("day_pl_pct"),
                "total_decisions":  data.get("total_decisions", 0),
                "buy_decisions":    data.get("buy_decisions", 0),
                "sell_decisions":   data.get("sell_decisions", 0),
                "hold_decisions":   data.get("hold_decisions", 0),
                "strategy":         data.get("strategy"),
                "spy_close":        data.get("spy_close"),
            })
        logger.info(f"save_daily_summary: saved snapshot for {data.get('date')} — portfolio=${data.get('portfolio_value'):,.2f}")
    except Exception as e:
        logger.warning(f"save_daily_summary failed ({e}), skipping.")


def log_strategy_change(strategy: str) -> None:
    """
    Record a strategy change with a timestamp.
    Never raises.
    """
    try:
        conn = _get_conn()
        if not conn:
            return
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO strategy_log (timestamp, strategy) VALUES (%s, %s)",
                (datetime.now(timezone.utc), strategy),
            )
        logger.info(f"log_strategy_change: strategy set to '{strategy}'")
    except Exception as e:
        logger.warning(f"log_strategy_change failed ({e}), skipping.")


def get_daily_summaries(days: int = 365) -> list[dict]:
    """
    Return daily_summary rows for the last *days* days, oldest first.
    Used by the performance endpoint for Sharpe ratio and equity curve.
    """
    try:
        conn = _get_conn()
        if not conn:
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT date, portfolio_value, cash, day_pl, day_pl_pct,
                       total_decisions, buy_decisions, sell_decisions, hold_decisions,
                       strategy, spy_close
                FROM daily_summary
                WHERE date >= %s
                ORDER BY date ASC
            """, (cutoff,))
            rows = cur.fetchall()
        return [
            {
                "date":             str(row[0]),
                "portfolio_value":  row[1],
                "cash":             row[2],
                "day_pl":           row[3],
                "day_pl_pct":       row[4],
                "total_decisions":  row[5],
                "buy_decisions":    row[6],
                "sell_decisions":   row[7],
                "hold_decisions":   row[8],
                "strategy":         row[9],
                "spy_close":        row[10],
            }
            for row in rows
        ]
    except Exception as e:
        logger.warning(f"get_daily_summaries failed ({e}), returning empty.")
        return []


def cleanup_old_trade_logs(days: int = 90) -> None:
    """
    Delete trade_log entries older than *days* days.
    Run periodically to prevent unbounded table growth.
    Safe to call at any time — never raises.
    """
    try:
        conn = _get_conn()
        if not conn:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM trade_log WHERE timestamp < %s", (cutoff,))
            deleted = cur.rowcount
        if deleted:
            logger.info(f"cleanup_old_trade_logs: removed {deleted} rows older than {days} days")
    except Exception as e:
        logger.warning(f"cleanup_old_trade_logs failed ({e}), skipping.")


# ── position_log helpers ───────────────────────────────────────────────────


def log_position_open(symbol: str, entry_price: float, quantity: int,
                      strategy: str = None, claude_reasoning: str = None,
                      market_regime: str = None, side: str = "long",
                      entry_rsi: float = None, entry_macd_hist_pct: float = None,
                      entry_score: float = None) -> Optional[int]:
    """
    Record that a new position was opened.
    side: "long" | "short"
    Returns the row id so the caller can update it on close, or None on failure.
    """
    try:
        conn = _get_conn()
        if not conn:
            return None
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO position_log
                    (symbol, side, entry_time, entry_price, quantity, strategy,
                     claude_reasoning, market_regime, entry_rsi, entry_macd_hist_pct, entry_score)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (symbol, side, datetime.now(timezone.utc), entry_price, quantity,
                  strategy, claude_reasoning, market_regime, entry_rsi,
                  entry_macd_hist_pct, entry_score))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.warning(f"log_position_open failed ({e})")
        return None


def log_position_close(symbol: str, exit_price: float, exit_reason: str,
                       entry_price: float = None, quantity: int = None,
                       entry_time: datetime = None, side: str = "long",
                       strategy: str = None, claude_reasoning: str = None,
                       market_regime: str = None, entry_rsi: float = None,
                       entry_macd_hist_pct: float = None, entry_score: float = None,
                       exit_reason_inferred: bool = False) -> None:
    """
    Update the most recent open position_log row for *symbol* with exit data.
    Also handles the case where no open row exists (logs a standalone closed row).
    side: "long" (profit = exit > entry) | "short" (profit = entry > exit)
    Never raises.
    """
    def _calc_pl(ep, xp, qty, side):
        """P&L is positive when trade is profitable regardless of direction."""
        if not ep or not qty:
            return None, None
        if side == "short":
            pl = (ep - xp) * qty        # short profits when price falls
            pct = (ep - xp) / ep * 100
        else:
            pl = (xp - ep) * qty        # long profits when price rises
            pct = (xp - ep) / ep * 100
        return pl, pct

    try:
        conn = _get_conn()
        if not conn:
            return
        now = datetime.now(timezone.utc)
        with conn.cursor() as cur:
            # Find the most recent open row (no exit_time yet)
            cur.execute("""
                SELECT id, entry_price, quantity, entry_time
                FROM position_log
                WHERE symbol = %s AND exit_time IS NULL
                AND (side = %s OR side IS NULL)
                ORDER BY entry_time DESC NULLS LAST
                LIMIT 1
            """, (symbol, side))
            row = cur.fetchone()

            if row:
                pos_id, ep, qty, et = row
                ep = ep or entry_price or 0
                qty = qty or quantity or 0
                et = et or entry_time or now
                # Read side from DB row (populated by log_position_open); fall back to caller param
                cur.execute("SELECT side FROM position_log WHERE id = %s", (pos_id,))
                side_row = cur.fetchone()
                db_side = (side_row[0] if side_row and side_row[0] else side) or "long"
                realized_pl, realized_pl_pct = _calc_pl(ep, exit_price, qty, db_side)
                hold_mins = int((now - et).total_seconds() / 60) if et else None
                cur.execute("""
                    UPDATE position_log SET
                        exit_time         = %s,
                        exit_price        = %s,
                        realized_pl       = %s,
                        realized_pl_pct   = %s,
                        hold_duration_mins = %s,
                        exit_reason       = %s,
                        exit_reason_inferred = %s,
                        strategy          = COALESCE(strategy, %s),
                        claude_reasoning  = COALESCE(claude_reasoning, %s),
                        market_regime     = COALESCE(market_regime, %s),
                        entry_rsi         = COALESCE(entry_rsi, %s),
                        entry_macd_hist_pct = COALESCE(entry_macd_hist_pct, %s),
                        entry_score       = COALESCE(entry_score, %s)
                    WHERE id = %s
                """, (now, exit_price, realized_pl, realized_pl_pct,
                      hold_mins, exit_reason, bool(exit_reason_inferred), strategy, claude_reasoning,
                      market_regime, entry_rsi, entry_macd_hist_pct, entry_score, pos_id))
            else:
                # No open row — insert a closed record directly
                ep = entry_price or 0
                qty = quantity or 0
                realized_pl, realized_pl_pct = _calc_pl(ep, exit_price, qty, side)
                cur.execute("""
                    INSERT INTO position_log
                        (symbol, side, entry_time, exit_time, entry_price, exit_price,
                         quantity, entry_rsi, entry_macd_hist_pct, entry_score,
                         realized_pl, realized_pl_pct, exit_reason, exit_reason_inferred, strategy,
                         claude_reasoning, market_regime)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (symbol, side or "long", entry_time or now, now, ep, exit_price,
                      qty, entry_rsi, entry_macd_hist_pct, entry_score,
                      realized_pl, realized_pl_pct, exit_reason, bool(exit_reason_inferred), strategy,
                      claude_reasoning, market_regime))
        logger.info(f"log_position_close: {symbol} exit=${exit_price:.2f} reason={exit_reason}")
    except Exception as e:
        logger.warning(f"log_position_close failed ({e})")


def get_position_history(limit: int = 50) -> list[dict]:
    """
    Return the most recent closed trades from position_log, newest first.
    Only returns rows with an exit_time (completed round-trips).
    """
    try:
        conn = _get_conn()
        if not conn:
            return []
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, side, entry_time, exit_time, entry_price, exit_price,
                       quantity, entry_rsi, entry_macd_hist_pct, entry_score,
                       realized_pl, realized_pl_pct, hold_duration_mins,
                       exit_reason, exit_reason_inferred, strategy, claude_reasoning, market_regime
                FROM position_log
                WHERE exit_time IS NOT NULL
                ORDER BY exit_time DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
        return [
            {
                "symbol":            r[0],
                "side":              r[1] or "long",
                "entry_time":        r[2].isoformat() if r[2] else None,
                "exit_time":         r[3].isoformat() if r[3] else None,
                "entry_price":       r[4],
                "exit_price":        r[5],
                "quantity":          r[6],
                "entry_rsi":         r[7],
                "entry_macd_hist_pct": r[8],
                "entry_score":       r[9],
                "realized_pl":       r[10],
                "realized_pl_pct":   r[11],
                "hold_duration_mins": r[12],
                "exit_reason":       r[13],
                "exit_reason_inferred": bool(r[14]),
                "strategy":          r[15],
                "claude_reasoning":  r[16],
                "market_regime":     r[17],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"get_position_history failed ({e})")
        return []


def get_trade_performance_summary() -> dict:
    """
    Aggregate stats across all closed trades in position_log.
    Returns win rate, avg P&L, best/worst symbols — fed to Claude as context.
    """
    try:
        conn = _get_conn()
        _empty = {
            "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate_pct": 0.0, "avg_pl_pct": 0.0,
            "avg_win_pct": 0.0, "avg_loss_pct": 0.0,
            "total_realized_pl": 0.0,
            "best_symbols": [], "worst_symbols": [],
        }
        if not conn:
            return _empty
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE realized_pl > 0) AS wins,
                    COUNT(*) FILTER (WHERE realized_pl < 0) AS losses,
                    ROUND(AVG(realized_pl_pct)::numeric, 2) AS avg_pl_pct,
                    ROUND(AVG(realized_pl_pct) FILTER (WHERE realized_pl > 0)::numeric, 2) AS avg_win_pct,
                    ROUND(AVG(realized_pl_pct) FILTER (WHERE realized_pl < 0)::numeric, 2) AS avg_loss_pct,
                    ROUND(SUM(realized_pl)::numeric, 2) AS total_realized_pl
                FROM position_log
                WHERE exit_time IS NOT NULL AND realized_pl IS NOT NULL
            """)
            row = cur.fetchone()
            if not row or not row[0]:
                return {
                    "total_trades": 0, "wins": 0, "losses": 0,
                    "win_rate_pct": 0.0, "avg_pl_pct": 0.0,
                    "avg_win_pct": 0.0, "avg_loss_pct": 0.0,
                    "total_realized_pl": 0.0,
                    "best_symbols": [], "worst_symbols": [],
                }
            total, wins, losses, avg_pct, avg_win, avg_loss, total_pl = row
            win_rate = round(wins / total * 100, 1) if total else 0

            # Best and worst symbols
            cur.execute("""
                SELECT symbol, ROUND(AVG(realized_pl_pct)::numeric, 2) AS avg_pct
                FROM position_log
                WHERE exit_time IS NOT NULL AND realized_pl_pct IS NOT NULL
                GROUP BY symbol HAVING COUNT(*) >= 1
                ORDER BY avg_pct DESC LIMIT 3
            """)
            best = [{"symbol": r[0], "avg_pct": float(r[1])} for r in cur.fetchall()]

            cur.execute("""
                SELECT symbol, ROUND(AVG(realized_pl_pct)::numeric, 2) AS avg_pct
                FROM position_log
                WHERE exit_time IS NOT NULL AND realized_pl_pct IS NOT NULL
                GROUP BY symbol HAVING COUNT(*) >= 1
                ORDER BY avg_pct ASC LIMIT 3
            """)
            worst = [{"symbol": r[0], "avg_pct": float(r[1])} for r in cur.fetchall()]

            return {
                "total_trades":    int(total),
                "wins":            int(wins),
                "losses":          int(losses),
                "win_rate_pct":    win_rate,
                "avg_pl_pct":      float(avg_pct or 0),
                "avg_win_pct":     float(avg_win or 0),
                "avg_loss_pct":    float(avg_loss or 0),
                "total_realized_pl": float(total_pl or 0),
                "best_symbols":    best,
                "worst_symbols":   worst,
            }
    except Exception as e:
        logger.warning(f"get_trade_performance_summary failed ({e})")
        return {
            "total_trades": 0, "wins": 0, "losses": 0,
            "win_rate_pct": 0.0, "avg_pl_pct": 0.0,
            "avg_win_pct": 0.0, "avg_loss_pct": 0.0,
            "total_realized_pl": 0.0,
            "best_symbols": [], "worst_symbols": [],
        }


def get_trade_metrics_report(days: int = 30) -> dict:
    """Return an expectancy-focused report over recent closed trades."""
    empty = {
        "period_days": days,
        "summary": {
            "trades": 0,
            "win_rate_pct": 0.0,
            "avg_winner_pct": 0.0,
            "avg_loser_pct": 0.0,
            "profit_factor": 0.0,
            "expectancy_pct": 0.0,
            "avg_hold_mins": 0.0,
            "total_realized_pl": 0.0,
        },
        "by_strategy": [],
        "by_symbol": [],
        "biggest_losers": [],
    }
    try:
        conn = _get_conn()
        if not conn:
            return empty
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS trades,
                    COUNT(*) FILTER (WHERE realized_pl > 0) AS wins,
                    ROUND(AVG(realized_pl_pct) FILTER (WHERE realized_pl > 0)::numeric, 2) AS avg_winner_pct,
                    ROUND(AVG(realized_pl_pct) FILTER (WHERE realized_pl < 0)::numeric, 2) AS avg_loser_pct,
                    ROUND(SUM(CASE WHEN realized_pl > 0 THEN realized_pl ELSE 0 END)::numeric, 2) AS gross_profit,
                    ROUND(ABS(SUM(CASE WHEN realized_pl < 0 THEN realized_pl ELSE 0 END))::numeric, 2) AS gross_loss,
                    ROUND(AVG(hold_duration_mins)::numeric, 2) AS avg_hold_mins,
                    ROUND(SUM(realized_pl)::numeric, 2) AS total_realized_pl
                FROM position_log
                WHERE exit_time IS NOT NULL
                  AND exit_time >= %s
                  AND realized_pl IS NOT NULL
            """, (cutoff,))
            row = cur.fetchone() or (0, 0, 0, 0, 0, 0, 0, 0)
            trades, wins, avg_winner, avg_loser, gross_profit, gross_loss, avg_hold, total_realized_pl = row

            trades = int(trades or 0)
            wins = int(wins or 0)
            win_rate_pct = round((wins / trades) * 100, 2) if trades else 0.0
            avg_winner = float(avg_winner or 0.0)
            avg_loser = float(avg_loser or 0.0)
            expectancy_pct = round(
                (win_rate_pct / 100.0 * avg_winner) - ((1 - win_rate_pct / 100.0) * abs(avg_loser)),
                2,
            ) if trades else 0.0
            profit_factor = round(float(gross_profit or 0.0) / float(gross_loss or 0.0), 2) if gross_loss else 0.0

            cur.execute("""
                SELECT
                    COALESCE(strategy, 'unknown') AS strategy_name,
                    COUNT(*) AS trades,
                    ROUND(AVG(realized_pl_pct)::numeric, 2) AS avg_pl_pct,
                    ROUND(SUM(realized_pl)::numeric, 2) AS total_pl
                FROM position_log
                WHERE exit_time IS NOT NULL
                  AND exit_time >= %s
                GROUP BY COALESCE(strategy, 'unknown')
                ORDER BY total_pl DESC, avg_pl_pct DESC
            """, (cutoff,))
            by_strategy = [
                {
                    "strategy": r[0],
                    "trades": int(r[1] or 0),
                    "avg_pl_pct": float(r[2] or 0.0),
                    "total_realized_pl": float(r[3] or 0.0),
                }
                for r in cur.fetchall()
            ]

            cur.execute("""
                SELECT
                    symbol,
                    COUNT(*) AS trades,
                    ROUND(AVG(realized_pl_pct)::numeric, 2) AS avg_pl_pct,
                    ROUND(SUM(realized_pl)::numeric, 2) AS total_pl
                FROM position_log
                WHERE exit_time IS NOT NULL
                  AND exit_time >= %s
                GROUP BY symbol
                ORDER BY total_pl DESC, avg_pl_pct DESC
                LIMIT 25
            """, (cutoff,))
            by_symbol = [
                {
                    "symbol": r[0],
                    "trades": int(r[1] or 0),
                    "avg_pl_pct": float(r[2] or 0.0),
                    "total_realized_pl": float(r[3] or 0.0),
                }
                for r in cur.fetchall()
            ]

            cur.execute("""
                SELECT
                    symbol, side, exit_time, realized_pl, realized_pl_pct, hold_duration_mins,
                    exit_reason, strategy, market_regime, entry_rsi, entry_macd_hist_pct, entry_score
                FROM position_log
                WHERE exit_time IS NOT NULL
                  AND exit_time >= %s
                ORDER BY realized_pl_pct ASC NULLS LAST
                LIMIT 20
            """, (cutoff,))
            biggest_losers = [
                {
                    "symbol": r[0],
                    "side": r[1] or "long",
                    "exit_time": r[2].isoformat() if r[2] else None,
                    "realized_pl": float(r[3] or 0.0),
                    "realized_pl_pct": float(r[4] or 0.0),
                    "hold_duration_mins": int(r[5] or 0),
                    "exit_reason": r[6] or "unknown",
                    "strategy": r[7] or "unknown",
                    "market_regime": r[8] or "unknown",
                    "entry_rsi": float(r[9]) if r[9] is not None else None,
                    "entry_macd_hist_pct": float(r[10]) if r[10] is not None else None,
                    "entry_score": float(r[11]) if r[11] is not None else None,
                }
                for r in cur.fetchall()
            ]

        return {
            "period_days": days,
            "summary": {
                "trades": trades,
                "win_rate_pct": win_rate_pct,
                "avg_winner_pct": avg_winner,
                "avg_loser_pct": avg_loser,
                "profit_factor": profit_factor,
                "expectancy_pct": expectancy_pct,
                "avg_hold_mins": float(avg_hold or 0.0),
                "total_realized_pl": float(total_realized_pl or 0.0),
            },
            "by_strategy": by_strategy,
            "by_symbol": by_symbol,
            "biggest_losers": biggest_losers,
        }
    except Exception as e:
        logger.warning(f"get_trade_metrics_report failed ({e})")
        return empty


def get_post_change_comparison(
    change_date: str | None = None,
    post_days: int = 7,
    baseline_days: int | None = None,
) -> dict:
    """
    Compare performance before vs after a change date.

    Uses:
    - daily_summary for portfolio-level return
    - position_log for closed-trade expectancy / realized P&L

    Default change_date is yesterday (UTC date), which matches the common
    "did yesterday's update help?" question.
    """
    baseline_days = baseline_days or post_days
    today = datetime.now(timezone.utc).date()
    try:
        pivot_date = datetime.fromisoformat(change_date).date() if change_date else (today - timedelta(days=1))
    except Exception:
        pivot_date = today - timedelta(days=1)

    empty_window = {
        "start_date": None,
        "end_date": None,
        "days_requested": 0,
        "equity_points": 0,
        "start_portfolio_value": None,
        "end_portfolio_value": None,
        "portfolio_return_pct": None,
        "portfolio_return_abs": None,
        "trades": 0,
        "win_rate_pct": 0.0,
        "avg_pl_pct": 0.0,
        "avg_winner_pct": 0.0,
        "avg_loser_pct": 0.0,
        "expectancy_pct": 0.0,
        "profit_factor": 0.0,
        "total_realized_pl": 0.0,
    }
    empty = {
        "change_date": pivot_date.isoformat(),
        "baseline_days": baseline_days,
        "post_days": post_days,
        "baseline": {**empty_window, "days_requested": baseline_days},
        "post": {**empty_window, "days_requested": post_days},
        "delta": {
            "portfolio_return_pct": None,
            "expectancy_pct": 0.0,
            "avg_pl_pct": 0.0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "total_realized_pl": 0.0,
            "trade_count": 0,
        },
        "assessment": {
            "status": "insufficient_data",
            "summary": "Not enough data yet to judge the recent change.",
        },
    }
    try:
        conn = _get_conn()
        if not conn:
            return empty

        baseline_start = pivot_date - timedelta(days=baseline_days)
        baseline_end = pivot_date
        post_start = pivot_date
        post_end = min(today + timedelta(days=1), pivot_date + timedelta(days=post_days))

        def _trade_window(start_date, end_date) -> dict:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COUNT(*) AS trades,
                        COUNT(*) FILTER (WHERE realized_pl > 0) AS wins,
                        ROUND(AVG(realized_pl_pct)::numeric, 2) AS avg_pl_pct,
                        ROUND(AVG(realized_pl_pct) FILTER (WHERE realized_pl > 0)::numeric, 2) AS avg_winner_pct,
                        ROUND(AVG(realized_pl_pct) FILTER (WHERE realized_pl < 0)::numeric, 2) AS avg_loser_pct,
                        ROUND(SUM(CASE WHEN realized_pl > 0 THEN realized_pl ELSE 0 END)::numeric, 2) AS gross_profit,
                        ROUND(ABS(SUM(CASE WHEN realized_pl < 0 THEN realized_pl ELSE 0 END))::numeric, 2) AS gross_loss,
                        ROUND(SUM(realized_pl)::numeric, 2) AS total_realized_pl
                    FROM position_log
                    WHERE exit_time IS NOT NULL
                      AND exit_time >= %s
                      AND exit_time < %s
                      AND realized_pl IS NOT NULL
                      AND realized_pl_pct IS NOT NULL
                """, (start_date, end_date))
                row = cur.fetchone() or (0, 0, 0, 0, 0, 0, 0, 0)
            trades, wins, avg_pl, avg_winner, avg_loser, gross_profit, gross_loss, total_realized_pl = row
            trades = int(trades or 0)
            wins = int(wins or 0)
            win_rate_pct = round((wins / trades) * 100, 2) if trades else 0.0
            avg_winner = float(avg_winner or 0.0)
            avg_loser = float(avg_loser or 0.0)
            expectancy_pct = round(
                (win_rate_pct / 100.0 * avg_winner) - ((1 - win_rate_pct / 100.0) * abs(avg_loser)),
                2,
            ) if trades else 0.0
            profit_factor = round(float(gross_profit or 0.0) / float(gross_loss or 0.0), 2) if gross_loss else 0.0
            return {
                "trades": trades,
                "win_rate_pct": win_rate_pct,
                "avg_pl_pct": float(avg_pl or 0.0),
                "avg_winner_pct": avg_winner,
                "avg_loser_pct": avg_loser,
                "expectancy_pct": expectancy_pct,
                "profit_factor": profit_factor,
                "total_realized_pl": float(total_realized_pl or 0.0),
            }

        def _equity_window(start_date, end_date) -> dict:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT date, portfolio_value
                    FROM daily_summary
                    WHERE date >= %s
                      AND date < %s
                    ORDER BY date ASC
                """, (start_date, end_date))
                rows = cur.fetchall()
            if not rows:
                return {
                    "equity_points": 0,
                    "start_portfolio_value": None,
                    "end_portfolio_value": None,
                    "portfolio_return_pct": None,
                    "portfolio_return_abs": None,
                }
            start_val = float(rows[0][1] or 0.0)
            end_val = float(rows[-1][1] or 0.0)
            portfolio_return_pct = round(((end_val - start_val) / start_val) * 100, 2) if start_val > 0 else None
            return {
                "equity_points": len(rows),
                "start_portfolio_value": start_val,
                "end_portfolio_value": end_val,
                "portfolio_return_pct": portfolio_return_pct,
                "portfolio_return_abs": round(end_val - start_val, 2),
            }

        baseline_trade = _trade_window(baseline_start, baseline_end)
        post_trade = _trade_window(post_start, post_end)
        baseline_equity = _equity_window(baseline_start, baseline_end)
        post_equity = _equity_window(post_start, post_end)

        # Include live portfolio value as the current post-window endpoint when useful.
        try:
            from services import alpaca_service
            account = alpaca_service.get_account()
            live_portfolio_value = float(account.portfolio_value)
            if post_equity["start_portfolio_value"] is not None and live_portfolio_value > 0:
                post_equity["end_portfolio_value"] = live_portfolio_value
                post_equity["portfolio_return_abs"] = round(
                    live_portfolio_value - float(post_equity["start_portfolio_value"]),
                    2,
                )
                start_val = float(post_equity["start_portfolio_value"])
                post_equity["portfolio_return_pct"] = round(
                    ((live_portfolio_value - start_val) / start_val) * 100,
                    2,
                ) if start_val > 0 else None
        except Exception:
            pass

        baseline = {
            **empty_window,
            **baseline_trade,
            **baseline_equity,
            "start_date": baseline_start.isoformat(),
            "end_date": (baseline_end - timedelta(days=1)).isoformat(),
            "days_requested": baseline_days,
        }
        post = {
            **empty_window,
            **post_trade,
            **post_equity,
            "start_date": post_start.isoformat(),
            "end_date": min(today, post_end - timedelta(days=1)).isoformat(),
            "days_requested": post_days,
        }

        delta = {
            "portfolio_return_pct": (
                round(float(post["portfolio_return_pct"]) - float(baseline["portfolio_return_pct"]), 2)
                if post["portfolio_return_pct"] is not None and baseline["portfolio_return_pct"] is not None
                else None
            ),
            "expectancy_pct": round(float(post["expectancy_pct"]) - float(baseline["expectancy_pct"]), 2),
            "avg_pl_pct": round(float(post["avg_pl_pct"]) - float(baseline["avg_pl_pct"]), 2),
            "win_rate_pct": round(float(post["win_rate_pct"]) - float(baseline["win_rate_pct"]), 2),
            "profit_factor": round(float(post["profit_factor"]) - float(baseline["profit_factor"]), 2),
            "total_realized_pl": round(float(post["total_realized_pl"]) - float(baseline["total_realized_pl"]), 2),
            "trade_count": int(post["trades"]) - int(baseline["trades"]),
        }

        status = "insufficient_data"
        summary = "Not enough data yet to judge the recent change."
        if post["trades"] >= 5 or post["equity_points"] >= 2:
            if (
                post["expectancy_pct"] > baseline["expectancy_pct"]
                or (delta["portfolio_return_pct"] is not None and delta["portfolio_return_pct"] > 0)
            ):
                status = "improving"
                summary = "Recent post-change performance looks better than the baseline so far."
            elif (
                post["expectancy_pct"] < baseline["expectancy_pct"]
                or (delta["portfolio_return_pct"] is not None and delta["portfolio_return_pct"] < 0)
            ):
                status = "worse"
                summary = "Recent post-change performance is currently weaker than the baseline."
            else:
                status = "mixed"
                summary = "Post-change performance is mixed so far."

        return {
            "change_date": pivot_date.isoformat(),
            "baseline_days": baseline_days,
            "post_days": post_days,
            "baseline": baseline,
            "post": post,
            "delta": delta,
            "assessment": {
                "status": status,
                "summary": summary,
            },
        }
    except Exception as e:
        logger.warning(f"get_post_change_comparison failed ({e})")
        return empty


def _bucket_rsi(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    if value < 30:
        return "<30"
    if value < 40:
        return "30-39"
    if value < 50:
        return "40-49"
    if value < 60:
        return "50-59"
    if value < 70:
        return "60-69"
    return "70+"


def _bucket_macd_hist_pct(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    if value <= -0.50:
        return "<=-0.50%"
    if value <= -0.20:
        return "-0.49% to -0.20%"
    if value < 0.0:
        return "-0.19% to -0.01%"
    if value < 0.20:
        return "0.00% to 0.19%"
    if value < 0.50:
        return "0.20% to 0.49%"
    return ">=0.50%"


def _bucket_entry_score(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    if value < 50:
        return "<50"
    if value < 60:
        return "50-59"
    if value < 70:
        return "60-69"
    if value < 80:
        return "70-79"
    return "80+"


def get_trade_diagnostics_report(days: int = 30, min_trades: int = 3) -> dict:
    """
    Diagnose what is driving wins and losses.
    Groups recent closed trades by regime, exit reason, entry score, RSI bucket,
    and MACD histogram bucket so tuning can focus on real edge.
    """
    empty = {
        "period_days": days,
        "min_trades": min_trades,
        "by_market_regime": [],
        "by_exit_reason": [],
        "by_entry_score_bucket": [],
        "by_entry_rsi_bucket": [],
        "by_entry_macd_hist_pct_bucket": [],
        "by_news_profile": [],
        "by_conviction_profile": [],
        "worst_clusters": [],
        "best_clusters": [],
    }
    try:
        conn = _get_conn()
        if not conn:
            return empty
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    symbol, side, realized_pl, realized_pl_pct, hold_duration_mins,
                    exit_reason, strategy, market_regime, entry_rsi, entry_macd_hist_pct, entry_score,
                    claude_reasoning
                FROM position_log
                WHERE exit_time IS NOT NULL
                  AND exit_time >= %s
                  AND realized_pl IS NOT NULL
                  AND realized_pl_pct IS NOT NULL
                ORDER BY exit_time DESC
            """, (cutoff,))
            rows = cur.fetchall()
        if not rows:
            return empty

        def summarize(name: str, grouped: dict[str, list[tuple]]) -> list[dict]:
            items = []
            for key, vals in grouped.items():
                if len(vals) < min_trades:
                    continue
                wins = sum(1 for v in vals if v[0] > 0)
                gross_profit = sum(v[0] for v in vals if v[0] > 0)
                gross_loss = abs(sum(v[0] for v in vals if v[0] < 0))
                avg_pct = sum(v[1] for v in vals) / len(vals)
                avg_hold = sum((v[2] or 0) for v in vals) / len(vals)
                avg_win = sum(v[1] for v in vals if v[1] > 0) / wins if wins else 0.0
                losses = len(vals) - wins
                avg_loss = sum(abs(v[1]) for v in vals if v[1] < 0) / losses if losses else 0.0
                expectancy = (wins / len(vals) * avg_win) - ((1 - wins / len(vals)) * avg_loss)
                items.append({
                    name: key,
                    "trades": len(vals),
                    "win_rate_pct": round(wins / len(vals) * 100, 2),
                    "avg_pl_pct": round(avg_pct, 2),
                    "avg_hold_mins": round(avg_hold, 2),
                    "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else 0.0,
                    "expectancy_pct": round(expectancy, 2),
                })
            return sorted(items, key=lambda x: (x["expectancy_pct"], x["avg_pl_pct"]), reverse=True)

        by_regime: dict[str, list[tuple]] = {}
        by_exit_reason: dict[str, list[tuple]] = {}
        by_score: dict[str, list[tuple]] = {}
        by_rsi: dict[str, list[tuple]] = {}
        by_macd: dict[str, list[tuple]] = {}
        by_news: dict[str, list[tuple]] = {}
        by_conviction: dict[str, list[tuple]] = {}
        clusters: dict[str, list[tuple]] = {}

        for symbol, side, realized_pl, realized_pl_pct, hold_mins, exit_reason, strategy, market_regime, entry_rsi, entry_macd_hist_pct, entry_score, claude_reasoning in rows:
            pnl = float(realized_pl or 0.0)
            pct = float(realized_pl_pct or 0.0)
            hold = int(hold_mins or 0)
            regime_key = market_regime or "unknown"
            exit_key = exit_reason or "unknown"
            score_key = _bucket_entry_score(float(entry_score) if entry_score is not None else None)
            rsi_key = _bucket_rsi(float(entry_rsi) if entry_rsi is not None else None)
            macd_key = _bucket_macd_hist_pct(float(entry_macd_hist_pct) if entry_macd_hist_pct is not None else None)
            reasoning = claude_reasoning or ""
            news_key = "news_event" if "NEWS_EVENT=" in reasoning else "non_news"
            conviction_key = "rocket" if "[ROCKET]" in reasoning else "standard"
            triple_key = f"{regime_key} | {score_key} | {exit_key}"
            triple_val = (pnl, pct, hold)

            by_regime.setdefault(regime_key, []).append(triple_val)
            by_exit_reason.setdefault(exit_key, []).append(triple_val)
            by_score.setdefault(score_key, []).append(triple_val)
            by_rsi.setdefault(rsi_key, []).append(triple_val)
            by_macd.setdefault(macd_key, []).append(triple_val)
            by_news.setdefault(news_key, []).append(triple_val)
            by_conviction.setdefault(conviction_key, []).append(triple_val)
            clusters.setdefault(triple_key, []).append(triple_val)

        cluster_stats = summarize("cluster", clusters)

        return {
            "period_days": days,
            "min_trades": min_trades,
            "by_market_regime": summarize("market_regime", by_regime),
            "by_exit_reason": summarize("exit_reason", by_exit_reason),
            "by_entry_score_bucket": summarize("entry_score_bucket", by_score),
            "by_entry_rsi_bucket": summarize("entry_rsi_bucket", by_rsi),
            "by_entry_macd_hist_pct_bucket": summarize("entry_macd_hist_pct_bucket", by_macd),
            "by_news_profile": summarize("news_profile", by_news),
            "by_conviction_profile": summarize("conviction_profile", by_conviction),
            "worst_clusters": sorted(cluster_stats, key=lambda x: (x["expectancy_pct"], x["avg_pl_pct"]))[:8],
            "best_clusters": cluster_stats[:8],
        }
    except Exception as e:
        logger.warning(f"get_trade_diagnostics_report failed ({e})")
        return empty


def _extract_setup_tag(reasoning: str, strategy: str = None, side: str = None) -> str:
    import re
    text = reasoning or ""
    match = re.search(r"\[STRATEGY:([A-Za-z0-9_\-]+)\]", text)
    return match.group(1) if match else (strategy or f"{side or 'trade'}_unknown")


def get_predictive_trade_priors(
    symbols: list[str],
    market_regime: str | None = None,
    days: int = 180,
    min_symbol_trades: int = 2,
    min_context_trades: int = 5,
) -> dict:
    """
    Build lightweight predictive priors from realized closed trades.
    Returns side-aware symbol expectancy plus broader setup/regime/news/conviction priors.
    """
    empty = {
        "symbol_side": {},
        "symbol_regime_side": {},
        "setup": {},
        "regime_side": {},
        "news_profile": {},
        "conviction_profile": {},
    }
    try:
        conn = _get_conn()
        if not conn or not symbols:
            return empty
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, side, realized_pl_pct, strategy, claude_reasoning, market_regime
                FROM position_log
                WHERE exit_time IS NOT NULL
                  AND exit_time >= %s
                  AND realized_pl_pct IS NOT NULL
            """, (cutoff,))
            rows = cur.fetchall()
        if not rows:
            return empty

        def _pack(values: list[float], min_trades: int) -> dict | None:
            if len(values) < min_trades:
                return None
            wins = sum(1 for v in values if v > 0)
            avg = sum(values) / len(values)
            avg_win = sum(v for v in values if v > 0) / wins if wins else 0.0
            losses = len(values) - wins
            avg_loss = sum(abs(v) for v in values if v < 0) / losses if losses else 0.0
            expectancy = (wins / len(values) * avg_win) - ((1 - wins / len(values)) * avg_loss)
            return {
                "trades": len(values),
                "win_rate_pct": round(wins / len(values) * 100, 2),
                "avg_pl_pct": round(avg, 2),
                "expectancy_pct": round(expectancy, 2),
            }

        symbol_side_vals: dict[str, list[float]] = {}
        symbol_regime_side_vals: dict[str, list[float]] = {}
        setup_vals: dict[str, list[float]] = {}
        regime_side_vals: dict[str, list[float]] = {}
        news_vals: dict[str, list[float]] = {}
        conviction_vals: dict[str, list[float]] = {}
        wanted = {s.upper() for s in symbols}

        for symbol, side, pl_pct, strategy, reasoning, regime in rows:
            sym = (symbol or "").upper()
            trade_side = (side or "long").lower()
            pct = float(pl_pct or 0.0)
            setup = _extract_setup_tag(reasoning, strategy=strategy, side=trade_side)
            news_key = "news_event" if "NEWS_EVENT=" in (reasoning or "") else "non_news"
            conviction_key = "rocket" if "[ROCKET]" in (reasoning or "") else "standard"
            regime_key = regime or "unknown"

            setup_vals.setdefault(setup, []).append(pct)
            regime_side_vals.setdefault(f"{regime_key}|{trade_side}", []).append(pct)
            news_vals.setdefault(news_key, []).append(pct)
            conviction_vals.setdefault(conviction_key, []).append(pct)

            if sym in wanted:
                symbol_side_vals.setdefault(f"{sym}|{trade_side}", []).append(pct)
                symbol_regime_side_vals.setdefault(f"{sym}|{regime_key}|{trade_side}", []).append(pct)

        result = {
            "symbol_side": {},
            "symbol_regime_side": {},
            "setup": {},
            "regime_side": {},
            "news_profile": {},
            "conviction_profile": {},
        }
        for key, vals in symbol_side_vals.items():
            packed = _pack(vals, min_symbol_trades)
            if packed:
                result["symbol_side"][key] = packed
        for key, vals in symbol_regime_side_vals.items():
            packed = _pack(vals, min_symbol_trades)
            if packed:
                result["symbol_regime_side"][key] = packed
        for key, vals in setup_vals.items():
            packed = _pack(vals, min_context_trades)
            if packed:
                result["setup"][key] = packed
        for key, vals in regime_side_vals.items():
            packed = _pack(vals, min_context_trades)
            if packed:
                result["regime_side"][key] = packed
        for key, vals in news_vals.items():
            packed = _pack(vals, min_context_trades)
            if packed:
                result["news_profile"][key] = packed
        for key, vals in conviction_vals.items():
            packed = _pack(vals, min_context_trades)
            if packed:
                result["conviction_profile"][key] = packed
        return result
    except Exception as e:
        logger.warning(f"get_predictive_trade_priors failed ({e})")
        return empty


def get_trade_learning_summary(limit: int = 80) -> dict:
    """
    Summarize recent closed-trade outcomes by setup tag and market regime.
    Setup tags are parsed from reasoning strings like [STRATEGY:long_breakout].
    """
    empty = {
        "total_closed": 0,
        "best_setups": [],
        "worst_setups": [],
        "best_regimes": [],
        "worst_regimes": [],
        "lessons": [],
    }
    try:
        conn = _get_conn()
        if not conn:
            return empty
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, side, realized_pl_pct, strategy,
                       claude_reasoning, market_regime
                FROM position_log
                WHERE exit_time IS NOT NULL AND realized_pl_pct IS NOT NULL
                ORDER BY exit_time DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
        if not rows:
            return empty

        by_setup: dict[str, list[float]] = {}
        by_regime: dict[str, list[float]] = {}
        for symbol, side, pl_pct, strategy, reasoning, regime in rows:
            setup = _extract_setup_tag(reasoning, strategy=strategy, side=side)
            by_setup.setdefault(setup, []).append(float(pl_pct or 0))
            by_regime.setdefault(regime or "unknown", []).append(float(pl_pct or 0))

        def summarize(grouped: dict[str, list[float]]) -> list[dict]:
            items = []
            for key, vals in grouped.items():
                if len(vals) < 2:
                    continue
                wins = sum(1 for v in vals if v > 0)
                items.append({
                    "name": key,
                    "trades": len(vals),
                    "win_rate_pct": round(wins / len(vals) * 100, 1),
                    "avg_pl_pct": round(sum(vals) / len(vals), 2),
                })
            return items

        setup_stats = summarize(by_setup)
        regime_stats = summarize(by_regime)
        best_setups = sorted(setup_stats, key=lambda x: (x["avg_pl_pct"], x["win_rate_pct"]), reverse=True)[:3]
        worst_setups = sorted(setup_stats, key=lambda x: (x["avg_pl_pct"], x["win_rate_pct"]))[:3]
        best_regimes = sorted(regime_stats, key=lambda x: x["avg_pl_pct"], reverse=True)[:2]
        worst_regimes = sorted(regime_stats, key=lambda x: x["avg_pl_pct"])[:2]

        lessons = []
        for setup in worst_setups:
            if setup["avg_pl_pct"] < 0:
                lessons.append(
                    f"Be more selective with {setup['name']} "
                    f"({setup['trades']} trades, {setup['win_rate_pct']}% win, avg {setup['avg_pl_pct']}%)."
                )
        for setup in best_setups:
            if setup["avg_pl_pct"] > 0:
                lessons.append(
                    f"Favor {setup['name']} when current signals confirm "
                    f"({setup['trades']} trades, {setup['win_rate_pct']}% win, avg +{setup['avg_pl_pct']})."
                )

        return {
            "total_closed": len(rows),
            "best_setups": best_setups,
            "worst_setups": worst_setups,
            "best_regimes": best_regimes,
            "worst_regimes": worst_regimes,
            "lessons": lessons[:6],
        }
    except Exception as e:
        logger.warning(f"get_trade_learning_summary failed ({e})")
        return empty


# ── circuit_breaker_log helpers ────────────────────────────────────────────


def log_circuit_breaker(day_pl_percent: float, portfolio_value: float,
                        limit_pct: float) -> None:
    """Record a circuit breaker trigger. Never raises."""
    try:
        conn = _get_conn()
        if not conn:
            return
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO circuit_breaker_log
                    (triggered_at, day_pl_percent, portfolio_value, limit_pct)
                VALUES (%s, %s, %s, %s)
            """, (datetime.now(timezone.utc), day_pl_percent,
                  portfolio_value, limit_pct))
        logger.info(f"log_circuit_breaker: day_pl={day_pl_percent:.2f}% portfolio=${portfolio_value:,.2f}")
    except Exception as e:
        logger.warning(f"log_circuit_breaker failed ({e})")


# ── bot_activity_log helpers ───────────────────────────────────────────────


def log_bot_activity(event_type: str, message: str,
                     symbol: str = None, cycle_id: str = None) -> None:
    """
    Log a single bot activity event. Never raises.
    event_type: 'scan', 'approved', 'rejected', 'earnings_block',
                'circuit_breaker', 'trailing_stop', 'scale_out', 'entry_rejected'
    """
    try:
        conn = _get_conn()
        if not conn:
            return
        with conn.cursor() as cur:
            if event_type == "entry_rejected" and symbol and message:
                normalized_message = re.sub(r"-?\d+(?:\.\d+)?%?", "#", message)
                cur.execute("""
                    SELECT message
                    FROM bot_activity_log
                    WHERE event_type = %s
                      AND symbol = %s
                      AND timestamp >= %s
                    ORDER BY timestamp DESC
                    LIMIT 8
                """, (
                    event_type,
                    symbol,
                    datetime.now(timezone.utc) - timedelta(minutes=20),
                ))
                for row in cur.fetchall():
                    prev_message = row[0] or ""
                    if prev_message == message:
                        return
                    if re.sub(r"-?\d+(?:\.\d+)?%?", "#", prev_message) == normalized_message:
                        return
            cur.execute("""
                INSERT INTO bot_activity_log (timestamp, cycle_id, event_type, symbol, message)
                VALUES (%s, %s, %s, %s, %s)
            """, (datetime.now(timezone.utc), cycle_id, event_type, symbol, message))
    except Exception as e:
        logger.warning(f"log_bot_activity failed ({e})")


def get_bot_activity_log(limit: int = 50) -> list[dict]:
    """Return recent bot activity events, newest first."""
    try:
        conn = _get_conn()
        if not conn:
            return []
        with conn.cursor() as cur:
            cur.execute("""
                SELECT timestamp, cycle_id, event_type, symbol, message
                FROM bot_activity_log
                ORDER BY timestamp DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
        return [
            {
                "timestamp":  r[0].isoformat() if r[0] else None,
                "cycle_id":   r[1],
                "event_type": r[2],
                "symbol":     r[3],
                "message":    r[4],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"get_bot_activity_log failed ({e})")
        return []


def get_rejection_summary(hours: int = 24, limit: int = 200) -> dict:
    """Summarize recent rejection patterns so prompts can avoid wasted candidates."""
    empty = {"total": 0, "categories": [], "symbols": [], "lessons": []}
    try:
        conn = _get_conn()
        if not conn:
            return empty
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, message
                FROM bot_activity_log
                WHERE timestamp >= %s
                  AND event_type IN ('entry_rejected', 'earnings_block', 'trade_cap', 'circuit_breaker')
                ORDER BY timestamp DESC
                LIMIT %s
            """, (cutoff, limit))
            rows = cur.fetchall()
        if not rows:
            return empty

        def category(message: str) -> str:
            msg = (message or "").lower()
            if "rsi" in msg:
                return "rsi"
            if "macd" in msg or "momentum" in msg:
                return "momentum"
            if "volume" in msg:
                return "volume"
            if "earnings" in msg or "fda" in msg:
                return "binary_event"
            if "cash" in msg or "buying power" in msg or "afford" in msg:
                return "cash"
            if "cap" in msg or "concentration" in msg or "correlated" in msg:
                return "risk_cap"
            if "fomc" in msg or "opening window" in msg:
                return "timing"
            if "circuit" in msg:
                return "circuit_breaker"
            return "other"

        cat_counts: dict[str, int] = {}
        sym_counts: dict[str, int] = {}
        for sym, msg in rows:
            cat = category(msg)
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
            if sym:
                sym_counts[sym] = sym_counts.get(sym, 0) + 1

        categories = [
            {"category": key, "count": count}
            for key, count in sorted(cat_counts.items(), key=lambda x: x[1], reverse=True)
        ]
        symbols = [
            {"symbol": key, "count": count}
            for key, count in sorted(sym_counts.items(), key=lambda x: x[1], reverse=True)[:8]
        ]
        lessons = []
        for item in categories[:4]:
            cat = item["category"]
            count = item["count"]
            if cat == "rsi":
                lessons.append(f"{count} recent RSI rejections: avoid overextended entries unless catalyst is exceptional.")
            elif cat == "momentum":
                lessons.append(f"{count} recent momentum/MACD rejections: require clearer trend confirmation.")
            elif cat == "volume":
                lessons.append(f"{count} recent volume rejections: prioritize higher relative volume names.")
            elif cat == "binary_event":
                lessons.append(f"{count} recent earnings/FDA blocks: only take binary-event plays with clear directional evidence.")
            elif cat == "cash":
                lessons.append(f"{count} recent cash/buying-power blocks: prefer affordable share counts and rotations.")
            elif cat == "risk_cap":
                lessons.append(f"{count} recent cap/correlation blocks: diversify before adding similar exposure.")
            elif cat == "timing":
                lessons.append(f"{count} recent timing blocks: avoid new-entry candidates during blocked windows.")

        return {
            "total": len(rows),
            "categories": categories,
            "symbols": symbols,
            "lessons": lessons,
        }
    except Exception as e:
        logger.warning(f"get_rejection_summary failed ({e})")
        return empty


def cleanup_old_bot_activity(days: int = 30) -> None:
    """Prune bot_activity_log older than *days* days. Never raises."""
    try:
        conn = _get_conn()
        if not conn:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bot_activity_log WHERE timestamp < %s", (cutoff,))
        logger.debug(f"cleanup_old_bot_activity: pruned entries before {cutoff.date()}")
    except Exception as e:
        logger.warning(f"cleanup_old_bot_activity failed ({e})")


def get_setting(key: str) -> Optional[Any]:
    """
    Return a persistent app setting by key, or None if not set.
    Reads from app_settings table — no TTL, never expires.
    Falls back to in-memory dict if DB is unavailable.
    """
    conn = _get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
                row = cur.fetchone()
                if row:
                    return row[0]  # psycopg2 returns JSONB as Python object
        except Exception as e:
            logger.warning(f"get_setting({key}) pg error: {e}")
    # In-memory fallback
    return _memory_cache.get(f"setting:{key}", (None, None))[0]


def set_setting(key: str, value: Any) -> None:
    """
    Persist an app setting. Pass value=None to delete the setting.
    Writes to app_settings table — no TTL, survives restarts indefinitely.
    """
    now = datetime.now(timezone.utc)
    # Always keep in-memory copy
    _memory_cache[f"setting:{key}"] = (value, datetime.max.replace(tzinfo=timezone.utc))

    conn = _get_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                if value is None:
                    cur.execute("DELETE FROM app_settings WHERE key = %s", (key,))
                else:
                    cur.execute("""
                        INSERT INTO app_settings (key, value, updated_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (key) DO UPDATE
                            SET value = EXCLUDED.value,
                                updated_at = EXCLUDED.updated_at
                    """, (key, json.dumps(value), now))
            logger.debug(f"set_setting: {key} = {value}")
        except Exception as e:
            logger.warning(f"set_setting({key}) pg error: {e}")


def get_trading_budget() -> Optional[float]:
    """
    Return the active trading budget cap, or None if not set (use full portfolio).
    A budget of e.g. 2000.0 means the bot sizes as if portfolio = $2,000,
    leaving the rest of the account untouched.
    """
    val = get_setting("trading_budget")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def set_trading_budget(amount: Optional[float]) -> None:
    """
    Set the trading budget cap. Pass None or 0 to clear (use full portfolio).
    Persisted in app_settings — survives restarts indefinitely.
    """
    if amount and amount > 0:
        set_setting("trading_budget", amount)
        logger.info(f"Trading budget set to ${amount:,.2f}")
    else:
        set_setting("trading_budget", None)
        logger.info("Trading budget cleared — using full portfolio value")


def cleanup_expired_cache() -> None:
    """
    Delete expired rows from ai_cache to keep the table lean.
    Safe to call at any time — never raises.
    """
    try:
        conn = _get_conn()
        if not conn:
            return
        now = datetime.now(timezone.utc)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_cache WHERE expires_at < %s", (now,))
            deleted = cur.rowcount
        if deleted:
            logger.debug(f"cleanup_expired_cache: removed {deleted} expired entries")
    except Exception as e:
        logger.warning(f"cleanup_expired_cache failed ({e}), skipping.")
