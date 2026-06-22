"""
spillover_engine.py — Sector earnings spillover strategy.

When a company beats earnings significantly (>10%), its sector peers almost
always follow within days. This engine buys peers before they move.

Own Alpaca paper account. Writes only to experiment_positions table.
FMP earnings cache is shared read-only — zero extra API calls.
Sector mapping loaded from backend/data/sector_peers.json.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

_SCAN_HOUR_ET = 9
_SCAN_MINUTE_ET = 50
_CHECK_INTERVAL = 300   # 5 min — ensures we hit the 9:50-9:59 AM scan window reliably
_MAX_POSITIONS = 3
_POSITION_SIZE_PCT = 0.20
_SCORE_THRESHOLD = 60
_STOP_PCT = 0.06
_TARGET_PCT = 0.10
_MAX_HOLD_DAYS = 14
_MIN_BEAT_PCT = 10.0
_TRIGGER_HOURS = 48
_PEER_MAX_MOVED_PCT = 5.0
_MIN_VOLUME = 300_000
_MIN_PRICE = 5.0

_last_scan_date: Optional[str] = None
_thread: Optional[threading.Thread] = None
_stop = threading.Event()

# Loaded once at startup
_sector_peers: dict = {}


def _load_sector_peers() -> dict:
    global _sector_peers
    if _sector_peers:
        return _sector_peers
    try:
        data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
        path = os.path.normpath(os.path.join(data_dir, "sector_peers.json"))
        with open(path) as f:
            raw = json.load(f)
        # Strip the comment key
        _sector_peers = {k: v for k, v in raw.items() if not k.startswith("_")}
        logger.info(f"[Spillover] loaded {len(_sector_peers)} peer mappings")
    except Exception as e:
        logger.warning(f"[Spillover] could not load sector_peers.json: {e}")
        _sector_peers = {}
    return _sector_peers


# ── Alpaca clients (own account) ─────────────────────────────────────────────

def _is_configured() -> bool:
    return bool(settings.alpaca_spillover_key and settings.alpaca_spillover_secret)


def _is_paper() -> bool:
    return "paper" in settings.alpaca_spillover_base_url.lower()


def _get_trading_client():
    from alpaca.trading.client import TradingClient
    return TradingClient(settings.alpaca_spillover_key,
                         settings.alpaca_spillover_secret, paper=_is_paper())


def _get_data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(settings.alpaca_spillover_key,
                                     settings.alpaca_spillover_secret)


def _market_is_open() -> bool:
    try:
        return bool(_get_trading_client().get_clock().is_open)
    except Exception as e:
        logger.warning(f"[Spillover] clock check failed: {e}")
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
                "SELECT COUNT(*) FROM experiment_positions WHERE engine='spillover' AND status='open'"
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
                "SELECT 1 FROM experiment_positions WHERE engine='spillover' AND symbol=%s AND status='open'",
                (symbol,)
            )
            return cur.fetchone() is not None
    except Exception:
        return False


def _record_open(symbol: str, entry_price: float, shares: int, stop: float,
                 target: float, trigger: str, beat_pct: float, sector: str, notes: str):
    try:
        conn = _get_conn()
        if not conn:
            return
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO experiment_positions
                    (engine, symbol, entry_price, shares, stop_price, target_price,
                     peak_price, trigger_symbol, trigger_beat_pct, sector, notes)
                VALUES ('spillover', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (symbol, entry_price, shares, stop, target, entry_price,
                  trigger, beat_pct, sector, notes))
        conn.commit()
        logger.info(f"[Spillover] opened {symbol} @ ${entry_price:.2f} trigger={trigger} beat={beat_pct:.1f}%")
    except Exception as e:
        logger.error(f"[Spillover] record_open error: {e}")


def _get_open_positions() -> list[dict]:
    try:
        conn = _get_conn()
        if not conn:
            return []
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, symbol, entry_price, shares, stop_price, target_price,
                       entry_date, trigger_symbol, trigger_beat_pct
                FROM experiment_positions
                WHERE engine='spillover' AND status='open'
            """)
            rows = cur.fetchall()
        return [
            {"id": r[0], "symbol": r[1], "entry_price": r[2], "shares": r[3],
             "stop_price": r[4], "target_price": r[5], "entry_date": r[6],
             "trigger_symbol": r[7], "trigger_beat_pct": r[8]}
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
        logger.info(f"[Spillover] closed pos {pos_id} @ ${exit_price:.2f} reason={reason} pl=${pl:.2f}")
    except Exception as e:
        logger.error(f"[Spillover] close_position error: {e}")


# ── Snapshot helpers ──────────────────────────────────────────────────────────

def _get_snapshots(symbols: list[str]) -> dict:
    if not symbols:
        return {}
    try:
        from alpaca.data.requests import StockSnapshotRequest
        from alpaca.data.enums import DataFeed as Feed
        client = _get_data_client()
        req = StockSnapshotRequest(symbol_or_symbols=symbols, feed=Feed.IEX)
        snaps = client.get_stock_snapshot(req)
        return {s: snaps[s] for s in snaps}
    except Exception as e:
        logger.warning(f"[Spillover] snapshot error: {e}")
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


def _ask_price(snap) -> Optional[float]:
    try:
        if snap.latest_quote:
            return float(snap.latest_quote.ask_price)
    except Exception:
        pass
    return _current_price(snap)


def _daily_volume(snap) -> float:
    try:
        if snap.daily_bar:
            return float(snap.daily_bar.volume)
    except Exception:
        pass
    return 0.0


# ── Place / cancel orders ─────────────────────────────────────────────────────

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
        logger.error(f"[Spillover] order error {symbol}: {e}")
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
        logger.error(f"[Spillover] sell error {symbol}: {e}")
        return False


def _get_equity() -> float:
    try:
        acct = _get_trading_client().get_account()
        return float(acct.equity)
    except Exception:
        return 25_000.0


# ── Peer scoring ──────────────────────────────────────────────────────────────

def _score_peer(trigger_sym: str, peer_sym: str, peer_snap,
                trigger_beat_pct: float) -> int:
    score = 0

    # Same sub-industry: the trigger and peer are in the same peer list
    peers = _load_sector_peers()
    trigger_peers = set(peers.get(trigger_sym, []))
    peer_peers = set(peers.get(peer_sym, []))
    if trigger_sym in peer_peers or peer_sym in trigger_peers:
        score += 30

    # Volume uptick (can't compute ratio without historical; use proxy: high volume today)
    vol = _daily_volume(peer_snap)
    if vol > 1_000_000:
        score += 15
    elif vol > 300_000:
        score += 5

    # Not overbought — approximate via price vs daily bar
    try:
        if peer_snap.daily_bar:
            bar = peer_snap.daily_bar
            day_chg = (bar.close - bar.open) / bar.open * 100
            if day_chg < 3:  # hasn't already gapped up
                score += 15
    except Exception:
        pass

    return score


# ── Main scan ─────────────────────────────────────────────────────────────────

def run_scan() -> dict:
    global _last_scan_date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _last_scan_date == today:
        return {"skipped": "already ran today"}

    logger.info("[Spillover] starting daily scan")
    results = {"triggers": [], "bought": [], "skipped": []}
    peers_map = _load_sector_peers()

    try:
        from services.brain.connectors.fmp_earnings import _earnings_cache, _ensure_fresh
        _ensure_fresh()
    except Exception as e:
        logger.warning(f"[Spillover] FMP cache error: {e}")
        return results

    # Find triggers from last 48 hours
    cutoff = time.time() - (_TRIGGER_HOURS * 3600)
    triggers = []
    for sym, data in _earnings_cache.items():
        if data.get("direction") != "beat" or data.get("beat_pct", 0) < _MIN_BEAT_PCT:
            continue
        try:
            beat_date = datetime.fromisoformat(data["beat_date"][:10])
            if beat_date.timestamp() < cutoff - 86400:  # allow some slack for date parsing
                continue
        except Exception:
            continue
        peers = peers_map.get(sym, [])
        if peers:
            triggers.append((sym, data["beat_pct"], peers))

    if not triggers:
        logger.info("[Spillover] no triggers with peers in last 48h")
        _last_scan_date = today
        return results

    # Gather all peer symbols
    all_peers: set[str] = set()
    for _, _, peers in triggers:
        all_peers.update(peers)

    snaps = _get_snapshots(list(all_peers))
    equity = _get_equity()
    open_count = _open_position_count()

    for trigger_sym, beat_pct, peer_list in triggers:
        results["triggers"].append({"symbol": trigger_sym, "beat_pct": beat_pct})

        for peer in peer_list:
            if open_count >= _MAX_POSITIONS:
                break
            if _has_open_position(peer):
                continue

            snap = snaps.get(peer)
            if not snap:
                continue

            price = _current_price(snap)
            if not price or price < _MIN_PRICE:
                results["skipped"].append({"symbol": peer, "reason": "price_too_low"})
                continue

            vol = _daily_volume(snap)
            if vol < _MIN_VOLUME:
                results["skipped"].append({"symbol": peer, "reason": "low_volume"})
                continue

            # Check peer hasn't already moved too much
            try:
                bar = snap.daily_bar
                if bar:
                    day_move = abs((bar.close - bar.open) / bar.open * 100)
                    if day_move > _PEER_MAX_MOVED_PCT:
                        results["skipped"].append({"symbol": peer, "reason": "already_moved"})
                        continue
            except Exception:
                pass

            score = _score_peer(trigger_sym, peer, snap, beat_pct)
            if score < _SCORE_THRESHOLD:
                results["skipped"].append({"symbol": peer, "reason": f"score_too_low_{score}"})
                continue

            ask = _ask_price(snap) or price
            budget = equity * _POSITION_SIZE_PCT
            shares = max(1, int(budget / ask))
            stop = round(ask * (1 - _STOP_PCT), 2)
            target = round(ask * (1 + _TARGET_PCT), 2)

            ok = _place_limit_buy(peer, shares, ask)
            if ok:
                _record_open(peer, ask, shares, stop, target,
                             trigger_sym, beat_pct,
                             None,
                             f"trigger={trigger_sym} beat={beat_pct:.1f}% score={score}")
                results["bought"].append({"symbol": peer, "shares": shares, "price": ask,
                                          "trigger": trigger_sym})
                open_count += 1

    _last_scan_date = today
    logger.info(f"[Spillover] scan done: {len(results['bought'])} bought, "
                f"{len(results['triggers'])} triggers")
    return results


# ── Exit management ───────────────────────────────────────────────────────────

def check_exits():
    from services.brain.connectors.fmp_earnings import _earnings_cache
    positions = _get_open_positions()
    if not positions:
        return

    symbols = [p["symbol"] for p in positions]
    trigger_syms = list({p["trigger_symbol"] for p in positions if p["trigger_symbol"]})
    all_syms = list(set(symbols + trigger_syms))
    snaps = _get_snapshots(all_syms)

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

        # Trigger reversal: if the earnings-beat stock has now closed below pre-earnings price
        trig = pos.get("trigger_symbol")
        if trig:
            trig_data = _earnings_cache.get(trig)
            trig_snap = snaps.get(trig)
            if trig_data and trig_snap:
                trig_price = _current_price(trig_snap)
                if trig_price and trig_snap.daily_bar:
                    # Check if trigger reversed: currently below daily open AND below entry day
                    trig_bar = trig_snap.daily_bar
                    if trig_price < trig_bar.open * 0.98:  # -2% from open = reversal
                        _place_market_sell(symbol, pos["shares"])
                        _close_position(pos["id"], price, "trigger_reversal")
                        continue


# ── Scheduler ─────────────────────────────────────────────────────────────────

def _is_scan_time() -> bool:
    import zoneinfo
    now = datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    return (now.weekday() < 5 and
            now.hour == _SCAN_HOUR_ET and
            now.minute >= _SCAN_MINUTE_ET)


def _loop():
    _load_sector_peers()
    logger.info("[Spillover] scheduler started")
    while not _stop.is_set():
        try:
            if _is_configured() and _market_is_open():
                if _is_scan_time():
                    run_scan()
                check_exits()
        except Exception as e:
            logger.error(f"[Spillover] scheduler error: {e}")
        _stop.wait(_CHECK_INTERVAL)


def start_spillover_scheduler():
    global _thread
    if not _is_configured():
        logger.info("[Spillover] keys not set — scheduler not started")
        return
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, daemon=True, name="spillover-scheduler")
    _thread.start()
    logger.info("[Spillover] scheduler thread started")


# ── Status / summary helpers (used by router) ─────────────────────────────────

def get_status() -> dict:
    configured = _is_configured()
    out: dict = {"engine": "spillover", "configured": configured, "running": False}
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
                    FROM experiment_positions WHERE engine='spillover'
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
                           trigger_symbol, trigger_beat_pct, sector, notes
                    FROM experiment_positions WHERE engine='spillover' AND status=%s
                    ORDER BY entry_date DESC LIMIT 50
                """, (status_filter,))
            else:
                cur.execute("""
                    SELECT id, symbol, entry_price, entry_date, shares, stop_price,
                           target_price, status, exit_price, exit_date, realized_pl,
                           trigger_symbol, trigger_beat_pct, sector, notes
                    FROM experiment_positions WHERE engine='spillover'
                    ORDER BY entry_date DESC LIMIT 50
                """)
            rows = cur.fetchall()
        return [
            {"id": r[0], "symbol": r[1], "entry_price": r[2],
             "entry_date": r[3].isoformat() if r[3] else None,
             "shares": r[4], "stop_price": r[5], "target_price": r[6],
             "status": r[7], "exit_price": r[8],
             "exit_date": r[9].isoformat() if r[9] else None,
             "realized_pl": r[10], "trigger_symbol": r[11],
             "trigger_beat_pct": r[12], "sector": r[13], "notes": r[14]}
            for r in rows
        ]
    except Exception as e:
        logger.error(f"[Spillover] get_positions error: {e}")
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
                FROM experiment_positions WHERE engine='spillover'
            """)
            row = cur.fetchone()
        total, net_pl, wins, best, worst = row
        total = int(total or 0)
        return {
            "engine": "spillover",
            "total_trades": total,
            "net_pl": round(float(net_pl or 0), 2),
            "win_rate": round(100 * wins / total, 1) if total else None,
            "best_trade": round(float(best), 2) if best else None,
            "worst_trade": round(float(worst), 2) if worst else None,
        }
    except Exception as e:
        logger.error(f"[Spillover] get_summary error: {e}")
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
