"""
wheel_engine.py — Options Wheel Bot execution engine

Strategy:
  Phase 1 (put_open):  Sell cash-secured put. Collect premium upfront.
  Phase 2 (assigned):  Put assigned → we own 100 shares at strike price.
  Phase 3 (call_open): Sell covered call 5% above cost basis.
  Phase 4 (complete):  Call expires/exercises → full cycle done → repeat.

Full isolation from Kova:
  - Separate Alpaca account (ALPACA_WHEEL_KEY / ALPACA_WHEEL_SECRET)
  - ALPACA_WHEEL_BASE_URL drives paper vs live — change on Railway to switch
  - Zero imports from trading_engine, claude_service, or brain modules
  - Reads Kova's regime cache read-only (no write dependency)
  - Own tables: wheel_positions, wheel_universe, wheel_symbol_stats
  - strategy='wheel' tag on all DB entries

AI:
  - Only ever calls ask_ai() — non-critical tier (Gemini Flash / Haiku)
  - Managed from iOS model settings (standard tier, never Pro/Sonnet)
  - Used for universe discovery only (once/week) — never per trade

Profit reserve:
  - Mirrors Kova's profit_reserve but keyed separately: 'wheel:reserved_cash'
  - Governed by same profit_reserve_pct setting from risk config
  - Tracked independently per account

Take profit (early close):
  - If open option has decayed to ≤50% of original premium → buy-to-close early
  - Frees capital 2-3 weeks early, redeploy for next cycle
  - check_profit_targets() runs daily alongside assignment/expiration checks
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

# ── Execution config ─────────────────────────────────────────────────────────
# All key params are Railway env-var overridable — change on Railway, no redeploy.
# Sensible defaults shown. Override via WHEEL_* env vars to tune as account grows.
import os as _os

MAX_ACTIVE_POSITIONS   = int(_os.environ.get("WHEEL_MAX_POSITIONS",    "5"))    # scale up as capital grows
CONTRACTS_PER_TRADE    = int(_os.environ.get("WHEEL_CONTRACTS",        "2"))    # tiered close strategy
MIN_PREMIUM_YIELD      = float(_os.environ.get("WHEEL_MIN_YIELD",      "0.008")) # 0.8% min yield/strike (bear: defensive stocks have lower IV)
TARGET_DTE             = int(_os.environ.get("WHEEL_TARGET_DTE",       "45"))   # sweet spot for theta
MIN_DTE                = int(_os.environ.get("WHEEL_MIN_DTE",          "30"))   # never inside 30 DTE — 21 is force-close, need buffer
MAX_DTE                = int(_os.environ.get("WHEEL_MAX_DTE",          "60"))   # never beyond 60 DTE
DTE_FORCE_CLOSE        = int(_os.environ.get("WHEEL_FORCE_CLOSE_DTE",  "21"))   # gamma risk spikes at 21
TARGET_DELTA           = float(_os.environ.get("WHEEL_TARGET_DELTA",   "0.25")) # ~25-delta put
ASSIGNMENT_CALL_BUFFER = float(_os.environ.get("WHEEL_CALL_BUFFER",    "0.05")) # covered call 5% above cost

# Tiered profit close (2 contracts per trade):
#   Contract 1: close when premium remaining = 50% → locked profit, capital freed early
#   Contract 2: close when premium remaining = 25% → rides theta to near-zero
PROFIT_TIER_1 = float(_os.environ.get("WHEEL_PROFIT_TIER_1", "0.50"))
PROFIT_TIER_2 = float(_os.environ.get("WHEEL_PROFIT_TIER_2", "0.25"))
EARLY_CLOSE_THRESHOLD = PROFIT_TIER_1   # alias for backward-compat (tests + status endpoint)

# IV Rank filter — only enter when options are genuinely expensive
MIN_IV_RANK     = float(_os.environ.get("WHEEL_MIN_IV_RANK",     "30"))  # 0-100 scale
MIN_IV_ABSOLUTE = float(_os.environ.get("WHEEL_MIN_IV_ABS",      "0.25")) # fallback when <20 days history

# Sector diversification
MAX_PER_SECTOR  = int(_os.environ.get("WHEEL_MAX_PER_SECTOR",    "2"))

# Scan quality filters
MAX_SPREAD_PCT      = float(_os.environ.get("WHEEL_MAX_SPREAD_PCT",    "0.30"))  # skip if bid-ask spread > 30%
MIN_OPTION_VOLUME   = int(_os.environ.get("WHEEL_MIN_OPTION_VOLUME",   "10"))    # skip if 0 contracts traded today
MIN_DOLLAR_PREMIUM  = float(_os.environ.get("WHEEL_MIN_DOLLAR_PREMIUM","50.0"))  # skip if < $50/contract
DELTA_MAX_BUFFER    = float(_os.environ.get("WHEEL_DELTA_BUFFER",      "0.10"))  # max delta = TARGET_DELTA + buffer

# Capital protection
WEEKLY_LOSS_CAP_PCT = float(_os.environ.get("WHEEL_WEEKLY_LOSS_CAP",   "0.03"))  # pause new trades if week loss > 3%

# Assignment bounce-wait
BOUNCE_WAIT_DROP_PCT  = float(_os.environ.get("WHEEL_BOUNCE_WAIT_DROP", "0.03")) # wait 1 day if drop 3-10%
STRUCTURAL_DROP_PCT   = float(_os.environ.get("WHEEL_STRUCTURAL_DROP",  "0.10")) # fetch news + AI if drop > 10%

# VIX-based position sizing
_VIX_SIZING = {
    "extreme_fear": {"contracts": 1, "max_positions": 3},
    "elevated":     {"contracts": 1, "max_positions": 5},
    "normal":       {"contracts": 2, "max_positions": 5},
    "low_fear":     {"contracts": 2, "max_positions": 5},
}
SECTOR_MAP = {
    "SOFI": "fintech",   "HOOD": "fintech",   "PYPL": "fintech",
    "AFRM": "fintech",   "DAVE": "fintech",   "BILL": "fintech",
    "COIN": "crypto",    "MSTR": "crypto",
    "BAC":  "banks",     "C":    "banks",      "WFC":  "banks",
    "JPM":  "banks",     "GS":   "banks",      "MS":   "banks",
    "NVDA": "semis",     "AMD":  "semis",      "MU":   "semis",
    "INTC": "semis",     "QCOM": "semis",      "AVGO": "semis",
    "SMCI": "tech",      "CRM":  "tech",       "SNOW": "tech",
    "SNAP": "social",    "PINS": "social",     "RBLX": "social",
    "RIVN": "ev",        "LCID": "ev",         "NIO":  "ev",
    "XPEV": "ev",        "LI":   "ev",
    "F":    "auto",      "GM":   "auto",
    "T":    "telecom",   "VZ":   "telecom",
    "WBA":  "healthcare","CVS":  "healthcare", "TDOC": "healthcare",
    "FCX":  "materials", "AA":   "materials",  "CLF":  "materials",
    "UBER": "gig",       "LYFT": "gig",        "DASH": "gig",
    "PENN": "gaming",    "DKNG": "gaming",     "MGM":  "gaming",
    "PLTR": "data",      "TSLA": "ev",
}

# Cache key for wheel profit reserve (separate from Kova's reserve)
_WHEEL_RESERVE_KEY = "wheel:reserved_cash"


# ── Alpaca client factory ──────────────────────────────────────────────────────

def _is_paper() -> bool:
    """Derive paper/live from the base URL env var — never hardcoded."""
    return "paper" in settings.alpaca_wheel_base_url.lower()


def _wheel_keys() -> tuple[str, str]:
    key    = settings.alpaca_wheel_key    or settings.alpaca_api_key
    secret = settings.alpaca_wheel_secret or settings.alpaca_secret_key
    return key, secret


def _get_wheel_trading_client():
    from alpaca.trading.client import TradingClient
    key, secret = _wheel_keys()
    return TradingClient(key, secret, paper=_is_paper())


def _get_wheel_data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    key, secret = _wheel_keys()
    return StockHistoricalDataClient(key, secret)


def _get_wheel_options_client():
    from alpaca.data.historical.option import OptionHistoricalDataClient
    key, secret = _wheel_keys()
    return OptionHistoricalDataClient(key, secret)


# ── Market hours guard ────────────────────────────────────────────────────────

def _market_is_open() -> bool:
    """
    Check Alpaca market clock. Returns False on holidays, half-days (after close),
    weekends, or outside regular hours.
    Wheel bot never attempts trades when market is closed.
    """
    try:
        client = _get_wheel_trading_client()
        clock = client.get_clock()
        return bool(clock.is_open)
    except Exception as e:
        logger.warning(f"Wheel market clock check failed: {e} — assuming closed")
        return False


def _next_market_open() -> Optional[str]:
    """Return next market open time as ISO string (for logging)."""
    try:
        clock = _get_wheel_trading_client().get_clock()
        return clock.next_open.isoformat() if clock.next_open else None
    except Exception:
        return None


# ── Regime (read-only from Kova cache) ───────────────────────────────────────
#
# Kova stores regime in "premarket_scan" → "macro_regime" (2h TTL, ~9 AM ET).
# Wheel reads this key. Falls back to computing fresh from macro.py if stale.
# NEVER writes to Kova's cache — read-only.

def _get_current_regime() -> str:
    try:
        from services.db import cache_get

        # Primary: Kova's premarket_scan cache (written ~9 AM ET daily)
        premarket = cache_get("premarket_scan")
        if isinstance(premarket, dict):
            regime = premarket.get("macro_regime", "")
            if regime:
                return regime.lower()

        # Fallback 1: legacy "market_regime" key (may not exist, but check anyway)
        data = cache_get("market_regime")
        if isinstance(data, dict):
            return data.get("regime", "neutral").lower()
        if isinstance(data, str) and data:
            return data.lower()

        # Fallback 2: compute fresh from macro.py directly
        # Used when: wheel runs before Kova's 9 AM scan, or scan hasn't fired today
        from services.macro import get_macro_context
        macro = get_macro_context()
        return macro.get("market_regime", "neutral").lower()

    except Exception:
        pass
    return "neutral"


def _regime_adjusted_delta(regime: str) -> float:
    if regime == "bearish":
        return 0.15   # Further OTM in downtrends
    elif regime == "bullish":
        return 0.30   # Closer in uptrends, more premium
    return TARGET_DELTA


# ── Profit reserve (separate from Kova) ──────────────────────────────────────

def _get_wheel_reserve() -> float:
    try:
        from services.db import cache_get
        v = cache_get(_WHEEL_RESERVE_KEY)
        return float(v) if v is not None else 0.0
    except Exception:
        return 0.0


def _add_to_wheel_reserve(amount: float) -> float:
    try:
        from services.db import cache_get, cache_set
        current = _get_wheel_reserve()
        new_total = round(current + amount, 2)
        cache_set(_WHEEL_RESERVE_KEY, new_total, 365 * 24 * 3600)
        logger.info(f"Wheel reserve: +${amount:.2f} → total ${new_total:.2f}")
        return new_total
    except Exception as e:
        logger.error(f"Wheel reserve update error: {e}")
        return 0.0


# ── DB helpers ────────────────────────────────────────────────────────────────

def _open_wheel_position(symbol: str, phase: str, put_contract: str,
                          put_strike: float, put_expiry, put_premium: float,
                          put_order_id: str, regime: str) -> Optional[int]:
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return None
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO wheel_positions
                    (symbol, phase, put_contract, put_strike, put_expiry,
                     put_premium, put_order_id, regime_at_open, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active')
                RETURNING id
            """, (symbol, phase, put_contract, put_strike, put_expiry,
                  put_premium, put_order_id, regime))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.error(f"Wheel DB open_position: {e}")
        return None


def _update_wheel_position(position_id: int, **kwargs):
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return
        set_clauses = ", ".join(f"{k} = %s" for k in kwargs)
        values = list(kwargs.values()) + [position_id]
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE wheel_positions SET {set_clauses} WHERE id = %s",
                values
            )
    except Exception as e:
        logger.error(f"Wheel DB update_position: {e}")


def get_active_wheel_positions() -> list[dict]:
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return []
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, symbol, phase, put_contract, put_strike, put_expiry,
                       put_premium, shares_qty, cost_basis, call_contract,
                       call_strike, call_expiry, call_premium,
                       total_premium_collected, regime_at_open, opened_at,
                       realized_pl, status, notes
                FROM wheel_positions
                WHERE status IN ('active', 'order_pending')
                ORDER BY opened_at DESC
            """)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"Wheel DB get_active: {e}")
        return []


def get_wheel_summary() -> dict:
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return {}
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE status='active')    as active_count,
                    COUNT(*) FILTER (WHERE status='completed') as completed_count,
                    COALESCE(SUM(total_premium_collected) FILTER (WHERE status='active'), 0)           as active_premium,
                    COALESCE(SUM(total_premium_collected) FILTER (WHERE status != 'order_pending'), 0) as total_premium_ever,
                    COALESCE(SUM(realized_pl) FILTER (WHERE status='completed'), 0)             as total_realized_pl,
                    COALESCE(AVG(realized_pl) FILTER (WHERE status='completed'), 0)             as avg_realized_pl
                FROM wheel_positions
            """)
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            raw = dict(zip(cols, row)) if row else {}
            # Normalize keys to match Swift WheelSummary CodingKeys exactly
            result = {
                "total_premium_collected": float(raw.get("total_premium_ever", 0)),
                "total_realized_pl":       float(raw.get("total_realized_pl", 0)),
                "active_cycles":           int(raw.get("active_count", 0)),
                "completed_cycles":        int(raw.get("completed_count", 0)),
                "win_rate":                None,   # computed below if data exists
                "avg_cycle_days":          None,
                "active_premium":          float(raw.get("active_premium", 0)),
            }
            # Win rate: completed cycles with realized_pl > 0 / total completed
            completed = result["completed_cycles"]
            if completed > 0:
                try:
                    cur.execute("""
                        SELECT COUNT(*) FROM wheel_positions
                        WHERE status='completed' AND realized_pl > 0
                    """)
                    winners = (cur.fetchone() or [0])[0]
                    result["win_rate"] = round(winners / completed * 100, 1)
                except Exception:
                    pass
            result["profit_reserve"] = _get_wheel_reserve()
            return result
    except Exception as e:
        logger.error(f"Wheel DB get_summary: {e}")
        return {}


# ── Earnings avoidance ───────────────────────────────────────────────────────

_EARNINGS_CACHE_KEY = "wheel:earnings_dates"
_EARNINGS_CACHE_TTL = 12 * 3600   # refresh every 12 hours


def _get_earnings_within_days(symbols: list, days: int = 14) -> dict:
    """
    Returns {symbol: "YYYY-MM-DD"} for symbols with earnings within `days` calendar days.
    Uses FMP API if available, falls back to AI estimate, caches result 12h.

    This is the single most important filter for the wheel strategy.
    A stock gapping ±20% on earnings wrecks a put position instantly.
    """
    try:
        from services.db import cache_get, cache_set
        cached = cache_get(_EARNINGS_CACHE_KEY)
        if cached and isinstance(cached, dict):
            # Filter to requested symbols only
            return {s: cached[s] for s in symbols if s in cached}
    except Exception:
        pass

    result: dict[str, str] = {}
    today = date.today()
    cutoff = today + timedelta(days=days)

    # ── Attempt 1: FMP earnings calendar (if key available) ──────────────────
    try:
        from config import settings
        fmp_key = settings.fmp_api_key
        if fmp_key:
            import urllib.request, json as _json
            symbol_csv = ",".join(symbols)
            url = (f"https://financialmodelingprep.com/api/v3/earning_calendar"
                   f"?from={today}&to={cutoff}&apikey={fmp_key}")
            with urllib.request.urlopen(url, timeout=8) as resp:
                data = _json.loads(resp.read())
            for item in data:
                sym = item.get("symbol", "")
                dt = item.get("date", "")
                if sym in symbols and dt:
                    result[sym] = dt
            logger.info(f"Wheel earnings: FMP found {len(result)} upcoming in {days}d window")
    except Exception as fmp_err:
        logger.debug(f"Wheel earnings FMP lookup failed ({fmp_err}), trying AI")

    # ── Attempt 2: AI estimate for symbols not found via FMP ─────────────────
    remaining = [s for s in symbols if s not in result]
    if remaining:
        try:
            from services.ai_client import ask_ai, parse_ai_json
            prompt = f"""For each stock below, does it have earnings within the next {days} calendar days (today is {today})?

Stocks: {', '.join(remaining)}

Return ONLY valid JSON:
{{"earnings": [{{"symbol": "TSLA", "has_earnings_soon": true, "expected_date": "2026-06-15"}}, ...]}}

If you are NOT confident a stock has earnings in the next {days} days, set has_earnings_soon to false.
Only flag ones you are confident about."""
            raw = ask_ai(prompt, max_tokens=600)
            parsed = parse_ai_json(raw)
            for item in parsed.get("earnings", []):
                if item.get("has_earnings_soon") and item.get("expected_date"):
                    sym = item["symbol"]
                    if sym in symbols:
                        result[sym] = item["expected_date"]
            logger.info(f"Wheel earnings: AI flagged {len(result)} stocks with upcoming earnings")
        except Exception as ai_err:
            logger.warning(f"Wheel earnings AI lookup failed: {ai_err}")

    # Cache the full result for all symbols
    try:
        from services.db import cache_set
        cache_set(_EARNINGS_CACHE_KEY, result, _EARNINGS_CACHE_TTL)
    except Exception:
        pass

    return result


# ── MA20 batch fetch ─────────────────────────────────────────────────────────

def _get_ma20_batch(symbols: list[str], data_client) -> dict[str, float]:
    """
    Fetch 25-day daily bars for all symbols in one call.
    Returns {symbol: ma20} — 0.0 if data unavailable (treated as neutral, not blocked).
    """
    result = {s: 0.0 for s in symbols}
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        end   = datetime.now(timezone.utc)
        start = end - timedelta(days=35)
        from alpaca.data.enums import DataFeed
        bars  = data_client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            feed=DataFeed.IEX,
        ))
        for sym in symbols:
            try:
                closes = [float(b.close) for b in (bars.get(sym) or [])]
                if len(closes) >= 20:
                    result[sym] = sum(closes[-20:]) / 20
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Wheel MA20 batch error: {e}")
    return result


# ── Weekly loss cap ───────────────────────────────────────────────────────────

def _get_weekly_realized_loss() -> float:
    """
    Sum of realized losses (negative P&L only) from positions closed this week (Mon–today).
    Returns a negative number or 0.0. Used to pause new trades if loss > WEEKLY_LOSS_CAP_PCT.
    """
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return 0.0
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(SUM(realized_pl), 0)
                FROM wheel_positions
                WHERE status IN ('completed', 'stop_loss')
                  AND realized_pl < 0
                  AND closed_at >= date_trunc('week', NOW())
            """)
            row = cur.fetchone()
            return float(row[0]) if row else 0.0
    except Exception as e:
        logger.warning(f"Wheel weekly loss fetch: {e}")
        return 0.0


# ── VIX-aware cycle sizing ────────────────────────────────────────────────────

def _get_vix_sizing() -> dict:
    """
    Read vix_level from macro cache. Return contracts-per-trade and max-positions
    for this cycle based on current fear level.
    Never blocks a stock — just sizes down in high-fear environments.
    """
    try:
        from services.db import cache_get
        macro = cache_get("premarket_scan") or {}
        vix_level = macro.get("vix_level", "normal") or "normal"
        sizing = _VIX_SIZING.get(vix_level.lower(), _VIX_SIZING["normal"])
        if vix_level != "normal":
            logger.info(f"Wheel VIX sizing: vix_level={vix_level} → {sizing['contracts']} contracts, max {sizing['max_positions']} positions")
        return sizing
    except Exception:
        return _VIX_SIZING["normal"]


# ── AI assignment decision ────────────────────────────────────────────────────

def _ai_assignment_decision(symbol: str, cost_basis: float, current_price: float) -> str:
    """
    Called only when stock drops > STRUCTURAL_DROP_PCT (10%) from cost basis at assignment.
    Fetches last 3 Benzinga headlines, asks AI: temporary or structural drop?

    Returns:
      "structural" → stop-loss now (don't write covered call into falling knife)
      "temporary"  → wait 1 day for bounce, then sell call
      "normal"     → proceed with covered call immediately (AI unsure / no news)
    """
    try:
        from services.alpaca_service import get_news
        from services.ai_client import ask_ai, parse_ai_json

        headlines = get_news(symbols=[symbol], limit=3)
        if not headlines:
            return "normal"

        headline_text = "\n".join(
            f"- {h.get('title', '')} ({h.get('source', '')})"
            for h in headlines[:3]
        )
        drop_pct = (cost_basis - current_price) / cost_basis * 100

        prompt = f"""Wheel options bot. We sold a cash-secured put on {symbol} and got assigned at ${cost_basis:.2f}.
Current price: ${current_price:.2f} (down {drop_pct:.1f}% from our cost basis).

Recent news:
{headline_text}

Question: Is this drop STRUCTURAL (company-specific bad news: earnings miss, guidance cut, fraud, bankruptcy, secondary offering, legal issues) or TEMPORARY (market-wide sell-off, sector rotation, sympathy move, overreaction)?

Return ONLY valid JSON: {{"decision": "structural"}} or {{"decision": "temporary"}} or {{"decision": "normal"}}

Rules:
- structural: company-specific news that permanently impairs the stock
- temporary: macro/sector/emotional event, stock likely recovers
- normal: no clear news or ambiguous — default to selling covered call"""

        raw    = ask_ai(prompt, max_tokens=100)
        parsed = parse_ai_json(raw)
        decision = str(parsed.get("decision", "normal")).lower()
        if decision not in ("structural", "temporary", "normal"):
            decision = "normal"
        logger.info(f"Wheel AI assignment decision: {symbol} drop={drop_pct:.1f}% → {decision}")
        return decision

    except Exception as e:
        logger.warning(f"Wheel AI assignment decision failed ({e}) — defaulting to normal")
        return "normal"


# ── Scanner ───────────────────────────────────────────────────────────────────

def scan_opportunities() -> list[dict]:
    """
    Fully automatic. Called by scheduler Mon + Wed 9:45 AM ET.
    Reads AI-discovered universe from DB — no hardcoded watchlist.
    Market-hours guard prevents execution on holidays.
    """
    regime = _get_current_regime()
    active = get_active_wheel_positions()
    active_symbols = {p["symbol"] for p in active}

    if len(active) >= MAX_ACTIVE_POSITIONS:
        logger.info(f"Wheel: at max positions ({MAX_ACTIVE_POSITIONS}) — skipping scan")
        return []

    from services.wheel_universe import get_active_universe
    universe = get_active_universe()
    if not universe:
        logger.warning("Wheel scan: universe empty — Sunday refresh not yet run")
        return []

    today = date.today()
    expiry_min = today + timedelta(days=MIN_DTE)
    expiry_max = today + timedelta(days=MAX_DTE)
    opportunities = []
    sector_counts = _active_sector_counts()

    trading_client = _get_wheel_trading_client()
    data_client = _get_wheel_data_client()
    opts_client = _get_wheel_options_client()

    # otm_depth: upper bound for strike/price ratio. Optimizer adjusts 0.92–0.97
    # based on win rate — lower when too many assignments, higher when doing well.
    try:
        from services.db import cache_get as _cg3
        _otm_depth = float((_cg3("wheel:adaptive_thresholds") or {}).get("otm_depth", 0.95))
        _otm_depth = max(0.88, min(0.97, _otm_depth))  # hard bounds
    except Exception:
        _otm_depth = 0.95

    # Pre-fetch earnings dates for the whole universe in one AI call (cheap)
    earnings_within_14d = _get_earnings_within_days(universe, days=14)

    # Pre-fetch MA20 for all symbols in one batch call — trend filter
    ma20_map = _get_ma20_batch(universe, data_client)

    # Regime delta ceiling — hard filter, not just logging
    regime_delta_max = _regime_adjusted_delta(regime) + DELTA_MAX_BUFFER

    for symbol in universe:
        if symbol in active_symbols:
            continue

        # ── Earnings avoidance ────────────────────────────────────────────────
        if symbol in earnings_within_14d:
            logger.info(f"Wheel skip {symbol}: earnings within 14 days ({earnings_within_14d[symbol]})")
            continue

        # ── Sector diversification ────────────────────────────────────────────
        sector_ok, sector_reason = _sector_allows(symbol, sector_counts)
        if not sector_ok:
            logger.debug(f"Wheel scan skip {symbol}: {sector_reason}")
            continue

        try:
            from alpaca.data.requests import StockLatestQuoteRequest
            q = data_client.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=symbol)
            ).get(symbol)
            if not q:
                continue
            stock_price = float((q.ask_price + q.bid_price) / 2)
            if stock_price <= 0:
                continue

            # ── Underlying trend filter — stock must be above 20-day MA ──────
            # Selling puts on a downtrending stock = almost certain assignment.
            # MA20 = 0.0 means no data → treat as neutral, don't block.
            ma20 = ma20_map.get(symbol, 0.0)
            if ma20 > 0 and stock_price < ma20:
                logger.debug(f"Wheel skip {symbol}: price ${stock_price:.2f} < MA20 ${ma20:.2f} (downtrend)")
                continue

            from alpaca.trading.requests import GetOptionContractsRequest
            from alpaca.trading.enums import ContractType
            contracts_resp = trading_client.get_option_contracts(
                GetOptionContractsRequest(
                    underlying_symbols=[symbol],
                    type=ContractType.PUT,
                    expiration_date_gte=str(expiry_min),
                    expiration_date_lte=str(expiry_max),
                )
            )
            if not contracts_resp or not contracts_resp.option_contracts:
                continue

            # Sort: prefer contracts closest to TARGET_DTE (45 days)
            sorted_contracts = sorted(
                contracts_resp.option_contracts,
                key=lambda c: abs((c.expiration_date - today).days - TARGET_DTE)
            )

            best = None
            best_score = 0.0
            atm_iv_recorded = False
            # Track IV at ~30-DTE and ~45-DTE for term structure check
            iv_by_dte: dict[str, float] = {}   # "30" or "45" → iv value

            from alpaca.data.requests import OptionSnapshotRequest
            for contract in sorted_contracts:
                try:
                    strike = float(contract.strike_price)
                    ratio = strike / stock_price
                    if not (0.75 <= ratio <= _otm_depth):
                        continue

                    # ── OI filter ─────────────────────────────────────────────
                    oi = int(getattr(contract, "open_interest", 0) or 0)
                    if oi < 50:
                        continue

                    snap = opts_client.get_option_snapshot(
                        OptionSnapshotRequest(symbol_or_symbols=contract.symbol)
                    ).get(contract.symbol)
                    if not snap or not snap.latest_quote:
                        continue

                    ask = float(snap.latest_quote.ask_price or 0)
                    bid = float(snap.latest_quote.bid_price or 0)
                    if ask <= 0 or bid <= 0:
                        continue

                    # ── Bid-ask spread filter ─────────────────────────────────
                    # Wide spread = we give up too much on entry. Skip if > 30%.
                    spread_pct = (ask - bid) / ask
                    if spread_pct > MAX_SPREAD_PCT:
                        logger.debug(f"Wheel skip {symbol} {contract.symbol}: spread {spread_pct:.0%} > {MAX_SPREAD_PCT:.0%}")
                        continue

                    # ── Option volume filter ──────────────────────────────────
                    # Zero volume today = nobody trading it → our order moves the market.
                    opt_vol = int(getattr(snap.daily_bar, "volume", 0) or 0) if snap.daily_bar else 0
                    if opt_vol < MIN_OPTION_VOLUME:
                        logger.debug(f"Wheel skip {symbol} {contract.symbol}: option volume {opt_vol} < {MIN_OPTION_VOLUME}")
                        continue

                    premium = (ask + bid) / 2

                    # ── Minimum dollar premium filter ─────────────────────────
                    # Low-priced stocks can have good % yield but tiny dollar premium.
                    # Commission + slippage erases the profit.
                    dollar_premium = premium * 100
                    if dollar_premium < MIN_DOLLAR_PREMIUM:
                        logger.debug(f"Wheel skip {symbol} {contract.symbol}: dollar premium ${dollar_premium:.0f} < ${MIN_DOLLAR_PREMIUM:.0f}")
                        continue

                    prem_yield = premium / strike
                    if prem_yield < MIN_PREMIUM_YIELD:
                        continue

                    # Get IV + delta from greeks
                    iv = 0.0
                    real_delta = None
                    try:
                        greeks = getattr(snap, "greeks", None)
                        if greeks:
                            iv = float(getattr(greeks, "implied_volatility", 0) or 0)
                            real_delta = abs(float(getattr(greeks, "delta", 0) or 0))
                    except Exception:
                        pass

                    # ── Delta hard filter ─────────────────────────────────────
                    # High delta = nearly ATM = high assignment probability.
                    # Cap at regime_delta + buffer (e.g. neutral: 0.25+0.10 = 0.35 max).
                    if real_delta and real_delta > regime_delta_max:
                        logger.debug(f"Wheel skip {symbol} {contract.symbol}: delta {real_delta:.2f} > {regime_delta_max:.2f}")
                        continue

                    # Record IV for rank history (once per symbol per scan)
                    if iv > 0 and not atm_iv_recorded:
                        _record_iv(symbol, iv)
                        atm_iv_recorded = True

                    # Track IV by DTE bucket for term structure check
                    dte = (contract.expiration_date - today).days
                    if iv > 0:
                        if 25 <= dte <= 35 and "30" not in iv_by_dte:
                            iv_by_dte["30"] = iv
                        elif 40 <= dte <= 50 and "45" not in iv_by_dte:
                            iv_by_dte["45"] = iv

                    # IV rank filter
                    iv_ok, iv_reason = _iv_passes_filter(symbol, iv)
                    if not iv_ok:
                        logger.debug(f"Wheel scan skip {symbol}: {iv_reason}")
                        continue

                    annual_yield = prem_yield * (365 / max(dte, 1))

                    # Score = annual yield weighted by DTE proximity to 45
                    dte_penalty = abs(dte - TARGET_DTE) / TARGET_DTE
                    score = annual_yield * (1 - dte_penalty * 0.2)

                    # ── IV term structure soft penalty ────────────────────────
                    # Backwardation (30-DTE IV > 45-DTE IV × 1.05) means premium
                    # decays faster than expected. Penalize 20% — don't block entirely,
                    # only skip if something better is available.
                    if iv_by_dte.get("30") and iv_by_dte.get("45"):
                        if iv_by_dte["30"] > iv_by_dte["45"] * 1.05:
                            score *= 0.80
                            logger.debug(f"Wheel {symbol}: IV backwardation (30d={iv_by_dte['30']:.2f} > 45d={iv_by_dte['45']:.2f}) — score penalized 20%")

                    if score > best_score:
                        best_score = score
                        best = {
                            "symbol":            symbol,
                            "stock_price":       round(stock_price, 2),
                            "ma20":              round(ma20, 2),
                            "contract":          contract.symbol,
                            "strike":            strike,
                            "expiry":            str(contract.expiration_date),
                            "dte":               dte,
                            "premium":           round(premium, 2),
                            "bid":               round(bid, 2),
                            "ask":               round(ask, 2),
                            "spread_pct":        round(spread_pct * 100, 1),
                            "open_interest":     oi,
                            "option_volume":     opt_vol,
                            "dollar_premium":    round(dollar_premium, 2),
                            "premium_yield_pct": round(prem_yield * 100, 2),
                            "annual_yield_pct":  round(annual_yield * 100, 1),
                            "collateral":        round(strike * CONTRACTS_PER_TRADE * 100, 2),
                            "contracts":         CONTRACTS_PER_TRADE,
                            "iv":                round(iv * 100, 1) if iv else None,
                            "delta":             round(real_delta, 3) if real_delta else None,
                            "iv_reason":         iv_reason,
                            "sector":            SECTOR_MAP.get(symbol, "other"),
                            "regime":            regime,
                            "mode":              "paper" if _is_paper() else "live",
                        }
                except Exception:
                    continue

            if best:
                opportunities.append(best)
                logger.info(
                    f"Wheel opp: {symbol} ${best['strike']} put exp {best['expiry']} "
                    f"DTE={best['dte']} | ${best['premium']}×{CONTRACTS_PER_TRADE} "
                    f"(${best['dollar_premium']:.0f}/contract | {best['premium_yield_pct']}% / {best['annual_yield_pct']}% annual) "
                    f"IV={best['iv']}% delta={best['delta']} spread={best['spread_pct']}% "
                    f"OI={best['open_interest']} vol={best['option_volume']} sector={best['sector']}"
                )
        except Exception as e:
            logger.error(f"Wheel scan {symbol}: {e}")

    opportunities.sort(key=lambda x: x["annual_yield_pct"], reverse=True)
    return opportunities


# ── Order execution ───────────────────────────────────────────────────────────

def execute_put(opportunity: dict, cycle_contracts: Optional[int] = None) -> Optional[dict]:
    """
    Place cash-secured put order.

    cycle_contracts: VIX-based override from run_wheel_cycle (1 in high-fear).
                     Takes priority over optimizer's contracts_override.
                     Falls back to optimizer value, then CONTRACTS_PER_TRADE default.
    """
    try:
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderType

        # Priority: VIX cycle override > optimizer adaptive > default
        if cycle_contracts and 1 <= cycle_contracts <= 3:
            qty = cycle_contracts
        else:
            try:
                from services.db import cache_get as _cg2
                _co = (_cg2("wheel:adaptive_thresholds") or {}).get("contracts_override")
                qty = int(_co) if _co and 1 <= int(_co) <= 3 else CONTRACTS_PER_TRADE
            except Exception:
                qty = CONTRACTS_PER_TRADE

        # ── Bug #1 fix: buying power check ───────────────────────────────────
        # Each put requires strike × 100 × qty in cash collateral.
        # Skip if we'd use more than 90% of remaining buying power.
        collateral_needed = opportunity["strike"] * 100 * qty
        try:
            account = _get_wheel_trading_client().get_account()
            # options_buying_power is the correct field for options collateral checks
            options_bp = float(getattr(account, "options_buying_power", None) or account.cash)
            if collateral_needed > options_bp * 0.90:
                logger.warning(
                    f"Wheel skip {opportunity['symbol']}: collateral ${collateral_needed:,.0f} "
                    f"> 90% of options buying power ${options_bp:,.0f}"
                )
                return None
        except Exception as bp_err:
            logger.warning(f"Wheel buying power check failed ({bp_err}), proceeding anyway")

        # Limit price: bid by default (patient); optimizer sets "ask" when fill_rate < 70%.
        try:
            from services.db import cache_get as _cg
            _adaptive = _cg("wheel:adaptive_thresholds") or {}
            _bias = _adaptive.get("limit_price_bias", "bid")
        except Exception:
            _bias = "bid"
        if _bias == "ask":
            limit_px = round(opportunity.get("ask", opportunity["premium"]), 2)
        else:
            limit_px = round(opportunity.get("bid", opportunity["premium"]), 2)

        order = _get_wheel_trading_client().submit_order(LimitOrderRequest(
            symbol=opportunity["contract"],
            qty=qty,
            side=OrderSide.SELL,
            type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_px,
        ))
        logger.info(
            f"Wheel PUT: {opportunity['symbol']} ${opportunity['strike']} exp {opportunity['expiry']} "
            f"×{qty} contracts @ ${limit_px} (bid) OI={opportunity.get('open_interest','?')} | {order.id}"
        )

        # Record as order_pending — only promote to active after confirmed fill
        pos_id = _open_wheel_position(
            symbol=opportunity["symbol"],
            phase="put_open",
            put_contract=opportunity["contract"],
            put_strike=opportunity["strike"],
            put_expiry=opportunity["expiry"],
            put_premium=limit_px,  # use actual limit price, not mid
            put_order_id=str(order.id),
            regime=_get_current_regime(),
        )
        _update_wheel_position(pos_id, status="order_pending", notes=f"contracts_remaining:{qty}")
        return {"order_id": str(order.id), "position_db_id": pos_id, "contracts": qty, **opportunity}
    except Exception as e:
        logger.error(f"Wheel execute_put: {e}")
        return None


def execute_covered_call(position: dict) -> Optional[dict]:
    """Sell covered call above cost basis after assignment."""
    try:
        from alpaca.trading.requests import GetOptionContractsRequest, LimitOrderRequest
        from alpaca.trading.enums import ContractType, OrderSide, TimeInForce, OrderType

        symbol = position["symbol"]
        cost_basis = position.get("cost_basis")
        if not cost_basis:
            return None

        target_strike = float(cost_basis) * (1 + ASSIGNMENT_CALL_BUFFER)
        today = date.today()
        client = _get_wheel_trading_client()

        contracts = client.get_option_contracts(GetOptionContractsRequest(
            underlying_symbols=[symbol],
            type=ContractType.CALL,
            expiration_date_gte=str(today + timedelta(days=MIN_DTE)),
            expiration_date_lte=str(today + timedelta(days=MAX_DTE)),
        ))
        if not contracts or not contracts.option_contracts:
            return None

        opts_client = _get_wheel_options_client()
        best = None
        best_prem = 0.0

        for contract in contracts.option_contracts:
            strike = float(contract.strike_price)
            if strike < target_strike:
                continue
            try:
                from alpaca.data.requests import OptionSnapshotRequest
                snap = opts_client.get_option_snapshot(
                    OptionSnapshotRequest(symbol_or_symbols=contract.symbol)
                ).get(contract.symbol)
                if not snap or not snap.latest_quote:
                    continue
                ask = float(snap.latest_quote.ask_price or 0)
                bid = float(snap.latest_quote.bid_price or 0)
                prem = (ask + bid) / 2
                if prem > best_prem:
                    best_prem = prem
                    best = {"contract_symbol": contract.symbol, "strike": strike,
                            "expiry": str(contract.expiration_date), "premium": prem}
            except Exception:
                continue

        if not best:
            return None

        # Match qty to shares held — assignment gives 100 shares per put contract.
        # If 2 puts were sold and both assigned → 200 shares → sell 2 calls.
        shares = int(position.get("shares_qty") or 100)
        call_qty = max(1, shares // 100)   # 100 shares = 1 contract

        order = client.submit_order(LimitOrderRequest(
            symbol=best["contract_symbol"],
            qty=call_qty,
            side=OrderSide.SELL,
            type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=round(best["premium"] * 0.98, 2),
        ))
        logger.info(
            f"Wheel CALL: {symbol} ${best['strike']} exp {best['expiry']} "
            f"×{call_qty} contracts | {order.id}"
        )

        prev = float(position.get("total_premium_collected") or 0)
        _update_wheel_position(
            position["id"],
            phase="call_open",
            call_contract=best["contract_symbol"],
            call_strike=best["strike"],
            call_expiry=best["expiry"],
            call_premium=best["premium"],
            call_order_id=str(order.id),
            total_premium_collected=prev + best["premium"] * call_qty * 100,
        )
        return {"order_id": str(order.id), "contracts": call_qty, **best}
    except Exception as e:
        logger.error(f"Wheel execute_covered_call: {e}")
        return None


def _buy_to_close(position: dict, contract: str, current_price: float, reason: str):
    """Close an option position early (take profit or stop loss)."""
    try:
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderType

        order = _get_wheel_trading_client().submit_order(LimitOrderRequest(
            symbol=contract,
            qty=1,
            side=OrderSide.BUY,
            type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=round(current_price * 1.02, 2),  # 2% above mid to fill
        ))
        logger.info(f"Wheel BUY-TO-CLOSE: {position['symbol']} {reason} | {order.id}")
        return str(order.id)
    except Exception as e:
        logger.error(f"Wheel buy-to-close error: {e}")
        return None


# ── Stop-loss on assigned shares ─────────────────────────────────────────────

# If assigned and stock drops more than this % below cost basis → sell shares + exit cycle.
# Prevents a single bad assignment from wiping out weeks of premium income.
ASSIGNMENT_STOP_LOSS_PCT = float(_os.environ.get("WHEEL_STOP_LOSS_PCT", "0.20"))  # 20% below cost = exit

def check_assignment_stop_loss():
    """
    Must-do #10 — stop-loss on assigned shares.

    After assignment we hold 100-200 shares. If the stock drops 20%+ below
    cost basis, sell shares immediately and mark cycle as completed with a loss.
    Without this, one bad assignment can erase 10 cycles of premium income.

    Example: assigned SOFI @ $18, drops to $14.40 (-20%) → sell, take -$720 loss.
    Better than riding it to $10 (-$1,600 loss).
    """
    if not _market_is_open():
        return

    active = get_active_wheel_positions()
    assigned = [p for p in active if p["phase"] in ("assigned", "assigned_waiting") and p.get("cost_basis")]
    if not assigned:
        return

    try:
        from alpaca.data.requests import StockLatestQuoteRequest
        data_client = _get_wheel_data_client()
    except Exception:
        return

    now = datetime.now(timezone.utc).isoformat()

    for pos in assigned:
        try:
            symbol = pos["symbol"]
            cost = float(pos["cost_basis"])
            stop_price = cost * (1 - ASSIGNMENT_STOP_LOSS_PCT)

            q = data_client.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=symbol)
            ).get(symbol)
            if not q:
                continue
            current_price = float((q.ask_price + q.bid_price) / 2)
            if current_price <= 0:
                continue

            if current_price <= stop_price:
                loss_pct = (current_price - cost) / cost * 100
                shares = pos.get("shares_qty") or 100
                loss_dollars = (current_price - cost) * shares

                logger.warning(
                    f"Wheel STOP-LOSS: {symbol} cost ${cost:.2f} → now ${current_price:.2f} "
                    f"({loss_pct:.1f}%) ≤ stop ${stop_price:.2f} — selling {shares} shares"
                )

                # Sell shares via market order
                try:
                    from alpaca.trading.requests import MarketOrderRequest
                    from alpaca.trading.enums import OrderSide, TimeInForce
                    order = _get_wheel_trading_client().submit_order(MarketOrderRequest(
                        symbol=symbol,
                        qty=shares,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY,
                    ))
                    prev_prem = float(pos.get("total_premium_collected") or 0)
                    net_result = prev_prem + loss_dollars  # premium collected minus share loss

                    _update_wheel_position(
                        pos["id"],
                        phase="completed",
                        status="stop_loss",
                        realized_pl=net_result,
                        closed_at=now,
                        notes=(
                            f"STOP-LOSS: assigned ${cost:.2f} → sold ${current_price:.2f} "
                            f"({loss_pct:.1f}%). Share loss: ${loss_dollars:.0f}. "
                            f"Premium collected: ${prev_prem:.0f}. Net: ${net_result:.0f}"
                        ),
                    )
                    logger.warning(
                        f"Wheel STOP-LOSS executed: {symbol} | share loss ${loss_dollars:.0f} "
                        f"| premium buffer ${prev_prem:.0f} | net ${net_result:.0f} | order {order.id}"
                    )
                except Exception as sell_err:
                    logger.error(f"Wheel stop-loss sell failed for {symbol}: {sell_err}")

        except Exception as e:
            logger.error(f"Wheel stop-loss check pos {pos.get('id')}: {e}")


# ── Assignment + expiration + profit targets ──────────────────────────────────

def check_assignments():
    """
    Detect put assignments and decide next action based on drop severity.

    drop < BOUNCE_WAIT_DROP_PCT (3%)  → sell covered call immediately (normal path)
    drop 3–STRUCTURAL_DROP_PCT (10%)  → wait 1 day for bounce, then sell call
    drop > STRUCTURAL_DROP_PCT (10%)  → fetch news, AI decides:
                                         structural → stop-loss now
                                         temporary  → wait 1 day
                                         normal     → sell call immediately

    Also handles "assigned_waiting" positions — second-day check.
    """
    active = get_active_wheel_positions()

    # ── Second-day check: positions that waited yesterday for a bounce ────────
    waiting = [p for p in active if p["phase"] == "assigned_waiting"]
    for wp in waiting:
        # Always sell the covered call now — bounce window has passed
        logger.info(f"Wheel ASSIGNED-WAIT resolved: {wp['symbol']} — selling covered call now")
        execute_covered_call(wp)

    put_open = [p for p in active if p["phase"] == "put_open"]
    if not put_open:
        return

    try:
        held = {
            p.symbol: float(p.avg_entry_price)
            for p in _get_wheel_trading_client().get_all_positions()
            if int(float(p.qty)) >= 100
        }
    except Exception as e:
        logger.error(f"Wheel check_assignments: {e}")
        return

    try:
        from alpaca.data.requests import StockLatestQuoteRequest
        data_client = _get_wheel_data_client()
    except Exception:
        data_client = None

    for wp in put_open:
        if wp["symbol"] not in held:
            continue

        cost = held[wp["symbol"]]
        symbol = wp["symbol"]
        logger.info(f"Wheel ASSIGNED: {symbol} @ ${cost:.2f}")

        prev     = float(wp.get("total_premium_collected") or 0)
        put_prem = float(wp.get("put_premium") or 0)
        _update_wheel_position(
            wp["id"],
            phase="assigned",
            cost_basis=cost,
            total_premium_collected=prev + put_prem * 100,
        )
        wp["cost_basis"] = cost

        # Get current price to measure drop
        current_price = cost  # default: assume no drop (safe fallback)
        if data_client:
            try:
                q = data_client.get_stock_latest_quote(
                    StockLatestQuoteRequest(symbol_or_symbols=symbol)
                ).get(symbol)
                if q:
                    current_price = float((q.ask_price + q.bid_price) / 2)
            except Exception:
                pass

        drop_pct = (cost - current_price) / cost if cost > 0 else 0.0

        # ── Decision tree ─────────────────────────────────────────────────────
        if drop_pct >= STRUCTURAL_DROP_PCT:
            # Sharp drop → check news → AI decides
            decision = _ai_assignment_decision(symbol, cost, current_price)
            if decision == "structural":
                # Don't write a covered call into a falling knife — stop-loss fires
                logger.warning(
                    f"Wheel ASSIGNMENT AI: {symbol} drop={drop_pct:.1%} structural → "
                    f"skipping covered call, stop-loss will handle exit"
                )
                # Stop-loss will catch this on next check_assignment_stop_loss() run
                # Mark phase so we don't keep trying to sell a call
                _update_wheel_position(wp["id"], notes="ai_structural_drop:skip_call")
                continue
            elif decision == "temporary":
                logger.info(
                    f"Wheel ASSIGNMENT AI: {symbol} drop={drop_pct:.1%} temporary → "
                    f"waiting 1 day for bounce"
                )
                _update_wheel_position(wp["id"], phase="assigned_waiting")
                continue
            else:
                # "normal" — AI unsure, proceed with covered call
                logger.info(f"Wheel ASSIGNMENT AI: {symbol} → normal, selling call now")
                execute_covered_call(wp)

        elif drop_pct >= BOUNCE_WAIT_DROP_PCT:
            # Moderate drop → wait 1 day — intraday/next-day bounces are common
            logger.info(
                f"Wheel ASSIGNMENT: {symbol} drop={drop_pct:.1%} (3–10%) → "
                f"waiting 1 day for bounce before selling call"
            )
            _update_wheel_position(wp["id"], phase="assigned_waiting")

        else:
            # Normal assignment (< 3% drop) → sell covered call immediately
            execute_covered_call(wp)


def check_expirations():
    """Mark expired puts/calls as completed. Log P&L. Add to profit reserve."""
    today = date.today()
    active = get_active_wheel_positions()
    now = datetime.now(timezone.utc).isoformat()

    for pos in active:
        try:
            if pos["phase"] == "put_open" and pos.get("put_expiry"):
                expiry = pos["put_expiry"] if isinstance(pos["put_expiry"], date) \
                         else date.fromisoformat(str(pos["put_expiry"]))
                if expiry < today:
                    prem = float(pos.get("put_premium") or 0) * 100
                    logger.info(f"Wheel PUT expired worthless: {pos['symbol']} +${prem:.2f}")
                    _add_to_profit_reserve_if_configured(prem)
                    _update_wheel_position(
                        pos["id"],
                        phase="completed", status="completed",
                        total_premium_collected=prem,
                        realized_pl=prem,
                        closed_at=now,
                        notes="Put expired worthless — full premium kept",
                    )

            elif pos["phase"] == "call_open" and pos.get("call_expiry"):
                expiry = pos["call_expiry"] if isinstance(pos["call_expiry"], date) \
                         else date.fromisoformat(str(pos["call_expiry"]))
                if expiry < today:
                    total_prem = float(pos.get("total_premium_collected") or 0)
                    cost = float(pos.get("cost_basis") or 0)
                    call_strike = float(pos.get("call_strike") or 0)
                    stock_pl = (call_strike - cost) * 100 if call_strike > 0 else 0
                    total_pl = stock_pl + total_prem
                    logger.info(f"Wheel CALL expired: {pos['symbol']} ${stock_pl:.2f} stock + ${total_prem:.2f} prem = ${total_pl:.2f}")
                    _add_to_profit_reserve_if_configured(total_pl)
                    _update_wheel_position(
                        pos["id"],
                        phase="completed", status="completed",
                        realized_pl=total_pl,
                        closed_at=now,
                        notes=f"Full cycle complete. Stock: ${stock_pl:.2f}, Premiums: ${total_prem:.2f}",
                    )
        except Exception as e:
            logger.error(f"Wheel check_expirations pos {pos.get('id')}: {e}")


def _parse_contracts_remaining(notes: str) -> int:
    """Extract contracts_remaining from notes field. Format: 'contracts_remaining:N ...'"""
    try:
        for part in str(notes or "").split():
            if part.startswith("contracts_remaining:"):
                return int(part.split(":")[1])
    except Exception:
        pass
    return CONTRACTS_PER_TRADE  # default: assume full position


def _set_contracts_remaining(notes: str, remaining: int) -> str:
    """Update or insert contracts_remaining in the notes string."""
    import re
    tag = f"contracts_remaining:{remaining}"
    if "contracts_remaining:" in str(notes or ""):
        return re.sub(r"contracts_remaining:\d+", tag, notes)
    return f"{notes or ''} {tag}".strip()


def check_profit_targets():
    """
    Tiered profit close — 2 contracts per position:
      Contract 1: close at PROFIT_TIER_1 (50% remaining) → buy-to-close 1 contract
      Contract 2: close at PROFIT_TIER_2 (25% remaining) → buy-to-close 1 contract

    Contract 1 books quick profit + frees half collateral early.
    Contract 2 rides theta to near-zero for max capture.
    Market must be open to execute.
    """
    if not _market_is_open():
        return

    active = get_active_wheel_positions()
    opts_client = _get_wheel_options_client()
    now = datetime.now(timezone.utc).isoformat()

    from alpaca.data.requests import OptionSnapshotRequest

    for pos in active:
        try:
            phase = pos["phase"]

            # ── PUTS: tiered close ──────────────────────────────────────────
            if phase == "put_open" and pos.get("put_contract") and pos.get("put_premium"):
                original_prem = float(pos["put_premium"])
                snap = opts_client.get_option_snapshot(
                    OptionSnapshotRequest(symbol_or_symbols=pos["put_contract"])
                ).get(pos["put_contract"])
                if not snap or not snap.latest_quote:
                    continue
                ask = float(snap.latest_quote.ask_price or 0)
                bid = float(snap.latest_quote.bid_price or 0)
                current_prem = (ask + bid) / 2
                if current_prem <= 0:
                    continue

                decay_ratio = current_prem / original_prem
                contracts_left = _parse_contracts_remaining(pos.get("notes", ""))

                if contracts_left >= 2 and decay_ratio <= PROFIT_TIER_1:
                    # Close contract 1 at 50% profit
                    profit1 = (original_prem - current_prem) * 100
                    logger.info(
                        f"Wheel TIER-1 CLOSE (put): {pos['symbol']} "
                        f"orig ${original_prem:.2f} → ${current_prem:.2f} "
                        f"({decay_ratio*100:.0f}% remaining) | 1-of-2 profit ${profit1:.2f}"
                    )
                    order_id = _buy_to_close(pos, pos["put_contract"], current_prem,
                                             f"tier-1 50% profit (1-of-2)")
                    if order_id:
                        _add_to_profit_reserve_if_configured(profit1)
                        new_notes = _set_contracts_remaining(pos.get("notes", ""), 1)
                        new_notes += f" | tier1_closed_at:{decay_ratio*100:.0f}%"
                        _update_wheel_position(
                            pos["id"],
                            notes=new_notes,
                        )

                elif contracts_left == 1 and decay_ratio <= PROFIT_TIER_2:
                    # Close final contract at 25% remaining → position complete
                    profit2 = (original_prem - current_prem) * 100
                    total_prem = float(pos.get("total_premium_collected") or 0) + profit2
                    logger.info(
                        f"Wheel TIER-2 CLOSE (put): {pos['symbol']} "
                        f"${current_prem:.2f} remaining | final profit ${profit2:.2f} "
                        f"| total collected ${total_prem:.2f}"
                    )
                    order_id = _buy_to_close(pos, pos["put_contract"], current_prem,
                                             f"tier-2 25% final close")
                    if order_id:
                        _add_to_profit_reserve_if_configured(profit2)
                        _update_wheel_position(
                            pos["id"],
                            phase="completed", status="completed",
                            realized_pl=total_prem,
                            total_premium_collected=total_prem,
                            closed_at=now,
                            notes=f"Tiered close complete. Total: ${total_prem:.2f}",
                        )

            # ── CALLS: tiered close (same tiers) ───────────────────────────
            elif phase == "call_open" and pos.get("call_contract") and pos.get("call_premium"):
                original_prem = float(pos["call_premium"])
                snap = opts_client.get_option_snapshot(
                    OptionSnapshotRequest(symbol_or_symbols=pos["call_contract"])
                ).get(pos["call_contract"])
                if not snap or not snap.latest_quote:
                    continue
                ask = float(snap.latest_quote.ask_price or 0)
                bid = float(snap.latest_quote.bid_price or 0)
                current_prem = (ask + bid) / 2
                if current_prem <= 0:
                    continue

                decay_ratio = current_prem / original_prem
                contracts_left = _parse_contracts_remaining(pos.get("notes", ""))
                prev_prem = float(pos.get("total_premium_collected") or 0)

                if contracts_left >= 2 and decay_ratio <= PROFIT_TIER_1:
                    call_profit1 = (original_prem - current_prem) * 100
                    logger.info(
                        f"Wheel TIER-1 CLOSE (call): {pos['symbol']} "
                        f"${call_profit1:.2f} profit | 1-of-2"
                    )
                    order_id = _buy_to_close(pos, pos["call_contract"], current_prem,
                                             "tier-1 50% call profit")
                    if order_id:
                        _add_to_profit_reserve_if_configured(call_profit1)
                        new_notes = _set_contracts_remaining(pos.get("notes", ""), 1)
                        _update_wheel_position(
                            pos["id"],
                            total_premium_collected=prev_prem + call_profit1,
                            notes=new_notes,
                        )

                elif contracts_left == 1 and decay_ratio <= PROFIT_TIER_2:
                    call_profit2 = (original_prem - current_prem) * 100
                    logger.info(
                        f"Wheel TIER-2 CLOSE (call): {pos['symbol']} "
                        f"${call_profit2:.2f} | back to assigned → sell new call"
                    )
                    order_id = _buy_to_close(pos, pos["call_contract"], current_prem,
                                             "tier-2 25% final call close")
                    if order_id:
                        new_notes = _set_contracts_remaining("", CONTRACTS_PER_TRADE)
                        _update_wheel_position(
                            pos["id"],
                            phase="assigned",
                            call_contract=None,
                            call_order_id=None,
                            total_premium_collected=prev_prem + call_profit2,
                            notes=new_notes,
                        )
                        execute_covered_call({
                            **pos,
                            "total_premium_collected": prev_prem + call_profit2,
                        })

        except Exception as e:
            logger.error(f"Wheel check_profit_targets pos {pos.get('id')}: {e}")


def _add_to_profit_reserve_if_configured(amount: float):
    """Add portion of profit to wheel reserve if profit_reserve_pct > 0."""
    try:
        from services.db import cache_get
        risk = cache_get("user_pref:risk_settings") or {}
        reserve_pct = float(risk.get("profit_reserve_pct", 0))
        if reserve_pct > 0 and amount > 0:
            reserve_amount = round(amount * reserve_pct / 100, 2)
            _add_to_wheel_reserve(reserve_amount)
    except Exception:
        pass


# ── DTE Force Close ──────────────────────────────────────────────────────────

def check_dte_force_close():
    """
    Force close all open option positions at DTE ≤ DTE_FORCE_CLOSE (21 days).

    45/21-DTE rule: open at ~45 DTE, ride to 21 DTE where theta decay
    flattens out. At 21 DTE gamma risk spikes — close to avoid pin risk
    and free up capital for the next 45-DTE cycle.

    Applies to both puts (put_open) and calls (call_open).
    Market must be open.
    """
    if not _market_is_open():
        return

    active = get_active_wheel_positions()
    if not active:
        return

    today = date.today()
    now = datetime.now(timezone.utc).isoformat()
    opts_client = _get_wheel_options_client()
    from alpaca.data.requests import OptionSnapshotRequest

    for pos in active:
        try:
            # ── Puts ────────────────────────────────────────────────────
            if pos["phase"] == "put_open" and pos.get("put_expiry") and pos.get("put_contract"):
                expiry = date.fromisoformat(str(pos["put_expiry"]))
                dte = (expiry - today).days
                if dte > DTE_FORCE_CLOSE:
                    continue

                logger.info(
                    f"Wheel DTE-FORCE-CLOSE (put): {pos['symbol']} exp {expiry} "
                    f"DTE={dte} ≤ {DTE_FORCE_CLOSE} — force closing"
                )
                snap = opts_client.get_option_snapshot(
                    OptionSnapshotRequest(symbol_or_symbols=pos["put_contract"])
                ).get(pos["put_contract"])
                if not snap or not snap.latest_quote:
                    continue
                ask = float(snap.latest_quote.ask_price or 0)
                bid = float(snap.latest_quote.bid_price or 0)
                current_prem = (ask + bid) / 2
                if current_prem <= 0:
                    continue

                contracts_left = _parse_contracts_remaining(pos.get("notes", ""))
                original_prem = float(pos.get("put_premium") or 0)
                profit = (original_prem - current_prem) * contracts_left * 100
                prev_total = float(pos.get("total_premium_collected") or 0)

                order_id = _buy_to_close(pos, pos["put_contract"], current_prem,
                                         f"DTE-force (DTE={dte})")
                if order_id:
                    _add_to_profit_reserve_if_configured(profit)
                    _update_wheel_position(
                        pos["id"],
                        phase="completed", status="completed",
                        realized_pl=prev_total + profit,
                        total_premium_collected=prev_total + profit,
                        closed_at=now,
                        notes=f"DTE force close at DTE={dte}. Profit: ${profit:.2f}",
                    )

            # ── Calls ────────────────────────────────────────────────────
            elif pos["phase"] == "call_open" and pos.get("call_expiry") and pos.get("call_contract"):
                expiry = date.fromisoformat(str(pos["call_expiry"]))
                dte = (expiry - today).days
                if dte > DTE_FORCE_CLOSE:
                    continue

                logger.info(
                    f"Wheel DTE-FORCE-CLOSE (call): {pos['symbol']} exp {expiry} "
                    f"DTE={dte} ≤ {DTE_FORCE_CLOSE} — force closing"
                )
                snap = opts_client.get_option_snapshot(
                    OptionSnapshotRequest(symbol_or_symbols=pos["call_contract"])
                ).get(pos["call_contract"])
                if not snap or not snap.latest_quote:
                    continue
                ask = float(snap.latest_quote.ask_price or 0)
                bid = float(snap.latest_quote.bid_price or 0)
                current_prem = (ask + bid) / 2
                if current_prem <= 0:
                    continue

                contracts_left = _parse_contracts_remaining(pos.get("notes", ""))
                original_prem = float(pos.get("call_premium") or 0)
                call_profit = (original_prem - current_prem) * contracts_left * 100
                prev_total = float(pos.get("total_premium_collected") or 0)

                order_id = _buy_to_close(pos, pos["call_contract"], current_prem,
                                         f"DTE-force call (DTE={dte})")
                if order_id:
                    _add_to_profit_reserve_if_configured(call_profit)
                    new_notes = _set_contracts_remaining("", CONTRACTS_PER_TRADE)
                    _update_wheel_position(
                        pos["id"],
                        phase="assigned",
                        call_contract=None,
                        call_order_id=None,
                        total_premium_collected=prev_total + call_profit,
                        notes=new_notes,
                    )
                    # Back to holding shares → sell new call immediately
                    execute_covered_call({
                        **pos,
                        "total_premium_collected": prev_total + call_profit,
                    })

        except Exception as e:
            logger.error(f"Wheel check_dte_force_close pos {pos.get('id')}: {e}")


# ── Bear Call Spreads ─────────────────────────────────────────────────────────

def scan_bear_spreads() -> list:
    """
    Scan for bear call spread opportunities when regime is bearish.

    Bear call spread: sell OTM call + buy higher-strike call on same expiry.
    - Max profit = net credit (keeps if stock stays below short strike)
    - Max loss = spread width - credit (capped downside, unlike naked calls)
    - Used in bearish/sideways regime instead of cash-secured puts

    Returns list of spread opportunities without placing orders.
    """
    regime = _get_current_regime()
    if regime not in ("bearish", "bear"):
        logger.debug(f"Bear spreads: regime={regime} — skipping (not bearish)")
        return []

    from services.wheel_universe import get_active_universe
    universe = get_active_universe()  # already returns list[str]
    active_symbols = {p["symbol"] for p in get_active_wheel_positions()}
    today = date.today()
    expiry_min = today + timedelta(days=MIN_DTE)
    expiry_max = today + timedelta(days=MAX_DTE)
    spreads = []

    trading_client = _get_wheel_trading_client()
    data_client = _get_wheel_data_client()
    opts_client = _get_wheel_options_client()

    from alpaca.data.requests import StockLatestQuoteRequest, OptionSnapshotRequest
    from alpaca.trading.requests import GetOptionContractsRequest
    from alpaca.trading.enums import ContractType

    sector_counts = _active_sector_counts()

    for symbol in universe:
        if symbol in active_symbols:
            continue
        sector_ok, sector_reason = _sector_allows(symbol, sector_counts)
        if not sector_ok:
            continue

        try:
            q = data_client.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=symbol)
            ).get(symbol)
            if not q:
                continue
            stock_price = float((q.ask_price + q.bid_price) / 2)
            if stock_price <= 0:
                continue

            contracts_resp = trading_client.get_option_contracts(GetOptionContractsRequest(
                underlying_symbols=[symbol],
                type=ContractType.CALL,
                expiration_date_gte=str(expiry_min),
                expiration_date_lte=str(expiry_max),
            ))
            if not contracts_resp or not contracts_resp.option_contracts:
                continue

            # Group contracts by expiry — find best spread per expiry
            from collections import defaultdict
            by_expiry = defaultdict(list)
            for c in contracts_resp.option_contracts:
                by_expiry[c.expiration_date].append(c)

            best_spread = None
            best_credit = 0.0

            for expiry_date, exp_contracts in by_expiry.items():
                dte = (expiry_date - today).days
                # Prefer 45-DTE
                exp_contracts_sorted = sorted(exp_contracts, key=lambda c: float(c.strike_price))

                # Find short call: 5-10% OTM (bearish → sell closer to ATM than wheel puts)
                short_candidates = [
                    c for c in exp_contracts_sorted
                    if 1.03 <= float(c.strike_price) / stock_price <= 1.10
                ]
                if not short_candidates:
                    continue

                for short_contract in short_candidates[:3]:
                    short_strike = float(short_contract.strike_price)
                    # Long call: 5-10% above short strike (defines max loss)
                    long_candidates = [
                        c for c in exp_contracts_sorted
                        if 1.04 <= float(c.strike_price) / short_strike <= 1.12
                    ]
                    if not long_candidates:
                        continue
                    long_contract = long_candidates[0]
                    long_strike = float(long_contract.strike_price)

                    try:
                        short_snap = opts_client.get_option_snapshot(
                            OptionSnapshotRequest(symbol_or_symbols=short_contract.symbol)
                        ).get(short_contract.symbol)
                        long_snap = opts_client.get_option_snapshot(
                            OptionSnapshotRequest(symbol_or_symbols=long_contract.symbol)
                        ).get(long_contract.symbol)
                        if not short_snap or not long_snap:
                            continue

                        short_bid = float((short_snap.latest_quote.bid_price or 0))
                        long_ask = float((long_snap.latest_quote.ask_price or 0))
                        if short_bid <= 0 or long_ask <= 0:
                            continue

                        net_credit = short_bid - long_ask
                        if net_credit <= 0:
                            continue

                        spread_width = long_strike - short_strike
                        max_profit = net_credit * 100
                        max_loss = (spread_width - net_credit) * 100
                        risk_reward = max_profit / max_loss if max_loss > 0 else 0

                        if net_credit > best_credit and risk_reward >= 0.25:
                            best_credit = net_credit
                            best_spread = {
                                "symbol": symbol,
                                "stock_price": round(stock_price, 2),
                                "type": "bear_call_spread",
                                "short_contract": short_contract.symbol,
                                "short_strike": short_strike,
                                "long_contract": long_contract.symbol,
                                "long_strike": long_strike,
                                "expiry": str(expiry_date),
                                "dte": dte,
                                "net_credit": round(net_credit, 2),
                                "spread_width": round(spread_width, 2),
                                "max_profit": round(max_profit, 2),
                                "max_loss": round(max_loss, 2),
                                "risk_reward": round(risk_reward, 3),
                                "sector": SECTOR_MAP.get(symbol, "other"),
                                "regime": regime,
                                "mode": "paper" if _is_paper() else "live",
                            }
                    except Exception:
                        continue

            if best_spread:
                spreads.append(best_spread)
                logger.info(
                    f"Bear spread: {symbol} sell ${best_spread['short_strike']} "
                    f"/ buy ${best_spread['long_strike']} call "
                    f"exp {best_spread['expiry']} DTE={best_spread['dte']} "
                    f"net credit ${best_spread['net_credit']} "
                    f"R/R={best_spread['risk_reward']}"
                )

        except Exception as e:
            logger.error(f"Bear spread scan {symbol}: {e}")

    return spreads


def execute_bear_spread(spread: dict) -> Optional[dict]:
    """
    Execute a bear call spread: sell short call + buy long call simultaneously.

    Uses a limit order on the short leg; long leg is a protective buy.
    Net credit must be positive (verified in scan_bear_spreads).
    """
    try:
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderType

        client = _get_wheel_trading_client()

        # Sell the short call (credit leg) first
        short_order = client.submit_order(LimitOrderRequest(
            symbol=spread["short_contract"],
            qty=CONTRACTS_PER_TRADE,
            side=OrderSide.SELL,
            type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=round(spread["net_credit"] * 0.98, 2),
        ))

        # Buy the long call (protective leg) — if this fails, cancel the short immediately
        long_credit = spread["spread_width"] - spread["net_credit"]
        try:
            long_order = client.submit_order(LimitOrderRequest(
                symbol=spread["long_contract"],
                qty=CONTRACTS_PER_TRADE,
                side=OrderSide.BUY,
                type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                limit_price=round(long_credit * 1.02, 2),
            ))
        except Exception as long_err:
            # Bug #2 fix: long leg failed → cancel short leg to avoid naked short exposure
            logger.error(
                f"Wheel bear spread: long leg FAILED for {spread['symbol']} ({long_err}). "
                f"Cancelling short leg {short_order.id} to avoid naked short."
            )
            try:
                client.cancel_order_by_id(str(short_order.id))
                logger.info(f"Wheel bear spread: short leg {short_order.id} cancelled successfully")
            except Exception as cancel_err:
                logger.error(
                    f"Wheel bear spread: CRITICAL — could not cancel short leg {short_order.id}: {cancel_err}. "
                    f"Manual intervention required for {spread['symbol']}."
                )
            return None

        logger.info(
            f"Wheel BEAR SPREAD: {spread['symbol']} "
            f"sell ${spread['short_strike']} / buy ${spread['long_strike']} call "
            f"exp {spread['expiry']} | credit=${spread['net_credit']} "
            f"×{CONTRACTS_PER_TRADE} | short={short_order.id} long={long_order.id}"
        )

        pos_id = _open_wheel_position(
            symbol=spread["symbol"],
            phase="bear_spread_open",
            put_contract=spread["short_contract"],   # reuse put_contract field for short leg
            put_strike=spread["short_strike"],
            put_expiry=spread["expiry"],
            put_premium=spread["net_credit"],
            put_order_id=str(short_order.id),
            regime=_get_current_regime(),
        )
        _update_wheel_position(
            pos_id,
            notes=(
                f"bear_call_spread | long_contract:{spread['long_contract']} "
                f"long_strike:{spread['long_strike']} "
                f"long_order_id:{long_order.id} "
                f"max_loss:{spread['max_loss']} "
                f"contracts_remaining:{CONTRACTS_PER_TRADE}"
            )
        )
        return {
            "short_order_id": str(short_order.id),
            "long_order_id": str(long_order.id),
            "position_db_id": pos_id,
            **spread,
        }
    except Exception as e:
        logger.error(f"Wheel execute_bear_spread: {e}")
        return None


# ── IV Rank ───────────────────────────────────────────────────────────────────

def _record_iv(symbol: str, iv: float):
    """Store today's ATM IV reading for a symbol. Builds 52-week history."""
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO wheel_iv_history (symbol, recorded_at, atm_iv)
                VALUES (%s, %s, %s)
                ON CONFLICT (symbol, recorded_at) DO UPDATE SET atm_iv = EXCLUDED.atm_iv
            """, (symbol, date.today(), iv))
    except Exception as e:
        logger.debug(f"IV record {symbol}: {e}")


def _get_iv_rank(symbol: str, current_iv: float) -> float:
    """
    IV Rank = (current_iv - min_52w) / (max_52w - min_52w) × 100.
    Returns -1 when insufficient history (< 20 days) → caller uses absolute threshold.
    """
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return -1
        with conn.cursor() as cur:
            cur.execute("""
                SELECT MIN(atm_iv), MAX(atm_iv), COUNT(*)
                FROM wheel_iv_history
                WHERE symbol = %s AND recorded_at >= CURRENT_DATE - 365
            """, (symbol,))
            row = cur.fetchone()
        if not row or (row[2] or 0) < 20:
            return -1   # not enough history yet
        min_iv, max_iv = float(row[0]), float(row[1])
        if max_iv <= min_iv:
            return 50.0
        return round(((current_iv - min_iv) / (max_iv - min_iv)) * 100, 1)
    except Exception:
        return -1


def _iv_passes_filter(symbol: str, current_iv: float) -> tuple[bool, str]:
    """Returns (passes, reason). Fails only when IV is genuinely low."""
    if current_iv <= 0:
        return True, "no IV data"   # can't filter — allow
    rank = _get_iv_rank(symbol, current_iv)
    if rank == -1:
        # Not enough history — use absolute threshold
        if current_iv < MIN_IV_ABSOLUTE:
            return False, f"IV {current_iv*100:.0f}% < {MIN_IV_ABSOLUTE*100:.0f}% (no rank history yet)"
        return True, f"IV {current_iv*100:.0f}% (no rank yet, above absolute floor)"
    if rank < MIN_IV_RANK:
        return False, f"IV rank {rank:.0f} < {MIN_IV_RANK} (options cheap, skip)"
    return True, f"IV rank {rank:.0f}"


# ── Sector diversification ────────────────────────────────────────────────────

def _active_sector_counts() -> dict[str, int]:
    """Count active positions per sector."""
    active = get_active_wheel_positions()
    counts: dict[str, int] = {}
    for pos in active:
        sector = SECTOR_MAP.get(pos["symbol"], "other")
        counts[sector] = counts.get(sector, 0) + 1
    return counts


def _sector_allows(symbol: str, sector_counts: dict[str, int]) -> tuple[bool, str]:
    """Returns (allowed, reason)."""
    sector = SECTOR_MAP.get(symbol, "other")
    count = sector_counts.get(sector, 0)
    if count >= MAX_PER_SECTOR:
        return False, f"sector '{sector}' full ({count}/{MAX_PER_SECTOR})"
    return True, f"sector '{sector}' {count+1}/{MAX_PER_SECTOR}"


# ── Full cycle ─────────────────────────────────────────────────────────────────

_LAST_SCAN_KEY = "wheel:last_scan_date"   # cache key — tracks when we last placed new puts
SCAN_MAX_GAP_DAYS = 4                      # if no scan in 4+ days and market is open → scan today


def _should_scan_today() -> bool:
    """
    Scan on Mon + Wed normally.
    But if Monday was a holiday (or any scan day missed), scan on the
    next open day automatically — never go more than SCAN_MAX_GAP_DAYS without scanning.
    This means Tuesday after a Monday holiday = scan day.
    """
    try:
        from services.db import cache_get
        last_scan = cache_get(_LAST_SCAN_KEY)   # stored as "YYYY-MM-DD"
        if not last_scan:
            return True   # never scanned before → scan now
        last = date.fromisoformat(str(last_scan))
        gap = (date.today() - last).days
        if gap >= SCAN_MAX_GAP_DAYS:
            logger.info(f"Wheel: {gap} days since last scan (>{SCAN_MAX_GAP_DAYS}) → scanning today")
            return True
    except Exception:
        return True   # on error → scan to be safe

    # Normal schedule: Mon (0) and Wed (2)
    weekday = datetime.now(timezone.utc).weekday()
    return weekday in (0, 1, 2, 3, 4)  # scan every market day


def _record_scan_date():
    try:
        from services.db import cache_set
        cache_set(_LAST_SCAN_KEY, str(date.today()), 30 * 24 * 3600)
    except Exception:
        pass


_LAST_REGIME_KEY = "wheel:last_known_regime"   # detects regime flips → triggers immediate universe refresh

def _check_regime_changed_and_refresh() -> bool:
    """
    Detect regime flip (e.g. bull → bear) and trigger immediate universe refresh.
    Returns True if a refresh was triggered.

    Why: universe composition changes dramatically between regimes.
    Bull → defensive/stable names (SOFI, BAC) → can stay.
    Bull → Bear → need to re-evaluate immediately, not wait until Sunday.
    """
    try:
        from services.db import cache_get, cache_set
        current_regime = _get_current_regime()
        last_regime = cache_get(_LAST_REGIME_KEY)

        if last_regime is None:
            # First run — store and move on
            cache_set(_LAST_REGIME_KEY, current_regime, 7 * 24 * 3600)
            return False

        if str(last_regime).lower() != current_regime.lower():
            logger.info(
                f"Wheel: regime flipped {last_regime} → {current_regime} — "
                f"triggering immediate universe refresh"
            )
            cache_set(_LAST_REGIME_KEY, current_regime, 7 * 24 * 3600)
            try:
                from services.wheel_universe import refresh_universe
                result = refresh_universe()
                logger.info(f"Wheel: regime-change refresh complete — {len(result)} stocks")
                return True
            except Exception as e:
                logger.error(f"Wheel regime-change refresh failed: {e}")
    except Exception:
        pass
    return False


def _reconcile_pending_orders():
    """
    Check all order_pending positions against Alpaca order status.
    - filled   → mark active, record actual fill price as total_premium_collected
    - expired/cancelled → delete from DB (phantom position cleanup)
    Called every cycle before scanning new opportunities.
    """
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, symbol, put_order_id, put_strike
                FROM wheel_positions
                WHERE status = 'order_pending' AND put_order_id IS NOT NULL
            """)
            cols = [d[0] for d in cur.description]
            pending = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"Wheel reconcile fetch: {e}")
        return

    if not pending:
        return

    client = _get_wheel_trading_client()
    for pos in pending:
        try:
            order = client.get_order_by_id(pos["put_order_id"])
            status = str(order.status).lower()

            if "filled" in status or status == "partially_filled":
                fill_price = float(order.filled_avg_price or pos.get("put_strike", 0))
                filled_qty = int(float(order.filled_qty or 0))
                premium_collected = fill_price * 100 * filled_qty
                _update_wheel_position(
                    pos["id"],
                    status="active",
                    total_premium_collected=premium_collected,
                )
                logger.info(
                    f"Wheel reconcile: {pos['symbol']} order FILLED "
                    f"@ ${fill_price} ×{filled_qty} = ${premium_collected:.2f} premium → active"
                )

            elif status in ("expired", "cancelled", "canceled", "rejected"):
                try:
                    from services.db import _get_conn
                    c2 = _get_conn()
                    with c2.cursor() as cur:
                        cur.execute("DELETE FROM wheel_positions WHERE id = %s", (pos["id"],))
                    c2.commit()
                    logger.info(
                        f"Wheel reconcile: {pos['symbol']} order {status} — "
                        f"removed phantom position {pos['id']}"
                    )
                except Exception as del_err:
                    logger.error(f"Wheel reconcile delete {pos['id']}: {del_err}")

        except Exception as e:
            logger.warning(f"Wheel reconcile order {pos['put_order_id']}: {e}")


def run_wheel_cycle():
    """
    Fully automatic. Called by scheduler Mon-Fri 9:45 AM ET.

    Every day:  check expirations, profit targets, assignments
    Scan days:  place new puts — Mon + Wed normally,
                OR next open day if previous scan day was a holiday.
                Gap cap: never go more than 4 calendar days without scanning.

    Holiday/half-day safe: checks Alpaca market clock before any order.
    If market closed → skips all orders silently, logs next open time.
    """
    is_open = _market_is_open()
    logger.info(f"Wheel cycle starting — market_open={is_open}")

    # ── Step 0: Regime-change check → immediate universe refresh if flipped ──
    try:
        _check_regime_changed_and_refresh()
    except Exception as e:
        logger.error(f"Wheel regime-change check: {e}")

    # ── Step 1: Reconcile pending orders — confirm fills, remove expired ghosts ─
    try:
        _reconcile_pending_orders()
    except Exception as e:
        logger.error(f"Wheel reconcile: {e}")

    # ── Step 2: Always check expirations (read-only DB, no orders) ────────────
    try:
        check_expirations()
    except Exception as e:
        logger.error(f"Wheel expirations: {e}")

    if not is_open:
        next_open = _next_market_open()
        logger.info(f"Wheel: market closed — skipping orders. Next open: {next_open}")
        return

    # ── Step 3: Stop-loss on assigned shares (runs before anything else) ─────
    try:
        check_assignment_stop_loss()
    except Exception as e:
        logger.error(f"Wheel assignment stop-loss: {e}")

    # ── Step 4: DTE force close (45→21 rule — must run before profit targets) ─
    try:
        check_dte_force_close()
    except Exception as e:
        logger.error(f"Wheel DTE force close: {e}")

    # ── Step 5: Tiered profit targets (50% then 25%) ──────────────────────
    try:
        check_profit_targets()
    except Exception as e:
        logger.error(f"Wheel profit targets: {e}")

    # ── Step 6: Check assignments → sell covered calls ─────────────────────
    try:
        check_assignments()
    except Exception as e:
        logger.error(f"Wheel assignments: {e}")

    # ── Step 7: Open new positions on scan days ────────────────────────────
    if _should_scan_today():
        try:
            regime = _get_current_regime()

            # ── Weekly loss cap — pause new trades if we've lost too much this week ──
            try:
                weekly_loss   = _get_weekly_realized_loss()
                account_value = 25000.0  # fallback
                try:
                    account_value = float(_get_wheel_trading_client().get_account().portfolio_value)
                except Exception:
                    pass
                loss_cap = account_value * WEEKLY_LOSS_CAP_PCT
                if weekly_loss < -loss_cap:
                    logger.warning(
                        f"Wheel: weekly loss cap hit (${weekly_loss:.0f} > "
                        f"-${loss_cap:.0f} / {WEEKLY_LOSS_CAP_PCT:.0%} of ${account_value:.0f}) "
                        f"— pausing new positions this week"
                    )
                    return
            except Exception as cap_err:
                logger.warning(f"Wheel weekly loss cap check failed ({cap_err}) — continuing")

            # ── VIX-aware sizing — size down contracts in high-fear, never block stocks ──
            vix_sizing      = _get_vix_sizing()
            cycle_contracts = vix_sizing["contracts"]
            max_pos_vix     = vix_sizing["max_positions"]

            active_count = len(get_active_wheel_positions())
            slots = min(MAX_ACTIVE_POSITIONS, max_pos_vix) - active_count
            placed = 0

            if slots > 0:
                logger.info(
                    f"Wheel: {regime} regime — scanning puts "
                    f"({slots} slots, {cycle_contracts} contracts/trade)"
                )
                opps = scan_opportunities()
                for opp in opps[:min(slots, MAX_ACTIVE_POSITIONS)]:
                    if execute_put(opp, cycle_contracts=cycle_contracts):
                        placed += 1

                # In bearish regime, also place 1 bear spread if slots remain
                if regime in ("bearish", "bear") and placed < slots:
                    logger.info(f"Wheel: bearish regime — also scanning bear spreads ({slots - placed} extra slots)")
                    spreads = scan_bear_spreads()
                    for spread in spreads[:1]:
                        if execute_bear_spread(spread):
                            placed += 1
                    logger.info(f"Wheel puts: {placed} placed, {len(opps)} opportunities found")
            else:
                logger.info(f"Wheel: all {MAX_ACTIVE_POSITIONS} slots full — no new positions")

            _record_scan_date()
        except Exception as e:
            logger.error(f"Wheel scan+execute: {e}")

    logger.info("Wheel cycle complete")


# ── iOS dashboard status ───────────────────────────────────────────────────────

def get_wheel_status() -> dict:
    regime = _get_current_regime()
    active = get_active_wheel_positions()
    summary = get_wheel_summary()

    for pos in active:
        for k, v in pos.items():
            if isinstance(v, (date, datetime)):
                pos[k] = v.isoformat()

    from services.wheel_universe import get_universe_details
    universe = get_universe_details()

    # Live account balance from Alpaca wheel account
    account_info = None   # None → null in JSON so Swift Optional decodes cleanly
    try:
        acct = _get_wheel_trading_client().get_account()
        account_info = {
            "portfolio_value": float(acct.portfolio_value),
            "cash":            float(acct.cash),
            "buying_power":    float(acct.buying_power),
            "equity":          float(acct.equity),
        }
    except Exception:
        pass  # Not fatal — dashboard still loads without balance row

    return {
        "regime": regime,
        "mode": "paper" if _is_paper() else "live",
        "base_url": settings.alpaca_wheel_base_url,
        "active_positions": active,
        "active_count": len(active),
        "max_positions": MAX_ACTIVE_POSITIONS,
        "summary": summary,
        "profit_reserve": _get_wheel_reserve(),
        "account": account_info,
        "universe": universe,
        "universe_count": len(universe),
        "config": {
            "min_premium_yield_pct": MIN_PREMIUM_YIELD * 100,
            "min_dte": MIN_DTE,
            "max_dte": MAX_DTE,
            "target_delta": TARGET_DELTA,
            "early_close_at_pct": EARLY_CLOSE_THRESHOLD * 100,
            "max_positions": MAX_ACTIVE_POSITIONS,
        },
    }
