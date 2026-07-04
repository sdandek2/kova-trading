"""
sec_intel_engine.py — SEC Intelligence institutional following strategy.

Follows high-conviction institutional 13F signals from EDGAR.
Whitelist institutions auto-trigger trades. Scored institutions need ≥6 points.

Own Alpaca paper account (ALPACA_SEC_INTEL_KEY / ALPACA_SEC_INTEL_SECRET).
Signals synced from local DuckDB into PostgreSQL sec_intel_signals table.
Exit ladder: sell 25% at +25%, sell 25% at +50%, trail 20% on remaining 50%.
"""

import logging
import threading
import time
from datetime import datetime, timezone, date, timedelta
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

# ── Strategy constants ─────────────────────────────────────────────────────────

WHITELIST_INSTITUTIONS = {
    "Situational Awareness",
    "Elliott Management",
    "Berkshire Hathaway",
    "Pershing Square",
}

INSTITUTION_SCORES = {
    "Goldman Sachs AM":         3,
    "Third Point":              3,
    "CalPERS":                  2,
    "Coatue Management":        2,
    "Tiger Global":             2,
    "Wellington Management":    2,
    "D. E. Shaw":               2,
    "Point72 Asset Management": 2,
    "CPP Investments":          2,
    "Citadel":                  1,
}

ACTION_SCORES = {
    "NEW":        2,
    "STRONG_BUY": 1,
    "ADD":        0,
}

CONVERGENCE_BONUS = {
    range(5, 9):   2,   # 5-8 institutions
    range(9, 999): 3,   # 9+ institutions
}

AUTO_TRADE_MIN_SCORE = 6

HOLD_DAYS = {
    "Berkshire Hathaway":    365,
    "Pershing Square":       270,
    "Elliott Management":    180,
    "Situational Awareness":  90,
    "Goldman Sachs AM":       90,
    "Third Point":           180,
    "CalPERS":               120,
    "Wellington Management": 120,
    "default":                90,
}

# Position sizing — tiered by institution conviction (see sec-intelligence/TRADING_PLAN.md).
# Highest-conviction signals (Berkshire/Pershing/Elliott, whitelisted) get the largest size
# and widest stop; lower-conviction signals (Goldman, scored/convergence) get smaller size
# and tighter stops so a weak signal can't lose as much as a strong one.
POSITION_SIZE_PCT_BY_INSTITUTION = {
    "Berkshire Hathaway":    0.08,
    "Pershing Square":       0.08,
    "Elliott Management":    0.08,
    "Situational Awareness": 0.08,
    "Goldman Sachs AM":      0.04,
    "default":               0.025,  # Third Point, CalPERS, 9+ convergence, everything else
}
STOP_LOSS_PCT_BY_INSTITUTION = {
    "Berkshire Hathaway":    0.10,
    "Goldman Sachs AM":      0.10,
    "Pershing Square":       0.12,
    "Situational Awareness": 0.12,
    "Elliott Management":    0.15,
    "default":               0.12,  # 9+ convergence / scored institutions
}
MAX_POSITIONS       = 10
# Cap how much of the portfolio any single institution's open positions can occupy —
# matches TRADING_PLAN.md's "Max sector concentration: 25%". Without this, one
# institution's whitelisted signals from a single 13F quarter can dominate the
# entire book (e.g. 7 of 10 positions from one fund's Q1 filing).
MAX_INSTITUTION_CONCENTRATION_PCT = 0.25
TRAIL_PCT           = 0.20   # 20% trailing stop from peak
LADDER_L1_PCT       = 0.25   # sell 25% of position at +25%
LADDER_L2_PCT       = 0.50   # sell 25% more at +50%

# Bear regime thresholds
BEAR_VIX_THRESHOLD  = 25.0
BEAR_DAYS_REQUIRED  = 5      # VIX > 25 for 5 consecutive days

_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


# ── Alpaca clients ─────────────────────────────────────────────────────────────

def _is_configured() -> bool:
    return bool(settings.alpaca_sec_intel_key and settings.alpaca_sec_intel_secret)


def _is_paper() -> bool:
    return "paper" in settings.alpaca_sec_intel_base_url.lower()


def _get_trading_client():
    from alpaca.trading.client import TradingClient
    return TradingClient(
        settings.alpaca_sec_intel_key,
        settings.alpaca_sec_intel_secret,
        paper=_is_paper(),
    )


def _get_data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(
        settings.alpaca_sec_intel_key,
        settings.alpaca_sec_intel_secret,
    )


# ── Scoring ────────────────────────────────────────────────────────────────────

def score_signal(institution: str, action: str, convergence_count: int = 0) -> tuple[int, bool]:
    """
    Returns (score, is_whitelist).
    Whitelist institutions always trade regardless of score.
    """
    if institution in WHITELIST_INSTITUTIONS:
        return (10, True)

    score = INSTITUTION_SCORES.get(institution, 0)
    score += ACTION_SCORES.get(action, 0)

    for r, bonus in CONVERGENCE_BONUS.items():
        if convergence_count in r:
            score += bonus
            break

    return (score, False)


# ── Bear regime detection ──────────────────────────────────────────────────────

def _get_vix() -> Optional[float]:
    """Get current VIX from Alpaca via VXX ETF as proxy, or return None."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest
        client = _get_data_client()
        req = StockLatestQuoteRequest(symbol_or_symbols=["VIXY"])
        quotes = client.get_stock_latest_quote(req)
        if "VIXY" in quotes:
            return float(quotes["VIXY"].ask_price)
    except Exception as e:
        logger.warning(f"[SecIntel] VIX proxy fetch failed: {e}")
    return None


def is_bear_regime() -> bool:
    """
    Bear regime = VIX proxy > 25 AND S&P 500 below 200-day MA.
    Returns False (allow trading) if data unavailable — fail open.
    """
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        client = _get_data_client()
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=220)

        req = StockBarsRequest(
            symbol_or_symbols=["SPY"],
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        bars = client.get_stock_bars(req)
        spy_bars = bars["SPY"]
        closes = [float(b.close) for b in spy_bars]

        if len(closes) < 200:
            return False

        current = closes[-1]
        ma200 = sum(closes[-200:]) / 200
        spy_below_ma200 = current < ma200

        vix = _get_vix()
        vix_elevated = vix is not None and vix > BEAR_VIX_THRESHOLD

        bear = spy_below_ma200 and vix_elevated
        if bear:
            logger.warning(f"[SecIntel] Bear regime active — SPY {current:.2f} < MA200 {ma200:.2f}, VIX proxy {vix:.1f}")
        return bear

    except Exception as e:
        logger.warning(f"[SecIntel] Bear regime check failed: {e} — defaulting to normal")
        return False


# ── Portfolio helpers ──────────────────────────────────────────────────────────

def _get_portfolio_value() -> float:
    try:
        client = _get_trading_client()
        account = client.get_account()
        return float(account.portfolio_value)
    except Exception as e:
        logger.error(f"[SecIntel] portfolio value fetch failed: {e}")
        return 0.0


def _open_position_count() -> int:
    try:
        conn = _db_conn()
        if not conn:
            return 0
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM sec_intel_trade_log WHERE status = 'open'")
            return cur.fetchone()[0]
    except Exception:
        return 0


def _institution_exposure_pct(institution: str, portfolio_value: float) -> float:
    """Fraction of portfolio already committed to OPEN positions from this institution."""
    if portfolio_value <= 0:
        return 0.0
    try:
        conn = _db_conn()
        if not conn:
            return 0.0
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(position_size_usd), 0) FROM sec_intel_trade_log "
                "WHERE institution=%s AND status='open'",
                [institution]
            )
            committed = float(cur.fetchone()[0] or 0)
        return committed / portfolio_value
    except Exception:
        return 0.0


def _get_current_price(ticker: str) -> Optional[float]:
    try:
        from alpaca.data.requests import StockLatestQuoteRequest
        client = _get_data_client()
        req = StockLatestQuoteRequest(symbol_or_symbols=[ticker])
        quotes = client.get_stock_latest_quote(req)
        if ticker in quotes:
            q = quotes[ticker]
            mid = (float(q.bid_price) + float(q.ask_price)) / 2
            return mid if mid > 0 else float(q.ask_price)
    except Exception as e:
        logger.warning(f"[SecIntel] price fetch failed for {ticker}: {e}")
    return None


def _db_conn():
    from services.db import _get_conn
    return _get_conn()


# ── Trade execution ────────────────────────────────────────────────────────────

def _is_tradeable_ticker(ticker: str) -> bool:
    """
    Skip preferred shares and obviously-non-standard tickers before hitting Alpaca.
    Deliberately does NOT guess warrants/rights/units by suffix (W/R/U) — that
    heuristic blocked ordinary common stocks like SNOW, UBER, PLTR, NKTR, FOUR.
    Actual warrants/rights/units that Alpaca can't trade get rejected by the
    order submission itself (caught in enter_trade's try/except).
    """
    t = ticker.upper()
    if "-" in t:
        return False  # preferred shares e.g. DLR-PL, HPE-PC
    if "." in t:
        return False  # tickers with a class/exchange suffix e.g. BRK.A
    if len(t) > 5:
        return False  # exchange-listed tickers are max 5 chars
    return True


def enter_trade(ticker: str, institution: str, action: str,
                quarter: str, score: int, is_whitelist: bool,
                hold_days: int) -> bool:
    """Buy stock — full position entry."""
    if not _is_tradeable_ticker(ticker):
        logger.info(f"[SecIntel] Skipping {ticker} — warrant/preferred/OTC (not tradeable on Alpaca)")
        return False

    if not _is_configured():
        logger.warning("[SecIntel] Alpaca not configured — skipping trade")
        return False

    if _open_position_count() >= MAX_POSITIONS:
        logger.info(f"[SecIntel] Max positions ({MAX_POSITIONS}) reached — skipping {ticker}")
        return False

    # Check not already in this position
    conn = _db_conn()
    if conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM sec_intel_trade_log WHERE ticker=%s AND status='open'",
                [ticker]
            )
            if cur.fetchone():
                logger.info(f"[SecIntel] Already holding {ticker} — skipping")
                return False

    portfolio_value = _get_portfolio_value()
    if portfolio_value <= 0:
        return False

    size_pct = POSITION_SIZE_PCT_BY_INSTITUTION.get(institution, POSITION_SIZE_PCT_BY_INSTITUTION["default"])
    stop_pct = STOP_LOSS_PCT_BY_INSTITUTION.get(institution, STOP_LOSS_PCT_BY_INSTITUTION["default"])

    existing_exposure = _institution_exposure_pct(institution, portfolio_value)
    if existing_exposure + size_pct > MAX_INSTITUTION_CONCENTRATION_PCT:
        logger.info(
            f"[SecIntel] Skipping {ticker} — {institution} already at "
            f"{existing_exposure*100:.0f}% of portfolio (cap {MAX_INSTITUTION_CONCENTRATION_PCT*100:.0f}%)"
        )
        return False

    current_price = _get_current_price(ticker)
    if not current_price or current_price <= 0:
        logger.warning(f"[SecIntel] Could not get price for {ticker}")
        return False

    position_usd = portfolio_value * size_pct
    shares = max(1, int(position_usd / current_price))
    stop_price = round(current_price * (1 - stop_pct), 2)
    max_hold_date = (date.today() + timedelta(days=hold_days)).isoformat()

    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        import time as _time

        client = _get_trading_client()
        order = client.submit_order(MarketOrderRequest(
            symbol=ticker,
            qty=shares,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        ))
        logger.info(f"[SecIntel] BUY {shares} {ticker} @ ~{current_price:.2f} | {institution} {action} | order={order.id}")

        # Wait for fill and use actual fill price so DB entry is accurate
        filled_price = current_price
        for _ in range(10):
            _time.sleep(2)
            filled_order = client.get_order_by_id(order.id)
            if filled_order.filled_avg_price:
                filled_price = float(filled_order.filled_avg_price)
                break
        logger.info(f"[SecIntel] FILLED {ticker} @ {filled_price:.2f} (estimated {current_price:.2f})")

    except Exception as e:
        logger.error(f"[SecIntel] Order failed for {ticker}: {e}")
        return False

    # Recompute stop using actual fill price
    stop_price = round(filled_price * (1 - stop_pct), 2)
    position_usd = filled_price * shares

    # Log to DB using actual fill price
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO sec_intel_trade_log
                    (ticker, institution, signal_quarter, entry_price, shares,
                     position_size_usd, stop_price, peak_price, max_hold_date,
                     status, notes)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',%s)
                """, [
                    ticker, institution, quarter,
                    filled_price, shares, position_usd,
                    stop_price, filled_price,
                    max_hold_date,
                    f"score={score} whitelist={is_whitelist}",
                ])
        except Exception as e:
            logger.error(f"[SecIntel] DB log failed: {e}")

    _send_telegram(
        f"🟢 *SEC INTEL ENTRY*\n"
        f"{'⭐ WHITELIST' if is_whitelist else f'Score {score}'} | "
        f"*{ticker}* @ ${current_price:.2f}\n"
        f"Institution: {institution} | {action}\n"
        f"Quarter: {quarter} | Hold: {hold_days}d\n"
        f"Shares: {shares} | Size: ${position_usd:,.0f}\n"
        f"Stop: ${stop_price:.2f} | Max hold: {max_hold_date}"
    )
    return True


def _exit_partial(trade_id: int, ticker: str, shares_to_sell: int,
                  current_price: float, entry_price: float, reason: str,
                  ladder_price_field: str, ladder_date_field: str,
                  ladder_shares_field: str, ladder_pl_field: str):
    """Sell a partial position (ladder step). Stores P&L for performance tracking."""
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        client = _get_trading_client()
        order = client.submit_order(MarketOrderRequest(
            symbol=ticker,
            qty=shares_to_sell,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        ))

        # Wait for fill and use actual fill price so realized P&L is accurate
        filled_price = current_price
        for _ in range(10):
            time.sleep(2)
            filled_order = client.get_order_by_id(order.id)
            if filled_order.filled_avg_price:
                filled_price = float(filled_order.filled_avg_price)
                break
    except Exception as e:
        logger.error(f"[SecIntel] Partial exit failed for {ticker}: {e}")
        return

    partial_pl = (filled_price - entry_price) * shares_to_sell

    conn = _db_conn()
    if conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE sec_intel_trade_log
                SET {ladder_price_field}=%s, {ladder_date_field}=NOW(),
                    {ladder_shares_field}=%s, {ladder_pl_field}=%s,
                    trail_stop_price=%s
                WHERE id=%s
            """, [filled_price, shares_to_sell, round(partial_pl, 2),
                  round(filled_price * (1 - TRAIL_PCT), 2), trade_id])

    _send_telegram(
        f"🟡 *SEC INTEL PARTIAL EXIT*\n"
        f"*{ticker}* {reason} @ ${filled_price:.2f}\n"
        f"Sold {shares_to_sell} shares | P&L: ${partial_pl:+,.0f}\n"
        f"Remaining position now trailing at 20%"
    )


def _exit_full(trade_id: int, ticker: str, shares: int,
               current_price: float, entry_price: float, reason: str,
               entry_date=None):
    """Exit full remaining position."""
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        client = _get_trading_client()
        order = client.submit_order(MarketOrderRequest(
            symbol=ticker,
            qty=shares,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        ))

        # Wait for fill and use actual fill price so realized P&L is accurate
        filled_price = current_price
        for _ in range(10):
            time.sleep(2)
            filled_order = client.get_order_by_id(order.id)
            if filled_order.filled_avg_price:
                filled_price = float(filled_order.filled_avg_price)
                break
    except Exception as e:
        logger.error(f"[SecIntel] Full exit failed for {ticker}: {e}")
        return

    pl = (filled_price - entry_price) * shares
    pl_pct = (filled_price - entry_price) / entry_price * 100

    conn = _db_conn()
    if conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE sec_intel_trade_log
                SET status='closed', exit_price=%s, exit_date=NOW(),
                    exit_reason=%s, realized_pl=%s, realized_pl_pct=%s,
                    hold_days_actual=%s
                WHERE id=%s
            """, [filled_price, reason, pl, pl_pct,
                  (datetime.now(timezone.utc).date() - entry_date.date()).days
                  if entry_date else None,
                  trade_id])

    emoji = "🟢" if pl >= 0 else "🔴"
    _send_telegram(
        f"{emoji} *SEC INTEL EXIT*\n"
        f"*{ticker}* @ ${filled_price:.2f} | Reason: {reason}\n"
        f"P&L: ${pl:+,.0f} ({pl_pct:+.1f}%)"
    )


# ── Position manager ───────────────────────────────────────────────────────────

def manage_positions():
    """
    Run exit ladder and trailing stop logic on all open positions.
    Called daily by scheduler.
    """
    if not _is_configured():
        return

    conn = _db_conn()
    if not conn:
        return

    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, ticker, entry_price, shares, stop_price,
                   peak_price, sold_25pct_at, sold_50pct_at,
                   trail_stop_price, max_hold_date,
                   entry_date, sold_25pct_shares, sold_50pct_shares
            FROM sec_intel_trade_log WHERE status = 'open'
        """)
        positions = cur.fetchall()

    for pos in positions:
        (trade_id, ticker, entry_price, shares, stop_price,
         peak_price, sold_25pct_at, sold_50pct_at,
         trail_stop, max_hold_date,
         entry_date, sold_25pct_shares, sold_50pct_shares) = pos

        price = _get_current_price(ticker)
        if not price:
            continue

        pct_gain = (price - entry_price) / entry_price

        # Actual shares still held in Alpaca (original minus any ladder exits)
        already_sold = (sold_25pct_shares or 0) + (sold_50pct_shares or 0)
        remaining_shares = max(1, shares - already_sold)

        # Update peak price
        if price > (peak_price or 0):
            peak_price = price
            new_trail = round(price * (1 - TRAIL_PCT), 2)
            conn2 = _db_conn()
            if conn2:
                with conn2.cursor() as cur2:
                    cur2.execute(
                        "UPDATE sec_intel_trade_log SET peak_price=%s, trail_stop_price=%s WHERE id=%s",
                        [peak_price, new_trail, trade_id]
                    )
            trail_stop = new_trail

        # Hard stop loss
        if price <= stop_price:
            _exit_full(trade_id, ticker, remaining_shares, price, entry_price, "stop_loss", entry_date)
            continue

        # Max hold time expired
        if max_hold_date and date.today() >= max_hold_date:
            _exit_full(trade_id, ticker, remaining_shares, price, entry_price, "time_limit", entry_date)
            continue

        # Trailing stop (only active after first partial exit)
        if sold_25pct_at and trail_stop and price <= trail_stop:
            _exit_full(trade_id, ticker, remaining_shares, price, entry_price, "trail_stop", entry_date)
            continue

        # Ladder L1: sell 25% at +25%
        if not sold_25pct_at and pct_gain >= LADDER_L1_PCT:
            sell_shares = max(1, shares // 4)
            _exit_partial(
                trade_id, ticker, sell_shares, price, entry_price,
                f"+25% ladder L1",
                "sold_25pct_at", "sold_25pct_date", "sold_25pct_shares", "sold_25pct_pl"
            )
            # Move stop to breakeven after L1
            conn2 = _db_conn()
            if conn2:
                with conn2.cursor() as cur2:
                    cur2.execute(
                        "UPDATE sec_intel_trade_log SET stop_price=%s WHERE id=%s",
                        [entry_price, trade_id]
                    )
            continue

        # Ladder L2: sell another 25% at +50%
        if sold_25pct_at and not sold_50pct_at and pct_gain >= LADDER_L2_PCT:
            sell_shares = max(1, shares // 4)
            _exit_partial(
                trade_id, ticker, sell_shares, price, entry_price,
                f"+50% ladder L2",
                "sold_50pct_at", "sold_50pct_date", "sold_50pct_shares", "sold_50pct_pl"
            )

        time.sleep(0.2)  # rate limit between positions


# ── Signal sync from local DuckDB ──────────────────────────────────────────────

def sync_signals_from_duckdb(duckdb_path: str):
    """
    Sync latest signals from local DuckDB into PostgreSQL.
    Only syncs COM (stock) signals with BUY actions.
    Called by scheduler weekly or after new 13F detected.
    """
    try:
        import duckdb
        duck = duckdb.connect(duckdb_path, read_only=True)
    except Exception as e:
        logger.error(f"[SecIntel] DuckDB connect failed: {e}")
        return 0

    try:
        rows = duck.execute("""
            SELECT
                i.name as institution,
                s.ticker,
                s.action,
                s.quarter,
                s.filed_date,
                s.investment_type
            FROM signals s
            JOIN institutions i ON i.id = s.institution_id
            WHERE s.action IN ('NEW', 'STRONG_BUY', 'ADD')
            AND s.investment_type = 'COM'
            AND s.filed_date >= CURRENT_DATE - INTERVAL '180 days'
            ORDER BY s.filed_date DESC
        """).fetchall()
        duck.close()
    except Exception as e:
        logger.error(f"[SecIntel] DuckDB query failed: {e}")
        duck.close()
        return 0

    conn = _db_conn()
    if not conn:
        logger.warning("[SecIntel] PostgreSQL unavailable — cannot sync signals")
        return 0

    synced = 0
    with conn.cursor() as cur:
        for institution, ticker, action, quarter, filed_date, inv_type in rows:
            score, is_whitelist = score_signal(institution, action)
            hold = HOLD_DAYS.get(institution, HOLD_DAYS["default"])

            try:
                cur.execute("""
                    INSERT INTO sec_intel_signals
                    (institution, ticker, action, quarter, filed_date,
                     investment_type, score, is_whitelist, hold_days)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (institution, ticker, quarter, investment_type)
                    DO UPDATE SET
                        action=EXCLUDED.action,
                        score=EXCLUDED.score,
                        is_whitelist=EXCLUDED.is_whitelist
                """, [institution, ticker, action, quarter, filed_date,
                      inv_type, score, is_whitelist, hold])
                synced += 1
            except Exception as e:
                logger.warning(f"[SecIntel] Signal insert failed {ticker}: {e}")

    logger.info(f"[SecIntel] Synced {synced} signals to PostgreSQL")
    return synced


# ── Auto-trade: process fresh signals ─────────────────────────────────────────

def process_new_signals():
    """
    Check PostgreSQL for signals not yet traded. Auto-enter qualifying ones.
    Skips if bear regime active.
    """
    if not _is_configured():
        return

    if is_bear_regime():
        logger.info("[SecIntel] Bear regime active — no new entries")
        _send_telegram("⚠️ *SEC INTEL* — Bear regime active, all entries paused")
        return

    conn = _db_conn()
    if not conn:
        return

    with conn.cursor() as cur:
        cur.execute("""
            SELECT s.institution, s.ticker, s.action, s.quarter,
                   s.filed_date, s.score, s.is_whitelist, s.hold_days
            FROM sec_intel_signals s
            WHERE (s.score >= %s OR s.is_whitelist = TRUE)
            AND s.investment_type = 'COM'
            AND NOT EXISTS (
                SELECT 1 FROM sec_intel_trade_log t
                WHERE t.ticker = s.ticker
                AND t.signal_quarter = s.quarter
            )
            AND s.filed_date >= CURRENT_DATE - INTERVAL '180 days'
            ORDER BY s.is_whitelist DESC, s.score DESC, s.filed_date DESC
        """, [AUTO_TRADE_MIN_SCORE])
        signals = cur.fetchall()

    logger.info(f"[SecIntel] {len(signals)} actionable signals to process")

    for institution, ticker, action, quarter, filed_date, score, is_whitelist, hold_days in signals:
        if _open_position_count() >= MAX_POSITIONS:
            logger.info("[SecIntel] Max positions reached")
            break
        entered = enter_trade(ticker, institution, action, quarter,
                              score, is_whitelist, hold_days)
        if entered:
            time.sleep(1)  # brief pause between orders


# ── Telegram ───────────────────────────────────────────────────────────────────

def _send_telegram(message: str):
    token = settings.sec_intel_telegram_token
    chat_id = settings.sec_intel_telegram_chat_id
    if not token or not chat_id:
        logger.info(f"[SecIntel] Telegram not configured — msg: {message[:80]}")
        return
    try:
        import httpx
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception as e:
        logger.warning(f"[SecIntel] Telegram send failed: {e}")


# ── Scheduler loop ─────────────────────────────────────────────────────────────

POSITION_CHECK_INTERVAL_MIN = 15  # how often to re-check stops/ladders during market hours


def _scheduler_loop():
    """
    Background thread:
    - Every POSITION_CHECK_INTERVAL_MIN during market hours (9:30 AM-4:00 PM ET,
      weekdays): manage open positions (exit ladder, stops). Intraday cadence so a
      hard stop or trailing stop isn't only checked once a day while the market
      is open and moving.
    - Every Sunday at 6 AM ET: process new signals
    """
    import pytz
    et = pytz.timezone("America/New_York")

    last_position_check: Optional[str] = None  # "YYYY-MM-DD HH:MM" of last slot run
    last_signal_check: Optional[str] = None

    while not _stop_event.is_set():
        now_et = datetime.now(et)
        today = now_et.strftime("%Y-%m-%d")
        hour, minute, weekday = now_et.hour, now_et.minute, now_et.weekday()

        # Intraday position management: every N minutes, 9:30 AM-4:00 PM ET, weekdays
        market_open = (hour > 9 or (hour == 9 and minute >= 30))
        market_close = hour < 16
        if (weekday < 5 and market_open and market_close
                and minute % POSITION_CHECK_INTERVAL_MIN == 0):
            slot = f"{today} {hour:02d}:{minute:02d}"
            if last_position_check != slot:
                logger.info("[SecIntel] Running position manager...")
                try:
                    manage_positions()
                    last_position_check = slot
                except Exception as e:
                    logger.error(f"[SecIntel] Position manager error: {e}")

        # Weekly signal processing: Sunday 6 AM ET
        if (weekday == 6 and hour == 6 and minute < 10
                and last_signal_check != today):
            logger.info("[SecIntel] Running weekly signal processor...")
            try:
                process_new_signals()
                last_signal_check = today
            except Exception as e:
                logger.error(f"[SecIntel] Signal processor error: {e}")

        _stop_event.wait(60)  # check every minute


def start():
    global _thread
    if not _is_configured():
        logger.info("[SecIntel] Alpaca keys not set — engine disabled")
        return
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_scheduler_loop, daemon=True, name="sec-intel")
    _thread.start()
    logger.info("[SecIntel] Engine started")


def stop():
    _stop_event.set()


def status() -> dict:
    configured = _is_configured()
    open_positions = _open_position_count() if configured else 0
    conn = _db_conn()
    signal_count = 0
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM sec_intel_signals WHERE filed_date >= CURRENT_DATE - 180"
                )
                signal_count = cur.fetchone()[0]
        except Exception:
            pass
    account_value = None
    buying_power = None
    if configured:
        try:
            client = _get_trading_client()
            acct = client.get_account()
            account_value = float(acct.portfolio_value)
            buying_power = float(acct.buying_power)
        except Exception:
            pass
    return {
        "configured": configured,
        "paper_mode": _is_paper() if configured else True,
        "open_positions": open_positions,
        "max_positions": MAX_POSITIONS,
        "signal_count_180d": signal_count,
        "thread_alive": _thread.is_alive() if _thread else False,
        "account_value": account_value,
        "buying_power": buying_power,
    }
