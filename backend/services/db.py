"""
db.py — PostgreSQL-backed cache for Kova.

Provides a simple key/value cache with TTL stored in Postgres.
Falls back silently to in-memory if DATABASE_URL is not set or connection fails,
so local development without Postgres still works fine.
"""

import json
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Test mode flag ─────────────────────────────────────────────────────────────
# Set to True by test scripts to prevent any writes to the real DB.
# Reads still work normally. Usage: import services.db as db; db.TEST_MODE = True
TEST_MODE: bool = False

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
                realized_pl          FLOAT,
                realized_pl_pct      FLOAT,
                hold_duration_mins   INTEGER,
                exit_reason          TEXT,
                strategy             TEXT,
                claude_reasoning     TEXT,
                market_regime        TEXT
            )
        """)
        # Migrations: add columns to existing tables
        cur.execute("""
            ALTER TABLE position_log
            ADD COLUMN IF NOT EXISTS side VARCHAR(10) DEFAULT 'long'
        """)
        cur.execute("""
            ALTER TABLE position_log
            ADD COLUMN IF NOT EXISTS setup_type TEXT
        """)
        cur.execute("""
            ALTER TABLE position_log
            ADD COLUMN IF NOT EXISTS entry_hour_et INTEGER
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
        # ── NEW: blocked_trades ────────────────────────────────────────────
        # Tracks every blocked trade + where the stock went afterwards.
        # Quantifies opportunity cost of each protection rule.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS blocked_trades (
                id               SERIAL PRIMARY KEY,
                timestamp        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                cycle_id         TEXT,
                symbol           TEXT NOT NULL,
                block_reason     TEXT NOT NULL,
                intended_side    TEXT DEFAULT 'buy',
                signal_score     FLOAT,
                price_at_block   FLOAT,
                price_15m        FLOAT,
                price_1h         FLOAT,
                price_eod        FLOAT,
                price_next_day   FLOAT,
                hypothetical_pnl_pct FLOAT
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
        # ── NEW: trade_slippage_log ────────────────────────────────────────
        # Records slippage (actual fill vs limit price) per trade.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trade_slippage_log (
                id              SERIAL PRIMARY KEY,
                timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                symbol          TEXT NOT NULL,
                side            TEXT NOT NULL,
                limit_price     FLOAT NOT NULL,
                fill_price      FLOAT NOT NULL,
                slippage_dollars FLOAT NOT NULL,
                slippage_pct    FLOAT NOT NULL,
                quantity        INTEGER
            )
        """)
        # ── Wheel Bot: wheel_universe ──────────────────────────────────────
        # AI-discovered stocks approved for wheel trading.
        # Refreshed weekly. No hardcoded watchlist.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wheel_universe (
                id              SERIAL PRIMARY KEY,
                symbol          TEXT NOT NULL UNIQUE,
                score           FLOAT DEFAULT 50,
                ai_reason       TEXT,
                iv_profile      TEXT,
                price_range     TEXT,
                added_by        TEXT DEFAULT 'ai',
                active          BOOLEAN DEFAULT TRUE,
                added_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_refreshed  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        # ── Wheel Bot: wheel_symbol_stats ───────────────────────────────────
        # Per-symbol performance stats — drives self-improvement.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wheel_symbol_stats (
                id              SERIAL PRIMARY KEY,
                symbol          TEXT NOT NULL UNIQUE,
                total_cycles    INTEGER DEFAULT 0,
                winning_cycles  INTEGER DEFAULT 0,
                win_rate        FLOAT DEFAULT 0,
                avg_premium_yield FLOAT DEFAULT 0,
                avg_realized_pl FLOAT DEFAULT 0,
                total_premium   FLOAT DEFAULT 0,
                last_updated    TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        # ── Wheel Bot: wheel_positions ──────────────────────────────────────
        # Tracks each active wheel cycle: put → (assignment) → covered call → repeat.
        # Completely separate from Kova trading. strategy tag = 'wheel'.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wheel_positions (
                id                  SERIAL PRIMARY KEY,
                symbol              TEXT NOT NULL,
                phase               TEXT NOT NULL DEFAULT 'scanning',
                put_contract        TEXT,
                put_strike          FLOAT,
                put_expiry          DATE,
                put_premium         FLOAT,
                put_order_id        TEXT,
                shares_qty          INTEGER DEFAULT 100,
                cost_basis          FLOAT,
                call_contract       TEXT,
                call_strike         FLOAT,
                call_expiry         DATE,
                call_premium        FLOAT,
                call_order_id       TEXT,
                total_premium_collected FLOAT DEFAULT 0,
                regime_at_open      TEXT,
                opened_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                closed_at           TIMESTAMPTZ,
                realized_pl         FLOAT,
                status              TEXT NOT NULL DEFAULT 'active',
                notes               TEXT
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
    if TEST_MODE:
        return
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
    if TEST_MODE:
        return
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
    if TEST_MODE:
        return
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
    if TEST_MODE:
        return
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
                      setup_type: str = None, entry_hour_et: int = None) -> Optional[int]:
    """
    Record that a new position was opened.
    side: "long" | "short"
    setup_type: "momentum_breakout" | "mean_reversion" | "event_driven" | "extended_hours"
    entry_hour_et: hour of entry in ET (9-15) for time-of-day analysis
    Returns the row id so the caller can update it on close, or None on failure.
    """
    if TEST_MODE:
        return
    try:
        conn = _get_conn()
        if not conn:
            return None
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO position_log
                    (symbol, side, entry_time, entry_price, quantity, strategy,
                     claude_reasoning, market_regime, setup_type, entry_hour_et)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (symbol, side, datetime.now(timezone.utc), entry_price, quantity,
                  strategy, claude_reasoning, market_regime, setup_type, entry_hour_et))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.warning(f"log_position_open failed ({e})")
        return None


def log_position_close(symbol: str, exit_price: float, exit_reason: str,
                       entry_price: float = None, quantity: int = None,
                       entry_time: datetime = None, side: str = "long") -> None:
    """
    Update the most recent open position_log row for *symbol* with exit data.
    Also handles the case where no open row exists (logs a standalone closed row).
    side: "long" (profit = exit > entry) | "short" (profit = entry > exit)
    Never raises.
    """
    if TEST_MODE:
        return
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
                        exit_reason       = %s
                    WHERE id = %s
                """, (now, exit_price, realized_pl, realized_pl_pct,
                      hold_mins, exit_reason, pos_id))
            else:
                # No open row — insert a closed record directly
                ep = entry_price or 0
                qty = quantity or 0
                realized_pl, realized_pl_pct = _calc_pl(ep, exit_price, qty, side)
                cur.execute("""
                    INSERT INTO position_log
                        (symbol, side, entry_time, exit_time, entry_price, exit_price,
                         quantity, realized_pl, realized_pl_pct, exit_reason)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (symbol, side or "long", entry_time or now, now, ep, exit_price,
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
                SELECT symbol, side, entry_time, exit_time, entry_price, exit_price,
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
                "side":              r[1] or "long",
                "entry_time":        r[2].isoformat() if r[2] else None,
                "exit_time":         r[3].isoformat() if r[3] else None,
                "entry_price":       r[4],
                "exit_price":        r[5],
                "quantity":          r[6],
                "realized_pl":       r[7],
                "realized_pl_pct":   r[8],
                "hold_duration_mins": r[9],
                "exit_reason":       r[10],
                "strategy":          r[11],
                "claude_reasoning":  r[12],
                "market_regime":     r[13],
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


# ── circuit_breaker_log helpers ────────────────────────────────────────────


def log_circuit_breaker(day_pl_percent: float, portfolio_value: float,
                        limit_pct: float) -> None:
    """Record a circuit breaker trigger. Never raises."""
    if TEST_MODE:
        return
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


def log_blocked_trade(symbol: str, block_reason: str, signal_score: float,
                      price_at_block: float = 0.0, cycle_id: str = None,
                      intended_side: str = "buy") -> int:
    """
    Log a blocked trade to blocked_trades table. Returns the row id for price followup.
    block_reason: 'circuit_breaker' | 'entry_rejected' | 'fomc' | 'concentration' |
                  'trade_cap' | 'earnings_block' | 'premarket_no_news' |
                  'afterhours_no_earnings' | 'window_closed'
    """
    if TEST_MODE:
        return
    row_id = None
    try:
        conn = _get_conn()
        if conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO blocked_trades
                        (cycle_id, symbol, block_reason, intended_side, signal_score, price_at_block)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (cycle_id, symbol, block_reason, intended_side, signal_score, price_at_block))
                row_id = cur.fetchone()[0]
        msg = f"BLOCKED [{block_reason}] {symbol} signal={signal_score:.0f} price=${price_at_block:.2f}"
        log_bot_activity("blocked_trade", msg, symbol=symbol, cycle_id=cycle_id)
    except Exception as e:
        logger.warning(f"log_blocked_trade failed ({e})")
    return row_id or 0


def update_blocked_trade_prices(row_id: int, field: str, price: float) -> None:
    """Update a single price field on a blocked_trades row. field: price_15m|price_1h|price_eod|price_next_day"""
    if TEST_MODE:
        return
    if not row_id:
        return
    try:
        conn = _get_conn()
        if not conn:
            return
        allowed = {"price_15m", "price_1h", "price_eod", "price_next_day"}
        if field not in allowed:
            return
        with conn.cursor() as cur:
            cur.execute(f"UPDATE blocked_trades SET {field} = %s WHERE id = %s", (price, row_id))
            # Compute hypothetical P&L once all four prices are filled
            cur.execute("""
                UPDATE blocked_trades SET
                    hypothetical_pnl_pct = CASE
                        WHEN price_at_block > 0 AND price_eod IS NOT NULL
                        THEN (price_eod - price_at_block) / price_at_block * 100
                        ELSE NULL END
                WHERE id = %s
            """, (row_id,))
    except Exception as e:
        logger.warning(f"update_blocked_trade_prices failed ({e})")


def get_blocked_trades_report() -> list[dict]:
    """Opportunity cost report: which block reason suppressed the most profit."""
    try:
        conn = _get_conn()
        if not conn:
            return []
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    block_reason,
                    COUNT(*)                              AS times_blocked,
                    AVG(signal_score)                     AS avg_signal_score,
                    AVG(hypothetical_pnl_pct)             AS avg_hypothetical_pnl,
                    SUM(hypothetical_pnl_pct)             AS total_hypothetical_pnl,
                    COUNT(*) FILTER (WHERE hypothetical_pnl_pct > 0) AS would_have_won,
                    COUNT(*) FILTER (WHERE hypothetical_pnl_pct < 0) AS would_have_lost
                FROM blocked_trades
                WHERE hypothetical_pnl_pct IS NOT NULL
                GROUP BY block_reason
                ORDER BY total_hypothetical_pnl DESC
            """)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.warning(f"get_blocked_trades_report failed ({e})")
        return []


def get_long_short_scorecard() -> dict:
    """Win rate, avg winner/loser, profit factor, avg hold time — split by long vs short."""
    try:
        conn = _get_conn()
        if not conn:
            return {}
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    side,
                    COUNT(*)                                                        AS trades,
                    ROUND(AVG(CASE WHEN realized_pl > 0 THEN 1.0 ELSE 0 END)::numeric, 3)
                                                                                    AS win_rate,
                    ROUND(AVG(CASE WHEN realized_pl > 0 THEN realized_pl_pct END)::numeric, 2)
                                                                                    AS avg_winner_pct,
                    ROUND(AVG(CASE WHEN realized_pl < 0 THEN realized_pl_pct END)::numeric, 2)
                                                                                    AS avg_loser_pct,
                    ROUND((
                        SUM(CASE WHEN realized_pl > 0 THEN realized_pl ELSE 0 END) /
                        NULLIF(ABS(SUM(CASE WHEN realized_pl < 0 THEN realized_pl ELSE 0 END)), 0)
                    )::numeric, 2)                                                  AS profit_factor,
                    ROUND(AVG(hold_duration_mins)::numeric, 0)                     AS avg_hold_mins
                FROM position_log
                WHERE exit_time IS NOT NULL
                GROUP BY side
            """)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            return {row[0]: dict(zip(cols[1:], row[1:])) for row in rows}
    except Exception as e:
        logger.warning(f"get_long_short_scorecard failed ({e})")
        return {}


def log_bot_activity(event_type: str, message: str,
                     symbol: str = None, cycle_id: str = None) -> None:
    """
    Log a single bot activity event. Never raises.
    event_type: 'scan', 'approved', 'rejected', 'earnings_block',
                'circuit_breaker', 'trailing_stop', 'scale_out', 'entry_rejected',
                'blocked_trade'
    """
    if TEST_MODE:
        return
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


def log_slippage(symbol: str, side: str, limit_price: float, fill_price: float,
                 quantity: int = None) -> None:
    """
    Record slippage for a completed fill.
    slippage_dollars = fill_price - limit_price (positive = paid more than intended)
    slippage_pct = slippage_dollars / limit_price
    """
    if TEST_MODE:
        return
    try:
        conn = _get_conn()
        if not conn:
            return
        slippage_dollars = round(fill_price - limit_price, 4)
        slippage_pct = round(slippage_dollars / limit_price, 6) if limit_price else 0.0
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trade_slippage_log
                    (symbol, side, limit_price, fill_price, slippage_dollars, slippage_pct, quantity)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (symbol, side, limit_price, fill_price, slippage_dollars, slippage_pct, quantity))
    except Exception as e:
        logger.warning(f"log_slippage failed ({e})")


def get_portfolio_var() -> dict:
    """Latest VaR and gross exposure from bot_activity_log."""
    try:
        conn = _get_conn()
        if not conn:
            return {}
        with conn.cursor() as cur:
            cur.execute("""
                SELECT message, timestamp
                FROM bot_activity_log
                WHERE event_type = 'portfolio_var'
                ORDER BY timestamp DESC LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return {"var_dollars": None, "var_pct": None, "gross_dollars": None, "gross_pct": None}
            # message format: "VaR=$X (Y%) gross=$A (B%)"
            import re as _re
            msg = row[0] or ""
            var_d = _re.search(r'VaR=\$([\d,\.]+)', msg)
            var_p = _re.search(r'VaR=.*?\(([\d\.]+)%\)', msg)
            gr_d  = _re.search(r'gross=\$([\d,\.]+)', msg)
            gr_p  = _re.search(r'gross=.*?\(([\d\.]+)%\)', msg)
            return {
                "var_dollars": float(var_d.group(1).replace(",", "")) if var_d else None,
                "var_pct": float(var_p.group(1)) if var_p else None,
                "gross_dollars": float(gr_d.group(1).replace(",", "")) if gr_d else None,
                "gross_pct": float(gr_p.group(1)) if gr_p else None,
                "as_of": row[1].isoformat() if row[1] else None,
            }
    except Exception as e:
        logger.warning(f"get_portfolio_var failed ({e})")
        return {}


def get_performance_by_setup() -> list[dict]:
    """P&L breakdown by setup_type from position_log."""
    try:
        conn = _get_conn()
        if not conn:
            return []
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COALESCE(setup_type, 'unknown') AS setup_type,
                    COUNT(*) AS trades,
                    ROUND(SUM(COALESCE(realized_pl, 0))::numeric, 2) AS total_pl,
                    ROUND(AVG(COALESCE(realized_pl_pct, 0))::numeric, 2) AS avg_pl_pct,
                    ROUND(AVG(CASE WHEN realized_pl > 0 THEN 1.0 ELSE 0 END)::numeric, 3) AS win_rate
                FROM position_log
                WHERE exit_time IS NOT NULL
                GROUP BY setup_type
                ORDER BY total_pl DESC
            """)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.warning(f"get_performance_by_setup failed ({e})")
        return []


def get_ai_baseline_comparison() -> dict:
    """Compare signal-only win rate vs Claude-assisted win rate, plus override rate."""
    try:
        conn = _get_conn()
        if not conn:
            return {}
        with conn.cursor() as cur:
            # Count signal_baseline events where signal said buy
            cur.execute("""
                SELECT COUNT(*) FROM bot_activity_log
                WHERE event_type = 'signal_baseline' AND message LIKE '%signal_only=buy%'
            """)
            signal_buys = (cur.fetchone() or [0])[0]

            # Count claude_override events (Claude said no to score>=65)
            cur.execute("""
                SELECT COUNT(*) FROM bot_activity_log
                WHERE event_type = 'claude_override'
            """)
            overrides = (cur.fetchone() or [0])[0]

            # Actual win rate from position_log
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END) AS wins
                FROM position_log WHERE exit_time IS NOT NULL
            """)
            row = cur.fetchone()
            total, wins = (row[0] or 0), (row[1] or 0)
            claude_win_rate = round(wins / total, 3) if total > 0 else None

            override_rate = round(overrides / signal_buys, 3) if signal_buys > 0 else None

            return {
                "signal_buys": signal_buys,
                "claude_overrides": overrides,
                "override_rate": override_rate,
                "claude_win_rate": claude_win_rate,
                "total_closed_trades": total,
            }
    except Exception as e:
        logger.warning(f"get_ai_baseline_comparison failed ({e})")
        return {}


def get_slippage_summary() -> dict:
    """Aggregate slippage stats from trade_slippage_log."""
    try:
        conn = _get_conn()
        if not conn:
            return {}
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS fills,
                    ROUND(AVG(slippage_dollars)::numeric, 4) AS avg_slippage_dollars,
                    ROUND(AVG(slippage_pct * 100)::numeric, 4) AS avg_slippage_pct,
                    ROUND(SUM(ABS(slippage_dollars))::numeric, 2) AS total_slippage_cost,
                    ROUND(MIN(slippage_dollars)::numeric, 4) AS best_slippage,
                    ROUND(MAX(slippage_dollars)::numeric, 4) AS worst_slippage
                FROM trade_slippage_log
            """)
            row = cur.fetchone()
            if not row or row[0] == 0:
                return {"fills": 0}
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))
    except Exception as e:
        logger.warning(f"get_slippage_summary failed ({e})")
        return {}


def get_performance_by_hour() -> list[dict]:
    """Win rate by entry_hour_et from position_log."""
    try:
        conn = _get_conn()
        if not conn:
            return []
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    entry_hour_et,
                    COUNT(*) AS trades,
                    ROUND(AVG(CASE WHEN realized_pl > 0 THEN 1.0 ELSE 0 END)::numeric, 3) AS win_rate,
                    ROUND(AVG(COALESCE(realized_pl_pct, 0))::numeric, 2) AS avg_pl_pct
                FROM position_log
                WHERE exit_time IS NOT NULL AND entry_hour_et IS NOT NULL
                GROUP BY entry_hour_et
                ORDER BY entry_hour_et
            """)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.warning(f"get_performance_by_hour failed ({e})")
        return []


# ── Session 7: Daily Universe Log ─────────────────────────────────────────────


def ensure_session7_tables() -> None:
    """Create Session 7 tables if they don't already exist. Safe to call repeatedly."""
    ddl = """
        CREATE TABLE IF NOT EXISTS daily_universe_log (
            id SERIAL PRIMARY KEY,
            log_date DATE NOT NULL,
            symbol VARCHAR(10) NOT NULL,
            sources TEXT[] NOT NULL DEFAULT '{}',
            first_seen_at TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (log_date, symbol)
        );

        CREATE TABLE IF NOT EXISTS near_miss_log (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(10) NOT NULL,
            score INTEGER NOT NULL,
            breakdown JSONB,
            suggested_action VARCHAR(10),
            price_at_skip NUMERIC,
            price_1h NUMERIC,
            price_eod NUMERIC,
            price_next_day NUMERIC,
            hypothetical_pnl_pct NUMERIC,
            timestamp TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS signal_performance_log (
            id SERIAL PRIMARY KEY,
            trade_date DATE NOT NULL,
            symbol VARCHAR(10) NOT NULL,
            signal_name VARCHAR(50) NOT NULL,
            signal_boost INTEGER NOT NULL,
            trade_profitable BOOLEAN,
            trade_pnl_pct NUMERIC,
            timestamp TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS signal_weights (
            signal_name VARCHAR(50) PRIMARY KEY,
            current_weight INTEGER NOT NULL,
            default_weight INTEGER NOT NULL,
            win_rate_30d NUMERIC,
            sample_count_30d INTEGER,
            last_adjusted DATE,
            adjustment_reason TEXT
        );

        CREATE TABLE IF NOT EXISTS connector_health_log (
            id SERIAL PRIMARY KEY,
            connector_name VARCHAR(50) NOT NULL,
            status VARCHAR(20) NOT NULL,  -- 'ok' | 'unavailable' | 'error'
            details TEXT,
            called_at TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_connector_health_name_time
            ON connector_health_log (connector_name, called_at DESC);

        CREATE TABLE IF NOT EXISTS signal_weight_history (
            id SERIAL PRIMARY KEY,
            signal_name VARCHAR(50) NOT NULL,
            old_weight INTEGER NOT NULL,
            new_weight INTEGER NOT NULL,
            direction VARCHAR(4),
            win_rate NUMERIC,
            sample_count INTEGER,
            adjusted_at DATE DEFAULT CURRENT_DATE
        );

        CREATE TABLE IF NOT EXISTS sprint_review_daily (
            id SERIAL PRIMARY KEY,
            review_date DATE UNIQUE NOT NULL,
            top_gainers JSONB,
            top_losers JSONB,
            opportunity_capture_rate NUMERIC,
            hypothetical_long_pnl NUMERIC,
            hypothetical_short_pnl NUMERIC,
            actual_kova_pnl NUMERIC,
            missed_entirely_count INTEGER,
            missed_by_source JSONB,
            signal_win_rates JSONB,
            flagged_signals TEXT[],
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """
    try:
        conn = _get_conn()
        if not conn:
            return
        with conn.cursor() as cur:
            cur.execute(ddl)
        # Seed default signal weights if table is empty
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM signal_weights")
            if cur.fetchone()[0] == 0:
                cur.execute("""
                    INSERT INTO signal_weights (signal_name, current_weight, default_weight) VALUES
                    ('barchart_very_unusual', 18, 18),
                    ('barchart_unusual', 10, 10),
                    ('earnings_surprise_strong', 12, 12),
                    ('earnings_surprise_mild', 6, 6),
                    ('insider_buy_large', 15, 15),
                    ('insider_buy_small', 8, 8),
                    ('analyst_revision', 10, 10),
                    ('options_flow_fallback', 10, 10)
                    ON CONFLICT DO NOTHING
                """)
        logger.info("Session 7 tables ensured.")
    except Exception as e:
        logger.warning(f"ensure_session7_tables failed ({e})")


def log_daily_universe(symbols_sources: dict[str, list[str]]) -> None:
    """
    Log which symbols entered the universe today and via which sources.
    symbols_sources: {symbol: [source1, source2, ...]}
    """
    if TEST_MODE:
        return
    try:
        conn = _get_conn()
        if not conn:
            return
        today = datetime.now(timezone.utc).date()
        with conn.cursor() as cur:
            for symbol, sources in symbols_sources.items():
                cur.execute("""
                    INSERT INTO daily_universe_log (log_date, symbol, sources)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (log_date, symbol) DO UPDATE
                        SET sources = ARRAY(
                            SELECT DISTINCT unnest(daily_universe_log.sources || EXCLUDED.sources)
                        )
                """, (today, symbol, sources))
    except Exception as e:
        logger.debug(f"log_daily_universe failed (non-fatal): {e}")


def log_near_miss(symbol: str, score: int, breakdown: dict,
                  suggested_action: str, price: float) -> int:
    """Log a near-miss candidate (score 35–44). Returns row id for price followup."""
    if TEST_MODE:
        return
    row_id = 0
    try:
        conn = _get_conn()
        if not conn:
            return 0
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO near_miss_log (symbol, score, breakdown, suggested_action, price_at_skip)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (symbol, score, json.dumps(breakdown), suggested_action, price))
            row_id = cur.fetchone()[0]
    except Exception as e:
        logger.debug(f"log_near_miss failed (non-fatal): {e}")
    return row_id


def update_near_miss_prices(row_id: int, field: str, price: float) -> None:
    """Update a price field on a near_miss_log row. field: price_1h|price_eod|price_next_day"""
    if TEST_MODE:
        return
    if not row_id:
        return
    allowed = {"price_1h", "price_eod", "price_next_day"}
    if field not in allowed:
        return
    try:
        conn = _get_conn()
        if not conn:
            return
        with conn.cursor() as cur:
            cur.execute(f"UPDATE near_miss_log SET {field} = %s WHERE id = %s", (price, row_id))
            cur.execute("""
                UPDATE near_miss_log SET
                    hypothetical_pnl_pct = CASE
                        WHEN price_at_skip > 0 AND price_eod IS NOT NULL
                        THEN (price_eod - price_at_skip) / price_at_skip * 100
                        ELSE NULL END
                WHERE id = %s
            """, (row_id,))
    except Exception as e:
        logger.debug(f"update_near_miss_prices failed (non-fatal): {e}")


def get_near_misses_report() -> dict:
    """Near-miss tracker: what happened to stocks that scored 35-44."""
    try:
        conn = _get_conn()
        if not conn:
            return {}
        with conn.cursor() as cur:
            # Summary stats
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE hypothetical_pnl_pct > 0) AS would_have_won,
                    ROUND(AVG(hypothetical_pnl_pct)::numeric, 2) AS avg_pnl,
                    ROUND(AVG(score)::numeric, 1) AS avg_score
                FROM near_miss_log
                WHERE timestamp > NOW() - INTERVAL '30 days'
                  AND hypothetical_pnl_pct IS NOT NULL
            """)
            row = cur.fetchone()
            total, won, avg_pnl, avg_score = row if row else (0, 0, None, None)

            accuracy = round(won / total * 100, 1) if total else 0.0

            # Which signals were most often the deciding factor (lowest score contributor)
            # We look at which signal had the most negative contribution in near-misses
            cur.execute("""
                SELECT breakdown
                FROM near_miss_log
                WHERE timestamp > NOW() - INTERVAL '30 days'
                  AND hypothetical_pnl_pct IS NOT NULL
            """)
            breakdowns = [r[0] for r in cur.fetchall() if r[0]]

            signal_stats: dict = {}
            for bd in breakdowns:
                if not isinstance(bd, dict):
                    continue
                for sig, val in bd.items():
                    if sig not in signal_stats:
                        signal_stats[sig] = {"count": 0, "total_pnl": 0.0}
                    signal_stats[sig]["count"] += 1

            top_signals = sorted(
                [{"signal": k, "times_was_deciding_factor": v["count"]} for k, v in signal_stats.items()],
                key=lambda x: x["times_was_deciding_factor"], reverse=True
            )[:5]

            # Threshold verdict
            verdict = "Not enough data yet (need 20+ near-misses with price followup)"
            if total >= 20:
                if accuracy > 60:
                    verdict = f"Threshold (45) may be too high — {accuracy:.0f}% of near-misses were profitable. Consider lowering to 40."
                elif accuracy < 40:
                    verdict = f"Threshold (45) looks correct — only {accuracy:.0f}% of near-misses would have been profitable."
                else:
                    verdict = f"Threshold (45) looks optimal — {accuracy:.0f}% accuracy on near-misses."

            # Recent near-misses
            cur.execute("""
                SELECT symbol, score, suggested_action, price_at_skip, price_eod, hypothetical_pnl_pct, timestamp
                FROM near_miss_log
                WHERE timestamp > NOW() - INTERVAL '7 days'
                ORDER BY timestamp DESC
                LIMIT 20
            """)
            recent = []
            for r in cur.fetchall():
                sym, sc, action, p_skip, p_eod, pnl, ts = r
                recent.append({
                    "symbol": sym, "score": sc, "suggested_action": action,
                    "price_at_skip": float(p_skip) if p_skip else None,
                    "price_eod": float(p_eod) if p_eod else None,
                    "hypothetical_pnl_pct": float(pnl) if pnl else None,
                    "was_right_to_skip": (pnl is not None and pnl < 0),
                    "timestamp": ts.isoformat() if ts else None,
                })

            return {
                "summary": {
                    "total_near_misses": total or 0,
                    "would_have_been_profitable": won or 0,
                    "accuracy_if_traded": f"{accuracy:.1f}%",
                    "avg_hypothetical_pnl": f"{avg_pnl:+.2f}%" if avg_pnl is not None else "N/A",
                    "avg_score": float(avg_score) if avg_score else None,
                    "threshold_verdict": verdict,
                },
                "top_missed_signals": top_signals,
                "recent": recent,
            }
    except Exception as e:
        logger.warning(f"get_near_misses_report failed ({e})")
        return {}


def log_signal_performance(trade_date, symbol: str, breakdown: dict,
                           trade_profitable: bool, trade_pnl_pct: float) -> None:
    """Log each signal's contribution to a closed trade for win-rate tracking."""
    if TEST_MODE:
        return
    try:
        conn = _get_conn()
        if not conn:
            return
        with conn.cursor() as cur:
            for signal_name, boost in (breakdown or {}).items():
                if boost == 0:
                    continue
                cur.execute("""
                    INSERT INTO signal_performance_log
                        (trade_date, symbol, signal_name, signal_boost, trade_profitable, trade_pnl_pct)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (trade_date, symbol, signal_name, int(boost), trade_profitable, trade_pnl_pct))
    except Exception as e:
        logger.debug(f"log_signal_performance failed (non-fatal): {e}")


def get_signal_weights() -> dict[str, int]:
    """Read current signal weights from DB. Returns {signal_name: current_weight}."""
    try:
        conn = _get_conn()
        if not conn:
            return {}
        with conn.cursor() as cur:
            cur.execute("SELECT signal_name, current_weight FROM signal_weights")
            return {row[0]: row[1] for row in cur.fetchall()}
    except Exception as e:
        logger.debug(f"get_signal_weights failed (non-fatal): {e}")
        return {}


def get_signal_weights_full() -> dict[str, dict]:
    """Read current + default weights. Returns {signal_name: {current, default}}."""
    try:
        conn = _get_conn()
        if not conn:
            return {}
        with conn.cursor() as cur:
            cur.execute("SELECT signal_name, current_weight, default_weight FROM signal_weights")
            return {row[0]: {"current": row[1], "default": row[2]} for row in cur.fetchall()}
    except Exception as e:
        logger.debug(f"get_signal_weights_full failed (non-fatal): {e}")
        return {}


def update_signal_weight(signal_name: str, new_weight: int, reason: str,
                         old_weight: int | None = None, win_rate: float | None = None,
                         sample_count: int | None = None) -> None:
    """Update a signal's weight in the DB and append a history row."""
    if TEST_MODE:
        return
    try:
        conn = _get_conn()
        if not conn:
            return
        with conn.cursor() as cur:
            # Fetch old weight if not provided
            if old_weight is None:
                cur.execute("SELECT current_weight FROM signal_weights WHERE signal_name = %s", (signal_name,))
                row = cur.fetchone()
                old_weight = row[0] if row else new_weight

            direction = "up" if new_weight > old_weight else "down" if new_weight < old_weight else "flat"

            cur.execute("""
                UPDATE signal_weights
                SET current_weight = %s, last_adjusted = CURRENT_DATE, adjustment_reason = %s
                WHERE signal_name = %s
            """, (new_weight, reason, signal_name))

            cur.execute("""
                INSERT INTO signal_weight_history
                    (signal_name, old_weight, new_weight, direction, win_rate, sample_count)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (signal_name, old_weight, new_weight, direction, win_rate, sample_count))

        conn.commit()
    except Exception as e:
        logger.debug(f"update_signal_weight failed (non-fatal): {e}")


def log_connector_call(connector_name: str, status: str, details: str = "") -> None:
    """Log a connector call outcome. status: 'ok' | 'unavailable' | 'error'."""
    if TEST_MODE:
        return
    try:
        conn = _get_conn()
        if not conn:
            return
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO connector_health_log (connector_name, status, details)
                VALUES (%s, %s, %s)
            """, (connector_name, status, details[:500] if details else ""))
        conn.commit()
    except Exception as e:
        logger.debug(f"log_connector_call failed (non-fatal): {e}")


def get_connector_health(hours: int = 24) -> list[dict]:
    """
    Returns failure rate per connector over last N hours.
    Used by health checker to decide when to alert.
    """
    try:
        conn = _get_conn()
        if not conn:
            return []
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    connector_name,
                    COUNT(*) FILTER (WHERE status != 'no_key') AS total_calls,
                    SUM(CASE WHEN status = 'unavailable' OR status = 'error' THEN 1 ELSE 0 END) AS failed_calls,
                    CASE
                        WHEN COUNT(*) FILTER (WHERE status != 'no_key') = 0 THEN 0
                        ELSE ROUND(
                            SUM(CASE WHEN status IN ('unavailable','error') THEN 1.0 ELSE 0 END)
                            / COUNT(*) FILTER (WHERE status != 'no_key') * 100
                        )
                    END AS failure_pct,
                    MAX(called_at) AS last_called,
                    MAX(CASE WHEN status IN ('unavailable','error') THEN details END) AS last_error,
                    BOOL_OR(status = 'no_key') AS key_missing
                FROM connector_health_log
                WHERE called_at >= NOW() - INTERVAL '%s hours'
                GROUP BY connector_name
                ORDER BY failure_pct DESC
            """, (hours,))
            rows = cur.fetchall()
            return [
                {
                    "connector_name": r[0],
                    "total_calls": r[1] or 0,
                    "failed_calls": r[2] or 0,
                    "failure_pct": int(r[3] or 0),
                    "last_called": r[4].isoformat() if r[4] else None,
                    "last_error": r[5],
                    "key_missing": bool(r[6]),
                }
                for r in rows
            ]
    except Exception as e:
        logger.debug(f"get_connector_health failed (non-fatal): {e}")
        return []


def get_signal_win_rates(days: int = 30) -> list[dict]:
    """Win rate per signal over last N days, for weekly weight adjustment."""
    try:
        conn = _get_conn()
        if not conn:
            return []
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    signal_name,
                    COUNT(*) AS sample_count,
                    COUNT(*) FILTER (WHERE trade_profitable) AS wins,
                    ROUND(AVG(CASE WHEN trade_profitable THEN 1.0 ELSE 0 END)::numeric, 3) AS win_rate,
                    ROUND(AVG(trade_pnl_pct)::numeric, 2) AS avg_pnl_pct
                FROM signal_performance_log
                WHERE trade_date >= CURRENT_DATE - %s
                GROUP BY signal_name
                ORDER BY sample_count DESC
            """, (days,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.warning(f"get_signal_win_rates failed ({e})")
        return []


def save_sprint_review(review: dict) -> None:
    """Upsert daily sprint review snapshot."""
    if TEST_MODE:
        return
    try:
        conn = _get_conn()
        if not conn:
            return
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sprint_review_daily
                    (review_date, top_gainers, top_losers, opportunity_capture_rate,
                     hypothetical_long_pnl, hypothetical_short_pnl, actual_kova_pnl,
                     missed_entirely_count, missed_by_source, signal_win_rates, flagged_signals)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (review_date) DO UPDATE SET
                    top_gainers = EXCLUDED.top_gainers,
                    top_losers = EXCLUDED.top_losers,
                    opportunity_capture_rate = EXCLUDED.opportunity_capture_rate,
                    hypothetical_long_pnl = EXCLUDED.hypothetical_long_pnl,
                    hypothetical_short_pnl = EXCLUDED.hypothetical_short_pnl,
                    actual_kova_pnl = EXCLUDED.actual_kova_pnl,
                    missed_entirely_count = EXCLUDED.missed_entirely_count,
                    missed_by_source = EXCLUDED.missed_by_source,
                    signal_win_rates = EXCLUDED.signal_win_rates,
                    flagged_signals = EXCLUDED.flagged_signals
            """, (
                review["review_date"],
                json.dumps(review.get("top_gainers", [])),
                json.dumps(review.get("top_losers", [])),
                review.get("opportunity_capture_rate"),
                review.get("hypothetical_long_pnl"),
                review.get("hypothetical_short_pnl"),
                review.get("actual_kova_pnl"),
                review.get("missed_entirely_count"),
                json.dumps(review.get("missed_by_source", {})),
                json.dumps(review.get("signal_win_rates", {})),
                review.get("flagged_signals", []),
            ))
    except Exception as e:
        logger.warning(f"save_sprint_review failed ({e})")


def get_latest_sprint_review() -> dict:
    """Get the most recent daily sprint review."""
    try:
        conn = _get_conn()
        if not conn:
            return {}
        with conn.cursor() as cur:
            cur.execute("""
                SELECT review_date, top_gainers, top_losers, opportunity_capture_rate,
                       hypothetical_long_pnl, hypothetical_short_pnl, actual_kova_pnl,
                       missed_entirely_count, missed_by_source, signal_win_rates, flagged_signals,
                       created_at
                FROM sprint_review_daily
                ORDER BY review_date DESC
                LIMIT 1
            """)
            row = cur.fetchone()
            if not row:
                return {}
            cols = [d[0] for d in cur.description]
            result = dict(zip(cols, row))
            result["review_date"] = result["review_date"].isoformat() if result["review_date"] else None
            result["created_at"] = result["created_at"].isoformat() if result["created_at"] else None
            return result
    except Exception as e:
        logger.warning(f"get_latest_sprint_review failed ({e})")
        return {}


def get_sprint_review_history(days: int = 7) -> list[dict]:
    """Get recent daily sprint reviews for weekly summary."""
    try:
        conn = _get_conn()
        if not conn:
            return []
        with conn.cursor() as cur:
            cur.execute("""
                SELECT review_date, opportunity_capture_rate, actual_kova_pnl,
                       hypothetical_long_pnl, missed_entirely_count, flagged_signals,
                       signal_win_rates
                FROM sprint_review_daily
                WHERE review_date >= CURRENT_DATE - %s
                ORDER BY review_date DESC
            """, (days,))
            cols = [d[0] for d in cur.description]
            rows = []
            for row in cur.fetchall():
                d = dict(zip(cols, row))
                d["review_date"] = d["review_date"].isoformat() if d["review_date"] else None
                rows.append(d)
            return rows
    except Exception as e:
        logger.warning(f"get_sprint_review_history failed ({e})")
        return []
