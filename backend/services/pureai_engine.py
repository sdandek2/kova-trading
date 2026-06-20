"""
pureai_engine.py — Pure-AI trading experiment (third Alpaca paper account).

The ablation test: Kova = data pipeline + rules + AI. This = AI alone.
Every cycle the AI receives ONLY its own portfolio state (holdings, entry
prices, current prices, P&L, cash) plus today's date, researches whatever it
wants via server-side web search, and returns buy/sell/add decisions as JSON.
No scored candidates, no regime, no RS ranks, no curated news — by design.
After 30 days we compare expectancy, win rate, drawdown, and vs-SPY against
Kova over the identical window.

Rails (protect the measurement, not the trader):
  - long-only US stocks, no leverage/options/shorts
  - max N buys per cycle (default 3, app-configurable)
  - max position 15% of equity; ticker validated against Alpaca assets
  - unlimited sells/adds on its own holdings
  - every decision + every search query logged to pureai_decisions

Completely isolated from Kova and Wheel: own Alpaca account, own tables,
own daemon-thread scheduler. Never touches their DB tables or logic.
"""

import json
import logging
import threading
from datetime import datetime, timezone

from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

# ── Defaults (app-configurable via /pureai/config, stored in DB cache) ───────
_SETTINGS_CACHE_KEY  = "pureai:settings"
_RESERVE_CACHE_KEY   = "pureai:reserved_cash"
_DEFAULT_SETTINGS = {
    "enabled": True,
    "max_buys_per_cycle": 3,        # user-requested default; 0..N buys per cycle
    "cycle_interval_minutes": 60,   # hourly during market hours
    "max_position_pct": 0.15,       # hard cap per position
    "max_searches_per_cycle": 8,
    "model": "claude-opus-4-8",     # app-configurable; env var is the hard default
    "profit_reserve_pct": 0.0,      # % of each realized profit moved to reserve (0 = disabled)
}

PUREAI_MODEL_OPTIONS = [
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]


def get_pureai_settings() -> dict:
    try:
        from services.db import cache_get
        cached = cache_get(_SETTINGS_CACHE_KEY)
        if isinstance(cached, dict):
            return {**_DEFAULT_SETTINGS, **cached}
    except Exception:
        pass
    return _DEFAULT_SETTINGS.copy()


def update_pureai_settings(updates: dict) -> dict:
    merged = {**get_pureai_settings(), **{k: v for k, v in updates.items()
                                          if k in _DEFAULT_SETTINGS and k != "model_options"}}
    try:
        from services.db import cache_set
        cache_set(_SETTINGS_CACHE_KEY, merged, 365 * 24 * 3600)
    except Exception as e:
        logger.error(f"PureAI settings save failed: {e}")
    return merged


# ── Profit reserve ───────────────────────────────────────────────────────────

def get_pureai_reserve() -> float:
    try:
        from services.db import cache_get
        v = cache_get(_RESERVE_CACHE_KEY)
        return float(v) if v is not None else 0.0
    except Exception:
        return 0.0


def _add_to_pureai_reserve(amount: float) -> float:
    try:
        from services.db import cache_get, cache_set
        current = get_pureai_reserve()
        new_total = round(current + amount, 2)
        cache_set(_RESERVE_CACHE_KEY, new_total, 365 * 24 * 3600)
        logger.info(f"PureAI reserve: +${amount:.2f} → total ${new_total:.2f}")
        return new_total
    except Exception as e:
        logger.error(f"PureAI reserve update error: {e}")
        return 0.0


def _add_to_pureai_reserve_if_configured(profit: float):
    cfg = get_pureai_settings()
    pct = float(cfg.get("profit_reserve_pct", 0))
    if pct > 0 and profit > 0:
        _add_to_pureai_reserve(round(profit * pct / 100, 2))


# ── Alpaca clients (separate account) ────────────────────────────────────────

def _is_configured() -> bool:
    return bool(settings.alpaca_pureai_key and settings.alpaca_pureai_secret)


def _is_paper() -> bool:
    return "paper" in settings.alpaca_pureai_base_url.lower()


def _get_trading_client():
    from alpaca.trading.client import TradingClient
    return TradingClient(settings.alpaca_pureai_key,
                         settings.alpaca_pureai_secret, paper=_is_paper())


def _get_data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(settings.alpaca_pureai_key,
                                     settings.alpaca_pureai_secret)


def _market_is_open() -> bool:
    try:
        return bool(_get_trading_client().get_clock().is_open)
    except Exception as e:
        logger.warning(f"PureAI clock check failed: {e}")
        return False


# ── DB tables (own namespace; created on first use) ──────────────────────────

def _ensure_tables():
    from services.db import _get_conn
    conn = _get_conn()
    if not conn:
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pureai_decisions (
                id          SERIAL PRIMARY KEY,
                timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                model       TEXT,
                searches    JSONB,
                raw_response TEXT,
                decision    JSONB,
                executed    JSONB,
                error       TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pureai_positions (
                id              SERIAL PRIMARY KEY,
                symbol          TEXT NOT NULL,
                entry_time      TIMESTAMPTZ,
                exit_time       TIMESTAMPTZ,
                entry_price     FLOAT,
                exit_price      FLOAT,
                quantity        FLOAT,
                realized_pl     FLOAT,
                realized_pl_pct FLOAT,
                open_thesis     TEXT,
                close_reason    TEXT
            )
        """)
    conn.commit()


def _log_decision(model: str, searches: list, raw: str,
                  decision: Optional[dict], executed: list, error: Optional[str]):
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pureai_decisions
                    (model, searches, raw_response, decision, executed, error)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (model, json.dumps(searches), raw,
                  json.dumps(decision) if decision else None,
                  json.dumps(executed), error))
        conn.commit()
    except Exception as e:
        logger.error(f"PureAI decision log failed: {e}")


def _log_position_open(symbol: str, qty: float, price: float, thesis: str):
    try:
        from services.db import _get_conn
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO pureai_positions
                    (symbol, entry_time, entry_price, quantity, open_thesis)
                VALUES (%s, NOW(), %s, %s, %s)
            """, (symbol, price, qty, thesis))
        conn.commit()
    except Exception as e:
        logger.error(f"PureAI position open log failed: {e}")


def _log_position_close(symbol: str, exit_price: float, reason: str):
    """Close the oldest open row for symbol; compute realized P&L."""
    try:
        from services.db import _get_conn
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, entry_price, quantity FROM pureai_positions
                WHERE symbol = %s AND exit_time IS NULL
                ORDER BY entry_time ASC LIMIT 1
            """, (symbol,))
            row = cur.fetchone()
            if not row:
                return
            pos_id, ep, qty = row
            pl = (exit_price - (ep or 0)) * (qty or 0)
            pl_pct = ((exit_price - ep) / ep * 100) if ep else None
            cur.execute("""
                UPDATE pureai_positions SET
                    exit_time = NOW(), exit_price = %s,
                    realized_pl = %s, realized_pl_pct = %s, close_reason = %s
                WHERE id = %s
            """, (exit_price, pl, pl_pct, reason, pos_id))
        conn.commit()
        if pl > 0:
            _add_to_pureai_reserve_if_configured(pl)
    except Exception as e:
        logger.error(f"PureAI position close log failed: {e}")


# ── Portfolio state → prompt ─────────────────────────────────────────────────

def _get_open_theses() -> dict:
    """symbol → original buy thesis for open positions (latest per symbol)."""
    try:
        from services.db import _get_conn
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (symbol) symbol, open_thesis
                FROM pureai_positions
                WHERE exit_time IS NULL AND open_thesis IS NOT NULL
                ORDER BY symbol, entry_time DESC
            """)
            return {row[0]: row[1] for row in cur.fetchall()}
    except Exception:
        return {}


def _get_track_record(limit: int = 10) -> dict:
    """Closed-trade history fed back to the AI — its own memory, not curation.
    Last N trades for recency plus aggregates (avg win vs avg loss is the
    diagnostic that exposes cutting-winners/riding-losers behavior)."""
    out = {"trades": [], "total": 0, "wins": 0, "total_pl": 0.0,
           "avg_win": None, "avg_loss": None}
    try:
        from services.db import _get_conn
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, entry_price, exit_price, realized_pl,
                       realized_pl_pct, open_thesis, close_reason
                FROM pureai_positions
                WHERE exit_time IS NOT NULL
                ORDER BY exit_time DESC LIMIT %s
            """, (limit,))
            for r in cur.fetchall():
                out["trades"].append({
                    "symbol": r[0],
                    "entry": r[1],
                    "exit": r[2],
                    "pl": round(float(r[3] or 0), 2),
                    "pl_pct": round(float(r[4] or 0), 1),
                    "your_thesis_was": r[5],
                    "you_sold_because": r[6],
                })
            cur.execute("""
                SELECT COUNT(*),
                       COUNT(*) FILTER (WHERE realized_pl > 0),
                       COALESCE(SUM(realized_pl), 0),
                       AVG(realized_pl) FILTER (WHERE realized_pl > 0),
                       AVG(realized_pl) FILTER (WHERE realized_pl <= 0)
                FROM pureai_positions WHERE exit_time IS NOT NULL
            """)
            total, wins, pl, avg_w, avg_l = cur.fetchone()
            out.update(
                total=int(total or 0), wins=int(wins or 0),
                total_pl=round(float(pl or 0), 2),
                avg_win=round(float(avg_w), 2) if avg_w is not None else None,
                avg_loss=round(float(avg_l), 2) if avg_l is not None else None,
            )
    except Exception:
        pass
    return out


def _get_portfolio_state() -> dict:
    client = _get_trading_client()
    acct = client.get_account()
    positions = client.get_all_positions()
    holdings = []
    for p in positions:
        holdings.append({
            "symbol": p.symbol,
            "qty": float(p.qty),
            "entry_price": float(p.avg_entry_price),
            "current_price": float(p.current_price or 0),
            "unrealized_pl": round(float(p.unrealized_pl or 0), 2),
            "unrealized_pl_pct": round(float(p.unrealized_plpc or 0) * 100, 2),
        })
    reserved = get_pureai_reserve()
    tradeable_cash = max(0.0, float(acct.cash) - reserved)
    return {
        "equity": float(acct.equity),
        "cash": tradeable_cash,
        "holdings": holdings,
    }


def _build_prompt(state: dict, cfg: dict, record: Optional[dict] = None) -> str:
    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    holdings_json = json.dumps(state["holdings"], indent=2) if state["holdings"] \
        else "[] (no positions — all cash)"
    max_buys = int(cfg["max_buys_per_cycle"])
    max_pos = cfg["max_position_pct"]

    track_section = ""
    if record and record["total"] > 0:
        wr = round(100 * record["wins"] / record["total"], 1)
        avg_w = f"${record['avg_win']:,.2f}" if record["avg_win"] is not None else "n/a"
        avg_l = f"${record['avg_loss']:,.2f}" if record["avg_loss"] is not None else "n/a"
        trades_json = json.dumps(record["trades"], indent=2)
        track_section = f"""

## Your track record so far
Closed trades: {record['total']} | Win rate: {wr}% | Total realized P&L: ${record['total_pl']:,.2f}
Average win: {avg_w} | Average loss: {avg_l}
Your last {len(record['trades'])} closed trades (most recent first):
{trades_json}
Review honestly: which theses worked, which didn't, and whether your average
loss is outrunning your average win. Adjust your decisions accordingly."""

    return f"""You are an autonomous stock trader managing a real portfolio. Today is {today}.

## Your portfolio right now
Equity: ${state['equity']:,.2f} | Cash available: ${state['cash']:,.2f}
Holdings:
{holdings_json}{track_section}

## Your job this cycle
Research the current market using web search — look up whatever YOU think matters
(market conditions, news on your holdings, candidates you're considering). Then decide:

1. BUY: 0 to {max_buys} US-listed stocks. Only buy when you have genuine conviction.
   Default position size is 10% of equity. High conviction (clear catalyst, strong
   edge) → 12–{max_pos:.0%}. Only go below 8% for thin liquidity or small-cap risk.
   Don't spread yourself thin — 1-2 high-conviction trades beat 3 mediocre ones.
2. SELL: any of your current holdings, partially or fully, for any reason.
   Each holding includes your_original_thesis (why you bought it) — re-evaluate it
   every cycle. If the original reason you bought a stock no longer holds, sell it.
   Don't anchor to purchase price or wait for price to confirm what you already know.
3. ADD: increase an existing holding (counts toward the {max_pos:.0%} position cap).
   Use this when you have strong conviction a holding is about to move significantly
   higher — a clear catalyst like an upcoming earnings beat, major product launch,
   regulatory approval, or sector tailwind that makes the upside asymmetric. An add
   is not just "thesis still intact" — it requires a specific near-term reason for
   outsized upside.

Rules: long-only US stocks. No options, no leveraged/inverse ETFs, no crypto tokens
(crypto-related stocks allowed if you have conviction), no penny stocks under $3.

## Response format — STRICT JSON only, no prose outside the JSON:
{{
  "market_view": "1-2 sentence summary of what you found and your stance",
  "buys": [{{"symbol": "TICKER", "dollars": 2500, "thesis": "one-line why"}}],
  "sells": [{{"symbol": "TICKER", "portion": "all" | 0.5, "reason": "one-line why"}}],
  "adds": [{{"symbol": "TICKER", "dollars": 1000, "thesis": "one-line why"}}]
}}
Empty arrays are perfectly fine. Be honest in the thesis — it's how your decisions
are audited later."""


# ── Validation & execution ────────────────────────────────────────────────────

def _validate_symbol(client, symbol: str) -> bool:
    """Ticker must exist on Alpaca, be active, tradable US equity."""
    try:
        asset = client.get_asset(symbol)
        return bool(asset and asset.tradable and str(asset.status).lower().endswith("active"))
    except Exception:
        return False


def _latest_price(symbol: str) -> float:
    try:
        from alpaca.data.requests import StockLatestQuoteRequest
        q = _get_data_client().get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=symbol)).get(symbol)
        if q and q.bid_price and q.ask_price:
            return float((q.ask_price + q.bid_price) / 2)
    except Exception:
        pass
    return 0.0


def run_pureai_cycle(force: bool = False) -> dict:
    """One full decision cycle. Returns a summary dict (also logged to DB)."""
    if not _is_configured():
        return {"status": "skipped", "reason": "ALPACA_PUREAI_KEY/SECRET not set"}
    cfg = get_pureai_settings()
    if not cfg.get("enabled") and not force:
        return {"status": "skipped", "reason": "disabled in settings"}
    if not force and not _market_is_open():
        return {"status": "skipped", "reason": "market closed"}

    _ensure_tables()
    # Model: DB-stored setting takes priority over env var (so app can override)
    model = cfg.get("model") or getattr(settings, "pureai_model", "claude-opus-4-8")
    executed: list[dict] = []
    raw, searches, decision, error = "", [], None, None

    try:
        state = _get_portfolio_state()
        # Feed back the AI's own buy thesis for each open position so it can
        # re-evaluate its original reasoning ("is X still true?") on later cycles.
        theses = _get_open_theses()
        for h in state["holdings"]:
            if h["symbol"] in theses:
                h["your_original_thesis"] = theses[h["symbol"]]
        prompt = _build_prompt(state, cfg, record=_get_track_record())

        from services.ai_client import ask_ai_with_search, parse_ai_json
        raw, searches = ask_ai_with_search(
            prompt, model=model, max_searches=int(cfg["max_searches_per_cycle"]))
        decision = parse_ai_json(raw)

        client = _get_trading_client()
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        held = {h["symbol"]: h for h in state["holdings"]}
        max_pos_dollars = state["equity"] * float(cfg["max_position_pct"])

        # ── SELLS first (frees cash for buys) — unlimited by design ─────────
        for s in (decision.get("sells") or []):
            sym = str(s.get("symbol", "")).upper()
            if sym not in held:
                executed.append({"action": "sell", "symbol": sym,
                                 "status": "rejected", "why": "not held"})
                continue
            portion = s.get("portion", "all")
            qty = held[sym]["qty"] if portion == "all" \
                else max(round(held[sym]["qty"] * float(portion), 4), 0)
            if qty <= 0:
                continue
            try:
                order = client.submit_order(MarketOrderRequest(
                    symbol=sym, qty=qty, side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY))
                px = _latest_price(sym) or held[sym]["current_price"]
                if portion == "all":
                    _log_position_close(sym, px, s.get("reason", ""))
                executed.append({"action": "sell", "symbol": sym, "qty": qty,
                                 "status": "submitted", "order_id": str(order.id),
                                 "reason": s.get("reason", "")})
            except Exception as e:
                executed.append({"action": "sell", "symbol": sym,
                                 "status": "error", "why": str(e)})

        # ── BUYS (capped at max_buys_per_cycle) + ADDS ───────────────────────
        buys = (decision.get("buys") or [])[: int(cfg["max_buys_per_cycle"])]
        adds = decision.get("adds") or []
        for b, is_add in [(x, False) for x in buys] + [(x, True) for x in adds]:
            sym = str(b.get("symbol", "")).upper()
            dollars = float(b.get("dollars") or 0)
            if not sym or dollars <= 0:
                continue
            if not _validate_symbol(client, sym):
                executed.append({"action": "add" if is_add else "buy",
                                 "symbol": sym, "status": "rejected",
                                 "why": "invalid/untradable ticker"})
                continue
            px = _latest_price(sym)
            if px < 3.0:
                executed.append({"action": "buy", "symbol": sym,
                                 "status": "rejected",
                                 "why": f"price ${px:.2f} < $3 floor or no quote"})
                continue
            # Position cap: existing exposure + new dollars ≤ 15% of equity
            existing = held.get(sym, {}).get("qty", 0) * \
                       held.get(sym, {}).get("current_price", 0)
            if existing + dollars > max_pos_dollars:
                dollars = max(max_pos_dollars - existing, 0)
                if dollars < 100:
                    executed.append({"action": "buy", "symbol": sym,
                                     "status": "rejected", "why": "position cap"})
                    continue
            # Cash guard — don't attempt if clearly insufficient
            if dollars > state["cash"] * 1.01:
                dollars = min(dollars, state["cash"] * 0.99)
                if dollars < 100:
                    executed.append({"action": "buy", "symbol": sym,
                                     "status": "rejected", "why": "insufficient cash"})
                    continue
            dollars = round(dollars, 2)
            est_qty = round(dollars / px, 4) if px > 0 else 0
            try:
                # Notional (dollar-based) orders avoid fractional-share account
                # restrictions. Alpaca requires DAY for all notional orders.
                order = client.submit_order(MarketOrderRequest(
                    symbol=sym, notional=dollars, side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY))
                if not is_add:
                    _log_position_open(sym, est_qty, px, b.get("thesis", ""))
                executed.append({"action": "add" if is_add else "buy",
                                 "symbol": sym, "qty": est_qty, "dollars": dollars,
                                 "status": "submitted", "order_id": str(order.id),
                                 "thesis": b.get("thesis", "")})
            except Exception as e:
                executed.append({"action": "buy", "symbol": sym,
                                 "status": "error", "why": str(e)})

    except Exception as e:
        error = str(e)
        logger.error(f"PureAI cycle error: {e}")

    _log_decision(model, searches, raw, decision, executed, error)
    summary = {
        "status": "error" if error else "ok",
        "error": error,
        "searches": searches,
        "market_view": (decision or {}).get("market_view"),
        "executed": executed,
    }
    logger.info(f"PureAI cycle done: {len(executed)} actions, "
                f"{len(searches)} searches{', ERROR: ' + error if error else ''}")
    return summary


# ── Scheduler (daemon thread, like wheel_scheduler) ───────────────────────────

_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def _loop():
    import time
    logger.info("PureAI scheduler started")
    while not _stop.is_set():
        try:
            cfg = get_pureai_settings()
            if cfg.get("enabled") and _is_configured() and _market_is_open():
                run_pureai_cycle()
            interval = max(int(cfg.get("cycle_interval_minutes", 60)), 10) * 60
        except Exception as e:
            logger.error(f"PureAI scheduler error: {e}")
            interval = 600
        _stop.wait(interval)


def start_pureai_scheduler():
    global _thread
    if not _is_configured():
        logger.info("PureAI: keys not set — scheduler not started")
        return
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, daemon=True, name="pureai-scheduler")
    _thread.start()


# ── Status for the app ────────────────────────────────────────────────────────

def get_pureai_status() -> dict:
    out = {"configured": _is_configured(), "paper": _is_paper(),
           "settings": get_pureai_settings(),
           "model": getattr(settings, "pureai_model", "claude-opus-4-8")}
    if not _is_configured():
        return out
    try:
        state = _get_portfolio_state()
        out.update(state)
    except Exception as e:
        out["account_error"] = str(e)
    try:
        from services.db import _get_conn
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FILTER (WHERE exit_time IS NOT NULL),
                       COALESCE(SUM(realized_pl) FILTER (WHERE exit_time IS NOT NULL), 0),
                       COUNT(*) FILTER (WHERE exit_time IS NOT NULL AND realized_pl > 0)
                FROM pureai_positions
            """)
            closed, total_pl, wins = cur.fetchone()
            out["closed_trades"] = int(closed or 0)
            out["realized_pl"] = round(float(total_pl or 0), 2)
            out["win_rate"] = round(100 * wins / closed, 1) if closed else None
    except Exception:
        pass
    out["reserved_cash"] = get_pureai_reserve()
    return out


def get_recent_decisions(limit: int = 20) -> list[dict]:
    try:
        from services.db import _get_conn
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT timestamp, model, searches, decision, executed, error
                FROM pureai_decisions ORDER BY timestamp DESC LIMIT %s
            """, (limit,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"PureAI decisions fetch failed: {e}")
        return []
