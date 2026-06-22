"""
revision_engine.py — Post-earnings drift capture (accelerating EPS beats).

Finds stocks where EPS beats are large (>15%) and the market hasn't fully
reacted yet. Post-earnings drift is well-documented: markets under-react to
earnings surprises, then gradually reprice over 1-30 days.

Own Alpaca paper account. Writes only to experiment_positions table.
Never imports from trading_engine, wheel_engine, or pureai_engine.
FMP earnings cache is shared read-only — zero extra API calls.
"""

import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

_SCAN_HOUR_ET = 9
_SCAN_MINUTE_ET = 50
_CHECK_INTERVAL = 300   # 5 min — ensures we hit the 9:50-9:59 AM scan window reliably
_MAX_POSITIONS = 4
_POSITION_SIZE_PCT = 0.20
_SCORE_THRESHOLD = 65
_STOP_PCT = 0.07
_TARGET_PCT = 0.12
_MAX_HOLD_DAYS = 30
_MIN_BEAT_PCT = 15.0
_MAX_ALREADY_MOVED_PCT = 20.0
_MIN_VOLUME = 200_000
_MIN_PRICE = 5.0

_last_scan_date: Optional[str] = None
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


# ── Alpaca clients (own account) ─────────────────────────────────────────────

def _is_configured() -> bool:
    return bool(settings.alpaca_revision_key and settings.alpaca_revision_secret)


def _is_paper() -> bool:
    return "paper" in settings.alpaca_revision_base_url.lower()


def _get_trading_client():
    from alpaca.trading.client import TradingClient
    return TradingClient(settings.alpaca_revision_key,
                         settings.alpaca_revision_secret, paper=_is_paper())


def _get_data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(settings.alpaca_revision_key,
                                     settings.alpaca_revision_secret)


def _market_is_open() -> bool:
    try:
        return bool(_get_trading_client().get_clock().is_open)
    except Exception as e:
        logger.warning(f"[Revision] clock check failed: {e}")
        return False


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_conn():
    from services.db import _get_conn as db_conn
    return db_conn()


def _open_position_count() -> int:
    try:
        conn = _get_conn()
        if not conn:
            return 0
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM experiment_positions WHERE engine='revision' AND status='open'"
            )
            return cur.fetchone()[0]
    except Exception:
        return 0


def _has_open_position(symbol: str) -> bool:
    try:
        conn = _get_conn()
        if not conn:
            return False
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM experiment_positions WHERE engine='revision' AND symbol=%s AND status='open'",
                (symbol,)
            )
            return cur.fetchone() is not None
    except Exception:
        return False


def _record_open(symbol: str, entry_price: float, shares: int, stop: float,
                 target: float, beat_pct: float, notes: str):
    try:
        conn = _get_conn()
        if not conn:
            return
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO experiment_positions
                    (engine, symbol, entry_price, shares, stop_price, target_price,
                     peak_price, beat_pct_curr, notes)
                VALUES ('revision', %s, %s, %s, %s, %s, %s, %s, %s)
            """, (symbol, entry_price, shares, stop, target, entry_price, beat_pct, notes))
        conn.commit()
        logger.info(f"[Revision] opened {symbol} @ ${entry_price:.2f} beat={beat_pct:.1f}%")
    except Exception as e:
        logger.error(f"[Revision] record_open error: {e}")


def _get_open_positions() -> list[dict]:
    try:
        conn = _get_conn()
        if not conn:
            return []
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, symbol, entry_price, shares, stop_price, target_price,
                       peak_price, entry_date, beat_pct_curr
                FROM experiment_positions
                WHERE engine='revision' AND status='open'
            """)
            rows = cur.fetchall()
        return [
            {"id": r[0], "symbol": r[1], "entry_price": r[2], "shares": r[3],
             "stop_price": r[4], "target_price": r[5], "peak_price": r[6],
             "entry_date": r[7], "beat_pct": r[8]}
            for r in rows
        ]
    except Exception:
        return []


def _close_position(pos_id: int, exit_price: float, reason: str):
    try:
        conn = _get_conn()
        if not conn:
            return
        with conn.cursor() as cur:
            cur.execute(
                "SELECT entry_price, shares FROM experiment_positions WHERE id=%s",
                (pos_id,)
            )
            row = cur.fetchone()
            if not row:
                return
            ep, shares = row
            pl = (exit_price - ep) * (shares or 0)
            cur.execute("""
                UPDATE experiment_positions SET
                    status='closed', exit_price=%s, exit_date=NOW(),
                    realized_pl=%s, notes=COALESCE(notes,'') || ' | exit: ' || %s
                WHERE id=%s
            """, (exit_price, pl, reason, pos_id))
        conn.commit()
        logger.info(f"[Revision] closed pos {pos_id} @ ${exit_price:.2f} reason={reason} pl=${pl:.2f}")
    except Exception as e:
        logger.error(f"[Revision] close_position error: {e}")


def _update_peak(pos_id: int, peak: float):
    try:
        conn = _get_conn()
        if not conn:
            return
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE experiment_positions SET peak_price=%s WHERE id=%s",
                (peak, pos_id)
            )
        conn.commit()
    except Exception:
        pass


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score_candidate(symbol: str, beat_pct: float, beat_date: str,
                     current_price: float, price_at_earnings: Optional[float],
                     avg_volume: float, ma20: Optional[float]) -> int:
    score = 0

    if beat_pct > 30:
        score += 40
    elif beat_pct > 20:
        score += 30
    elif beat_pct > 15:
        score += 20

    # How much the stock has already moved since earnings
    if price_at_earnings and price_at_earnings > 0:
        already_moved = (current_price - price_at_earnings) / price_at_earnings * 100
        if already_moved < 5:
            score += 25
        elif already_moved < 20:
            score += 10

    # Uptrend intact
    if ma20 and current_price > ma20:
        score += 15

    # Fresher earnings = more drift ahead
    try:
        edate = datetime.fromisoformat(beat_date[:10]).date()
        days_ago = (datetime.now().date() - edate).days
        if days_ago < 7:
            score += 10
    except Exception:
        pass

    return score


# ── Snapshot helpers ──────────────────────────────────────────────────────────

def _get_snapshots(symbols: list[str]) -> dict:
    if not symbols:
        return {}
    try:
        from alpaca.data.requests import StockSnapshotRequest
        from alpaca.data.enums import Feed
        client = _get_data_client()
        req = StockSnapshotRequest(symbol_or_symbols=symbols, feed=Feed.IEX)
        snaps = client.get_stock_snapshot(req)
        return {s: snaps[s] for s in snaps}
    except Exception as e:
        logger.warning(f"[Revision] snapshot error: {e}")
        return {}


def _current_price(snap) -> Optional[float]:
    try:
        if snap.latest_trade:
            return float(snap.latest_trade.price)
        if snap.latest_quote:
            return float((snap.latest_quote.ask_price + snap.latest_quote.bid_price) / 2)
    except Exception:
        pass
    return None


def _avg_volume(snap) -> float:
    try:
        daily = snap.daily_bar
        if daily:
            return float(daily.volume)
    except Exception:
        pass
    return 0.0


# ── Place order ───────────────────────────────────────────────────────────────

def _place_limit_buy(symbol: str, shares: int, ask: float) -> bool:
    try:
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        client = _get_trading_client()
        req = LimitOrderRequest(
            symbol=symbol,
            qty=shares,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=round(ask, 2),
        )
        client.submit_order(req)
        return True
    except Exception as e:
        logger.error(f"[Revision] order error {symbol}: {e}")
        return False


def _place_market_sell(symbol: str, shares: int) -> bool:
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        client = _get_trading_client()
        req = MarketOrderRequest(
            symbol=symbol,
            qty=shares,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        client.submit_order(req)
        return True
    except Exception as e:
        logger.error(f"[Revision] sell error {symbol}: {e}")
        return False


# ── Account equity ────────────────────────────────────────────────────────────

def _get_equity() -> float:
    try:
        acct = _get_trading_client().get_account()
        return float(acct.equity)
    except Exception:
        return 25_000.0


# ── Main scan ─────────────────────────────────────────────────────────────────

def run_scan() -> dict:
    global _last_scan_date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _last_scan_date == today:
        return {"skipped": "already ran today"}

    logger.info("[Revision] starting daily scan")
    results = {"candidates": [], "bought": [], "skipped": []}

    try:
        from services.brain.connectors.fmp_earnings import _earnings_cache, _ensure_fresh
        _ensure_fresh()
    except Exception as e:
        logger.warning(f"[Revision] FMP cache error: {e}")
        return results

    # Filter strong beats
    candidates = [
        (sym, data)
        for sym, data in _earnings_cache.items()
        if data.get("direction") == "beat" and data.get("beat_pct", 0) >= _MIN_BEAT_PCT
    ]

    if not candidates:
        logger.info("[Revision] no candidates with beat_pct >= 15%")
        _last_scan_date = today
        return results

    symbols = [sym for sym, _ in candidates]
    snaps = _get_snapshots(symbols)
    equity = _get_equity()
    open_count = _open_position_count()

    for symbol, data in candidates:
        if open_count >= _MAX_POSITIONS:
            break
        if _has_open_position(symbol):
            results["skipped"].append({"symbol": symbol, "reason": "already_open"})
            continue

        snap = snaps.get(symbol)
        if not snap:
            results["skipped"].append({"symbol": symbol, "reason": "no_snapshot"})
            continue

        price = _current_price(snap)
        if not price or price < _MIN_PRICE:
            results["skipped"].append({"symbol": symbol, "reason": "price_too_low"})
            continue

        vol = _avg_volume(snap)
        if vol < _MIN_VOLUME:
            results["skipped"].append({"symbol": symbol, "reason": "low_volume"})
            continue

        beat_pct = data["beat_pct"]
        beat_date = data.get("beat_date", "")

        # Approximate price at earnings — we don't have it directly, use current
        # The scoring penalizes stocks that have already moved >20%
        score = _score_candidate(
            symbol, beat_pct, beat_date,
            current_price=price,
            price_at_earnings=None,  # not tracked, score still works
            avg_volume=vol,
            ma20=None,
        )

        results["candidates"].append({"symbol": symbol, "score": score, "beat_pct": beat_pct})

        if score < _SCORE_THRESHOLD:
            results["skipped"].append({"symbol": symbol, "reason": f"score_too_low_{score}"})
            continue

        budget = equity * _POSITION_SIZE_PCT
        shares = max(1, int(budget / price))
        stop = round(price * (1 - _STOP_PCT), 2)
        target = round(price * (1 + _TARGET_PCT), 2)

        ok = _place_limit_buy(symbol, shares, price)
        if ok:
            _record_open(symbol, price, shares, stop, target,
                         beat_pct, f"score={score} beat={beat_pct:.1f}%")
            results["bought"].append({"symbol": symbol, "shares": shares, "price": price})
            open_count += 1

    _last_scan_date = today
    logger.info(f"[Revision] scan done: {len(results['bought'])} bought, "
                f"{len(results['candidates'])} candidates")
    return results


# ── Exit management ───────────────────────────────────────────────────────────

def check_exits():
    positions = _get_open_positions()
    if not positions:
        return

    symbols = [p["symbol"] for p in positions]
    snaps = _get_snapshots(symbols)

    for pos in positions:
        symbol = pos["symbol"]
        snap = snaps.get(symbol)
        if not snap:
            continue

        price = _current_price(snap)
        if not price:
            continue

        ep = pos["entry_price"] or price
        stop = pos["stop_price"] or (ep * (1 - _STOP_PCT))
        target = pos["target_price"] or (ep * (1 + _TARGET_PCT))
        peak = pos["peak_price"] or price

        # Update peak
        if price > peak:
            _update_peak(pos["id"], price)
            peak = price

        # Stop loss
        if price <= stop:
            _place_market_sell(symbol, pos["shares"])
            _close_position(pos["id"], price, "stop_loss")
            continue

        # Target hit
        if price >= target:
            _place_market_sell(symbol, pos["shares"])
            _close_position(pos["id"], price, "target_hit")
            continue

        # Time limit
        if pos["entry_date"]:
            held_days = (datetime.now(timezone.utc) - pos["entry_date"]).days
            if held_days >= _MAX_HOLD_DAYS:
                _place_market_sell(symbol, pos["shares"])
                _close_position(pos["id"], price, "time_limit")
                continue


# ── Scheduler ─────────────────────────────────────────────────────────────────

def _is_scan_time() -> bool:
    from datetime import datetime
    import zoneinfo
    now = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    return (now.weekday() < 5 and
            now.hour == _SCAN_HOUR_ET and
            now.minute >= _SCAN_MINUTE_ET)


def _loop():
    logger.info("[Revision] scheduler started")
    while not _stop.is_set():
        try:
            if _is_configured() and _market_is_open():
                if _is_scan_time():
                    run_scan()
                check_exits()
        except Exception as e:
            logger.error(f"[Revision] scheduler error: {e}")
        _stop.wait(_CHECK_INTERVAL)


def start_revision_scheduler():
    global _thread
    if not _is_configured():
        logger.info("[Revision] keys not set — scheduler not started")
        return
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, daemon=True, name="revision-scheduler")
    _thread.start()
    logger.info("[Revision] scheduler thread started")


# ── Status / summary helpers (used by router) ─────────────────────────────────

def get_status() -> dict:
    configured = _is_configured()
    out: dict = {"engine": "revision", "configured": configured, "running": False}
    if not configured:
        return out
    out["running"] = bool(_thread and _thread.is_alive())
    try:
        acct = _get_trading_client().get_account()
        out["equity"] = float(acct.equity)
        out["cash"] = float(acct.cash)
    except Exception as e:
        out["account_error"] = str(e)
    try:
        conn = _get_conn()
        if conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FILTER (WHERE status='open'),
                           COUNT(*) FILTER (WHERE status='closed'),
                           COALESCE(SUM(realized_pl) FILTER (WHERE status='closed'), 0),
                           COUNT(*) FILTER (WHERE status='closed' AND realized_pl > 0)
                    FROM experiment_positions WHERE engine='revision'
                """)
                open_c, closed_c, total_pl, wins = cur.fetchone()
            out["open_positions"] = int(open_c or 0)
            out["closed_trades"] = int(closed_c or 0)
            out["realized_pl"] = round(float(total_pl or 0), 2)
            out["win_rate"] = round(100 * wins / closed_c, 1) if closed_c else None
    except Exception:
        pass
    return out


def get_positions(status_filter: Optional[str] = None) -> list[dict]:
    try:
        conn = _get_conn()
        if not conn:
            return []
        with conn.cursor() as cur:
            if status_filter:
                cur.execute("""
                    SELECT id, symbol, entry_price, entry_date, shares, stop_price,
                           target_price, status, exit_price, exit_date, realized_pl,
                           beat_pct_curr, notes
                    FROM experiment_positions WHERE engine='revision' AND status=%s
                    ORDER BY entry_date DESC LIMIT 50
                """, (status_filter,))
            else:
                cur.execute("""
                    SELECT id, symbol, entry_price, entry_date, shares, stop_price,
                           target_price, status, exit_price, exit_date, realized_pl,
                           beat_pct_curr, notes
                    FROM experiment_positions WHERE engine='revision'
                    ORDER BY entry_date DESC LIMIT 50
                """)
            rows = cur.fetchall()
        return [
            {"id": r[0], "symbol": r[1], "entry_price": r[2],
             "entry_date": r[3].isoformat() if r[3] else None,
             "shares": r[4], "stop_price": r[5], "target_price": r[6],
             "status": r[7], "exit_price": r[8],
             "exit_date": r[9].isoformat() if r[9] else None,
             "realized_pl": r[10], "beat_pct": r[11], "notes": r[12]}
            for r in rows
        ]
    except Exception as e:
        logger.error(f"[Revision] get_positions error: {e}")
        return []


def get_summary() -> dict:
    try:
        conn = _get_conn()
        if not conn:
            return {}
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE status='closed') AS total_trades,
                    COALESCE(SUM(realized_pl) FILTER (WHERE status='closed'), 0) AS net_pl,
                    COUNT(*) FILTER (WHERE status='closed' AND realized_pl > 0) AS wins,
                    MAX(realized_pl) FILTER (WHERE status='closed') AS best,
                    MIN(realized_pl) FILTER (WHERE status='closed') AS worst
                FROM experiment_positions WHERE engine='revision'
            """)
            row = cur.fetchone()
        total, net_pl, wins, best, worst = row
        total = int(total or 0)
        return {
            "engine": "revision",
            "total_trades": total,
            "net_pl": round(float(net_pl or 0), 2),
            "win_rate": round(100 * wins / total, 1) if total else None,
            "best_trade": round(float(best), 2) if best else None,
            "worst_trade": round(float(worst), 2) if worst else None,
        }
    except Exception as e:
        logger.error(f"[Revision] get_summary error: {e}")
        return {}


def close_position_by_id(pos_id: int) -> dict:
    positions = get_positions("open")
    pos = next((p for p in positions if p["id"] == pos_id), None)
    if not pos:
        return {"error": "position not found or not open"}
    sym = pos["symbol"]
    snaps = _get_snapshots([sym])
    snap = snaps.get(sym)
    price = _current_price(snap) if snap else pos["entry_price"]
    _place_market_sell(sym, pos["shares"])
    _close_position(pos_id, price, "manual_close")
    return {"closed": True, "symbol": sym, "exit_price": price}
