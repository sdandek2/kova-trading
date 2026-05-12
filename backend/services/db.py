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
