"""
squeeze_engine.py — Short squeeze detection strategy.

Detects stocks where high short interest (daysToCover) + volume spike creates
conditions forcing short sellers to buy back shares, driving the price up.

Short interest source: NASDAQ API (no key, covers NASDAQ-listed stocks).
NYSE fallback: yfinance (max 1-5 calls/day — well below 429 threshold).
Own Alpaca paper account. Writes only to experiment_positions table.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)

_SCAN_HOUR_ET = 9
_SCAN_MINUTE_ET = 50
_CHECK_INTERVAL = 300   # 5 min — ensures we hit the 9:50-9:59 AM scan window reliably
_MAX_POSITIONS = 2
_POSITION_SIZE_PCT = 0.25
_SCORE_THRESHOLD = 70
_STOP_PCT = 0.08
_PROFIT_LOCK_TRIGGER = 0.10  # once up 10%, activate trailing
_TRAILING_STOP_PCT = 0.06    # 6% trailing from peak
_TARGET_PCT = 0.25
_MAX_HOLD_DAYS = 5
_MIN_VOLUME_RATIO = 2.0
_MIN_PRICE_CHANGE_PCT = 3.0
_MIN_PRICE = 3.0
_NASDAQ_API_DELAY = 0.3      # seconds between NASDAQ API calls

_last_scan_date: Optional[str] = None
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


# ── Alpaca clients (own account) ─────────────────────────────────────────────

def _is_configured() -> bool:
    return bool(settings.alpaca_squeeze_key and settings.alpaca_squeeze_secret)


def _is_paper() -> bool:
    return "paper" in settings.alpaca_squeeze_base_url.lower()


def _get_trading_client():
    from alpaca.trading.client import TradingClient
    return TradingClient(settings.alpaca_squeeze_key,
                         settings.alpaca_squeeze_secret, paper=_is_paper())


def _get_data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(settings.alpaca_squeeze_key,
                                     settings.alpaca_squeeze_secret)


def _market_is_open() -> bool:
    try:
        return bool(_get_trading_client().get_clock().is_open)
    except Exception as e:
        logger.warning(f"[Squeeze] clock check failed: {e}")
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
                "SELECT COUNT(*) FROM experiment_positions WHERE engine='squeeze' AND status='open'"
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
                "SELECT 1 FROM experiment_positions WHERE engine='squeeze' AND symbol=%s AND status='open'",
                (symbol,)
            )
            return cur.fetchone() is not None
    except Exception:
        return False


def _record_open(symbol: str, entry_price: float, shares: int, stop: float,
                 target: float, days_to_cover: Optional[float],
                 volume_ratio: float, notes: str):
    try:
        conn = _get_conn()
        if not conn:
            return
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO experiment_positions
                    (engine, symbol, entry_price, shares, stop_price, target_price,
                     peak_price, days_to_cover, volume_ratio, notes)
                VALUES ('squeeze', %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (symbol, entry_price, shares, stop, target, entry_price,
                  days_to_cover, volume_ratio, notes))
        conn.commit()
        logger.info(f"[Squeeze] opened {symbol} @ ${entry_price:.2f} "
                    f"dtc={days_to_cover} vol_ratio={volume_ratio:.1f}")
    except Exception as e:
        logger.error(f"[Squeeze] record_open error: {e}")


def _get_open_positions() -> list[dict]:
    try:
        conn = _get_conn()
        if not conn:
            return []
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, symbol, entry_price, shares, stop_price, target_price,
                       peak_price, entry_date, days_to_cover, volume_ratio
                FROM experiment_positions
                WHERE engine='squeeze' AND status='open'
            """)
            rows = cur.fetchall()
        return [
            {"id": r[0], "symbol": r[1], "entry_price": r[2], "shares": r[3],
             "stop_price": r[4], "target_price": r[5], "peak_price": r[6],
             "entry_date": r[7], "days_to_cover": r[8], "volume_ratio": r[9]}
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
        logger.info(f"[Squeeze] closed pos {pos_id} @ ${exit_price:.2f} reason={reason} pl=${pl:.2f}")
    except Exception as e:
        logger.error(f"[Squeeze] close_position error: {e}")


def _update_stop_and_peak(pos_id: int, new_stop: float, new_peak: float):
    try:
        conn = _get_conn()
        if not conn:
            return
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE experiment_positions SET stop_price=%s, peak_price=%s WHERE id=%s",
                (new_stop, new_peak, pos_id)
            )
        conn.commit()
    except Exception:
        pass


# ── Short interest fetching ───────────────────────────────────────────────────

def _get_days_to_cover_nasdaq(symbol: str) -> Optional[float]:
    """Fetch daysToCover from NASDAQ API. Returns None if unavailable."""
    url = f"https://api.nasdaq.com/api/quote/{symbol}/short-interest"
    try:
        resp = httpx.get(
            url,
            params={"type": "SHORT_INTEREST", "limit": 1},
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                "Accept": "application/json",
            },
            timeout=10,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        rows = (data.get("data") or {}).get("shortInterestTable", {}).get("rows", [])
        if not rows:
            return None
        dtc = rows[0].get("daysToCover")
        return float(dtc) if dtc is not None else None
    except Exception as e:
        logger.debug(f"[Squeeze] NASDAQ API {symbol}: {e}")
        return None


def _get_days_to_cover_yfinance(symbol: str) -> Optional[float]:
    """Fallback for NYSE-listed stocks. Used sparingly (1-5 calls/day max)."""
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info
        short_pct = info.get("shortPercentOfFloat")
        avg_vol = info.get("averageVolume")
        shares_out = info.get("sharesOutstanding")
        if short_pct and avg_vol and shares_out and avg_vol > 0:
            shares_short = short_pct * shares_out
            return round(shares_short / avg_vol, 2)
    except Exception as e:
        logger.debug(f"[Squeeze] yfinance {symbol}: {e}")
    return None


def _fetch_short_interest(symbol: str) -> Optional[float]:
    """Get daysToCover: NASDAQ API first, yfinance fallback for NYSE stocks."""
    dtc = _get_days_to_cover_nasdaq(symbol)
    if dtc is not None:
        return dtc
    # Fallback — only for NYSE stocks (NASDAQ returns 200 with no data for unsupported)
    time.sleep(0.5)
    return _get_days_to_cover_yfinance(symbol)


# ── Volume/price screening via Alpaca ────────────────────────────────────────

def _get_all_snapshots_batch() -> dict:
    """Fetch batch snapshot for a broad universe to find volume spikes."""
    try:
        from alpaca.data.requests import StockSnapshotRequest
        from alpaca.data.enums import Feed
        # Use a broad but manageable universe — high-vol stocks most likely to squeeze
        universe = [
            "MARA", "RIOT", "COIN", "HOOD", "SOFI", "RIVN", "LCID", "NIO",
            "XPEV", "UPST", "AFRM", "BBAI", "SOUN", "SMCI", "IONQ", "QBTS",
            "RGTI", "ARKG", "ARKK", "GME", "AMC", "BBBY", "SPCE", "NKLA",
            "WKHS", "RIDE", "GOEV", "HYLN", "BLNK", "CHPT", "EVGO", "PLUG",
            "FCEL", "BLDP", "VJET", "OPEN", "OPFI", "CLOV", "WISH", "SKLZ",
            "BARK", "BODY", "GREE", "HRTX", "SAVA", "AGEN", "IMVT", "PRAX",
        ]
        client = _get_data_client()
        req = StockSnapshotRequest(symbol_or_symbols=universe, feed=Feed.IEX)
        snaps = client.get_stock_snapshot(req)
        return {s: snaps[s] for s in snaps}
    except Exception as e:
        logger.warning(f"[Squeeze] batch snapshot error: {e}")
        return {}


def _volume_ratio(snap) -> float:
    """Volume ratio: today's cumulative volume vs yesterday's total volume."""
    try:
        today_vol = snap.daily_bar.volume if snap.daily_bar else None
        yest_vol = snap.prev_daily_bar.volume if snap.prev_daily_bar else None
        if today_vol and yest_vol and yest_vol > 0:
            return today_vol / yest_vol
    except Exception:
        pass
    return 0.0


def _price_change_pct(snap) -> float:
    try:
        if snap.daily_bar:
            bar = snap.daily_bar
            if bar.open and bar.open > 0:
                return (bar.close - bar.open) / bar.open * 100
    except Exception:
        pass
    return 0.0


def _current_price(snap) -> Optional[float]:
    try:
        if snap.latest_trade:
            return float(snap.latest_trade.price)
        if snap.latest_quote:
            return float((snap.latest_quote.ask_price + snap.latest_quote.bid_price) / 2)
    except Exception:
        pass
    return None


def _ask_price(snap) -> Optional[float]:
    try:
        if snap.latest_quote:
            return float(snap.latest_quote.ask_price)
    except Exception:
        pass
    return _current_price(snap)


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score_candidate(dtc: Optional[float], vol_ratio: float,
                     price_change_pct: float) -> int:
    score = 0
    has_short_data = dtc is not None

    if has_short_data:
        if dtc > 7:
            score += 35
        elif dtc > 5:
            score += 25
        elif dtc > 3:
            score += 10

    if vol_ratio > 3:
        score += 20
    elif vol_ratio > 2:
        score += 10

    if price_change_pct > 5:
        score += 15
    elif price_change_pct > 3:
        score += 8

    # Cap score if no short interest data
    if not has_short_data:
        score = min(score, 60)

    return score


# ── Place orders ──────────────────────────────────────────────────────────────

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
        logger.error(f"[Squeeze] order error {symbol}: {e}")
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
        logger.error(f"[Squeeze] sell error {symbol}: {e}")
        return False


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

    logger.info("[Squeeze] starting daily scan")
    results = {"candidates": [], "bought": [], "skipped": []}

    snaps = _get_all_snapshots_batch()
    if not snaps:
        logger.warning("[Squeeze] no snapshots returned — skipping scan")
        return results

    equity = _get_equity()
    open_count = _open_position_count()

    # Step 1: filter by volume + price
    first_pass = []
    for symbol, snap in snaps.items():
        price = _current_price(snap)
        if not price or price < _MIN_PRICE:
            continue
        vol_r = _volume_ratio(snap)
        pct_chg = _price_change_pct(snap)
        if vol_r >= _MIN_VOLUME_RATIO and pct_chg >= _MIN_PRICE_CHANGE_PCT:
            first_pass.append((symbol, snap, vol_r, pct_chg))

    logger.info(f"[Squeeze] {len(first_pass)} candidates after vol/price filter")

    # Step 2: get short interest for each candidate
    for symbol, snap, vol_r, pct_chg in first_pass:
        if open_count >= _MAX_POSITIONS:
            break
        if _has_open_position(symbol):
            continue

        time.sleep(_NASDAQ_API_DELAY)
        dtc = _fetch_short_interest(symbol)

        score = _score_candidate(dtc, vol_r, pct_chg)
        results["candidates"].append({
            "symbol": symbol, "score": score,
            "days_to_cover": dtc, "vol_ratio": vol_r, "price_chg": pct_chg
        })

        if score < _SCORE_THRESHOLD:
            results["skipped"].append({"symbol": symbol, "reason": f"score_{score}"})
            continue

        price = _current_price(snap)
        ask = _ask_price(snap) or price
        budget = equity * _POSITION_SIZE_PCT
        shares = max(1, int(budget / ask))
        stop = round(ask * (1 - _STOP_PCT), 2)
        target = round(ask * (1 + _TARGET_PCT), 2)

        ok = _place_limit_buy(symbol, shares, ask)
        if ok:
            _record_open(symbol, ask, shares, stop, target, dtc, vol_r,
                         f"score={score} dtc={dtc} vol={vol_r:.1f}x pct={pct_chg:.1f}%")
            results["bought"].append({"symbol": symbol, "shares": shares,
                                      "price": ask, "days_to_cover": dtc})
            open_count += 1

    _last_scan_date = today
    logger.info(f"[Squeeze] scan done: {len(results['bought'])} bought, "
                f"{len(results['candidates'])} candidates")
    return results


# ── Exit management ───────────────────────────────────────────────────────────

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
        logger.warning(f"[Squeeze] exit snapshot error: {e}")
        return {}


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

        # Update peak and activate trailing stop once up 10%
        if price > peak:
            peak = price
            gain_pct = (peak - ep) / ep
            if gain_pct >= _PROFIT_LOCK_TRIGGER:
                new_stop = round(peak * (1 - _TRAILING_STOP_PCT), 2)
                stop = max(stop, new_stop)
            _update_stop_and_peak(pos["id"], stop, peak)

        # Stop loss (includes trailing)
        if price <= stop:
            _place_market_sell(symbol, pos["shares"])
            _close_position(pos["id"], price, "stop_loss")
            continue

        # Target hit: +25%
        if price >= target:
            _place_market_sell(symbol, pos["shares"])
            _close_position(pos["id"], price, "target_hit")
            continue

        # Time limit: 5 days
        if pos["entry_date"]:
            held_days = (datetime.now(timezone.utc) - pos["entry_date"]).days
            if held_days >= _MAX_HOLD_DAYS:
                _place_market_sell(symbol, pos["shares"])
                _close_position(pos["id"], price, "time_limit")
                continue

        # Squeeze died: volume fell back to normal
        vol_r = _volume_ratio(snap)
        if vol_r < 1.5:
            _place_market_sell(symbol, pos["shares"])
            _close_position(pos["id"], price, "squeeze_died")
            continue


# ── Scheduler ─────────────────────────────────────────────────────────────────

def _is_scan_time() -> bool:
    import zoneinfo
    now = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    return (now.weekday() < 5 and
            now.hour == _SCAN_HOUR_ET and
            now.minute >= _SCAN_MINUTE_ET)


def _loop():
    logger.info("[Squeeze] scheduler started")
    while not _stop.is_set():
        try:
            if _is_configured() and _market_is_open():
                if _is_scan_time():
                    run_scan()
                check_exits()
        except Exception as e:
            logger.error(f"[Squeeze] scheduler error: {e}")
        _stop.wait(_CHECK_INTERVAL)


def start_squeeze_scheduler():
    global _thread
    if not _is_configured():
        logger.info("[Squeeze] keys not set — scheduler not started")
        return
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, daemon=True, name="squeeze-scheduler")
    _thread.start()
    logger.info("[Squeeze] scheduler thread started")


# ── Status / summary helpers (used by router) ─────────────────────────────────

def get_status() -> dict:
    configured = _is_configured()
    out: dict = {"engine": "squeeze", "configured": configured, "running": False}
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
                    FROM experiment_positions WHERE engine='squeeze'
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
                           days_to_cover, volume_ratio, notes
                    FROM experiment_positions WHERE engine='squeeze' AND status=%s
                    ORDER BY entry_date DESC LIMIT 50
                """, (status_filter,))
            else:
                cur.execute("""
                    SELECT id, symbol, entry_price, entry_date, shares, stop_price,
                           target_price, status, exit_price, exit_date, realized_pl,
                           days_to_cover, volume_ratio, notes
                    FROM experiment_positions WHERE engine='squeeze'
                    ORDER BY entry_date DESC LIMIT 50
                """)
            rows = cur.fetchall()
        return [
            {"id": r[0], "symbol": r[1], "entry_price": r[2],
             "entry_date": r[3].isoformat() if r[3] else None,
             "shares": r[4], "stop_price": r[5], "target_price": r[6],
             "status": r[7], "exit_price": r[8],
             "exit_date": r[9].isoformat() if r[9] else None,
             "realized_pl": r[10], "days_to_cover": r[11],
             "volume_ratio": r[12], "notes": r[13]}
            for r in rows
        ]
    except Exception as e:
        logger.error(f"[Squeeze] get_positions error: {e}")
        return []


def get_summary() -> dict:
    try:
        conn = _get_conn()
        if not conn:
            return {}
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE status='closed'),
                    COALESCE(SUM(realized_pl) FILTER (WHERE status='closed'), 0),
                    COUNT(*) FILTER (WHERE status='closed' AND realized_pl > 0),
                    MAX(realized_pl) FILTER (WHERE status='closed'),
                    MIN(realized_pl) FILTER (WHERE status='closed')
                FROM experiment_positions WHERE engine='squeeze'
            """)
            row = cur.fetchone()
        total, net_pl, wins, best, worst = row
        total = int(total or 0)
        return {
            "engine": "squeeze",
            "total_trades": total,
            "net_pl": round(float(net_pl or 0), 2),
            "win_rate": round(100 * wins / total, 1) if total else None,
            "best_trade": round(float(best), 2) if best else None,
            "worst_trade": round(float(worst), 2) if worst else None,
        }
    except Exception as e:
        logger.error(f"[Squeeze] get_summary error: {e}")
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
