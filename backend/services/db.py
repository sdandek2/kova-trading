"""
db.py — PostgreSQL-backed cache for Kova.

Provides a simple key/value cache with TTL stored in Postgres.
Falls back silently to in-memory if DATABASE_URL is not set or connection fails,
so local development without Postgres still works fine.
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

# In-memory fallback (used when Postgres is unavailable)
_memory_cache: dict[str, tuple[Any, datetime]] = {}

_conn = None  # module-level connection (lazy)


def _get_conn():
    """Return a live psycopg2 connection, or None if DB is unavailable."""
    global _conn
    from config import settings

    if not settings.database_url:
        return None

    # Re-use existing connection if still open
    try:
        if _conn and not _conn.closed:
            _conn.cursor().execute("SELECT 1")
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
                entry_time           TIMESTAMPTZ,
                exit_time            TIMESTAMPTZ,
                entry_price          FLOAT,
                exit_price           FLOAT,
                quantity             INTEGER,
                realized_pl          FLOAT,
                realized_pl_pct      FLOAT,
                hold_duration_mins   INTEGER,
                exit_reason          TEXT,
                strategy             TEXT,
                claude_reasoning     TEXT,
                market_regime        TEXT
            )
        """)
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
            # Create table with full schema
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
            # Add new columns if they don't exist yet (safe on existing tables)
            for col_sql in [
                "ALTER TABLE trade_log ADD COLUMN IF NOT EXISTS take_profit_pct FLOAT",
                "ALTER TABLE trade_log ADD COLUMN IF NOT EXISTS stop_loss_pct FLOAT",
                "ALTER TABLE trade_log ADD COLUMN IF NOT EXISTS partial_exit BOOLEAN DEFAULT FALSE",
            ]:
                cur.execute(col_sql)

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
                      market_regime: str = None) -> Optional[int]:
    """
    Record that a new position was opened.
    Returns the row id so the caller can update it on close, or None on failure.
    """
    try:
        conn = _get_conn()
        if not conn:
            return None
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO position_log
                    (symbol, entry_time, entry_price, quantity, strategy,
                     claude_reasoning, market_regime)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (symbol, datetime.now(timezone.utc), entry_price, quantity,
                  strategy, claude_reasoning, market_regime))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.warning(f"log_position_open failed ({e})")
        return None


def log_position_close(symbol: str, exit_price: float, exit_reason: str,
                       entry_price: float = None, quantity: int = None,
                       entry_time: datetime = None) -> None:
    """
    Update the most recent open position_log row for *symbol* with exit data.
    Also handles the case where no open row exists (logs a standalone closed row).
    Never raises.
    """
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
                ORDER BY entry_time DESC NULLS LAST
                LIMIT 1
            """, (symbol,))
            row = cur.fetchone()

            if row:
                pos_id, ep, qty, et = row
                ep = ep or entry_price or 0
                qty = qty or quantity or 0
                et = et or entry_time or now
                realized_pl = (exit_price - ep) * qty if ep and qty else None
                realized_pl_pct = ((exit_price - ep) / ep * 100) if ep else None
                hold_mins = int((now - et).total_seconds() / 60) if et else None
                cur.execute("""
                    UPDATE position_log SET
                        exit_time         = %s,
                        exit_price        = %s,
                        realized_pl       = %s,
                        realized_pl_pct   = %s,
                        hold_duration_mins = %s,
                        exit_reason       = %s
                    WHERE id = %s
                """, (now, exit_price, realized_pl, realized_pl_pct,
                      hold_mins, exit_reason, pos_id))
            else:
                # No open row — insert a closed record directly
                ep = entry_price or 0
                qty = quantity or 0
                realized_pl = (exit_price - ep) * qty if ep else None
                realized_pl_pct = ((exit_price - ep) / ep * 100) if ep else None
                cur.execute("""
                    INSERT INTO position_log
                        (symbol, entry_time, exit_time, entry_price, exit_price,
                         quantity, realized_pl, realized_pl_pct, exit_reason)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (symbol, entry_time or now, now, ep, exit_price,
                      qty, realized_pl, realized_pl_pct, exit_reason))
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
                SELECT symbol, entry_time, exit_time, entry_price, exit_price,
                       quantity, realized_pl, realized_pl_pct, hold_duration_mins,
                       exit_reason, strategy, claude_reasoning, market_regime
                FROM position_log
                WHERE exit_time IS NOT NULL
                ORDER BY exit_time DESC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
        return [
            {
                "symbol":            r[0],
                "entry_time":        r[1].isoformat() if r[1] else None,
                "exit_time":         r[2].isoformat() if r[2] else None,
                "entry_price":       r[3],
                "exit_price":        r[4],
                "quantity":          r[5],
                "realized_pl":       r[6],
                "realized_pl_pct":   r[7],
                "hold_duration_mins": r[8],
                "exit_reason":       r[9],
                "strategy":          r[10],
                "claude_reasoning":  r[11],
                "market_regime":     r[12],
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
        if not conn:
            return {}
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
                return {}
            total, wins, losses, avg_pct, avg_win, avg_loss, total_pl = row
            win_rate = round(wins / total * 100, 1) if total else 0

            # Best and worst symbols
            cur.execute("""
                SELECT symbol, ROUND(AVG(realized_pl_pct)::numeric, 2) AS avg_pct
                FROM position_log
                WHERE exit_time IS NOT NULL AND realized_pl_pct IS NOT NULL
                GROUP BY symbol HAVING COUNT(*) >= 2
                ORDER BY avg_pct DESC LIMIT 3
            """)
            best = [{"symbol": r[0], "avg_pct": float(r[1])} for r in cur.fetchall()]

            cur.execute("""
                SELECT symbol, ROUND(AVG(realized_pl_pct)::numeric, 2) AS avg_pct
                FROM position_log
                WHERE exit_time IS NOT NULL AND realized_pl_pct IS NOT NULL
                GROUP BY symbol HAVING COUNT(*) >= 2
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
        return {}


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
