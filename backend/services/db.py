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
    """Create the ai_cache table if it doesn't exist."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_cache (
                key         TEXT PRIMARY KEY,
                data        JSONB NOT NULL,
                cached_at   TIMESTAMPTZ NOT NULL,
                expires_at  TIMESTAMPTZ NOT NULL
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
