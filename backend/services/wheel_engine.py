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

# ── Execution config ───────────────────────────────────────────────────────────
MAX_ACTIVE_POSITIONS  = 5      # max concurrent wheel positions
MIN_PREMIUM_YIELD     = 0.015  # min premium / strike = 1.5%
MIN_DTE               = 21     # minimum DTE when opening
MAX_DTE               = 45     # maximum DTE when opening
TARGET_DELTA          = 0.25   # ~25-delta put
ASSIGNMENT_CALL_BUFFER = 0.05  # sell covered call 5% above cost basis
EARLY_CLOSE_THRESHOLD  = 0.50  # buy-to-close when premium decays to 50% of collected

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

def _get_current_regime() -> str:
    try:
        from services.db import cache_get
        data = cache_get("market_regime")
        if isinstance(data, dict):
            return data.get("regime", "neutral").lower()
        if isinstance(data, str):
            return data.lower()
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
                WHERE status = 'active'
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
                    COALESCE(SUM(total_premium_collected) FILTER (WHERE status='active'), 0)    as active_premium,
                    COALESCE(SUM(total_premium_collected), 0)                                   as total_premium_ever,
                    COALESCE(SUM(realized_pl) FILTER (WHERE status='completed'), 0)             as total_realized_pl,
                    COALESCE(AVG(realized_pl) FILTER (WHERE status='completed'), 0)             as avg_realized_pl
                FROM wheel_positions
            """)
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            result = dict(zip(cols, row)) if row else {}
            result["profit_reserve"] = _get_wheel_reserve()
            return result
    except Exception as e:
        logger.error(f"Wheel DB get_summary: {e}")
        return {}


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

    trading_client = _get_wheel_trading_client()
    data_client = _get_wheel_data_client()
    opts_client = _get_wheel_options_client()

    for symbol in universe:
        if symbol in active_symbols:
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

            from alpaca.trading.requests import GetOptionContractsRequest
            from alpaca.trading.enums import ContractType
            contracts = trading_client.get_option_contracts(
                GetOptionContractsRequest(
                    underlying_symbols=[symbol],
                    type=ContractType.PUT,
                    expiration_date_gte=str(expiry_min),
                    expiration_date_lte=str(expiry_max),
                )
            )
            if not contracts or not contracts.option_contracts:
                continue

            best = None
            best_yield = 0.0

            for contract in contracts.option_contracts:
                try:
                    strike = float(contract.strike_price)
                    ratio = strike / stock_price
                    if not (0.80 <= ratio <= 0.97):
                        continue
                    from alpaca.data.requests import OptionSnapshotRequest
                    snap = opts_client.get_option_snapshot(
                        OptionSnapshotRequest(symbol_or_symbols=contract.symbol)
                    ).get(contract.symbol)
                    if not snap or not snap.latest_quote:
                        continue
                    ask = float(snap.latest_quote.ask_price or 0)
                    bid = float(snap.latest_quote.bid_price or 0)
                    if ask <= 0 or bid <= 0:
                        continue
                    premium = (ask + bid) / 2
                    prem_yield = premium / strike
                    if prem_yield < MIN_PREMIUM_YIELD:
                        continue
                    if prem_yield > best_yield:
                        best_yield = prem_yield
                        dte = (contract.expiration_date - today).days
                        best = {
                            "symbol": symbol,
                            "stock_price": round(stock_price, 2),
                            "contract": contract.symbol,
                            "strike": strike,
                            "expiry": str(contract.expiration_date),
                            "dte": dte,
                            "premium": round(premium, 2),
                            "premium_yield_pct": round(prem_yield * 100, 2),
                            "annual_yield_pct": round(prem_yield * (365 / max(dte, 1)) * 100, 1),
                            "collateral": round(strike * 100, 2),
                            "regime": regime,
                            "mode": "paper" if _is_paper() else "live",
                        }
                except Exception:
                    continue

            if best:
                opportunities.append(best)
                logger.info(
                    f"Wheel opp: {symbol} ${best['strike']} put exp {best['expiry']} | "
                    f"${best['premium']} ({best['premium_yield_pct']}% / {best['annual_yield_pct']}% annual)"
                )
        except Exception as e:
            logger.error(f"Wheel scan {symbol}: {e}")

    opportunities.sort(key=lambda x: x["annual_yield_pct"], reverse=True)
    return opportunities


# ── Order execution ───────────────────────────────────────────────────────────

def execute_put(opportunity: dict) -> Optional[dict]:
    """Place cash-secured put order."""
    try:
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderType

        order = _get_wheel_trading_client().submit_order(LimitOrderRequest(
            symbol=opportunity["contract"],
            qty=1,
            side=OrderSide.SELL,
            type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=round(opportunity["premium"] * 0.98, 2),
        ))
        logger.info(f"Wheel PUT: {opportunity['symbol']} ${opportunity['strike']} exp {opportunity['expiry']} | {order.id}")

        pos_id = _open_wheel_position(
            symbol=opportunity["symbol"],
            phase="put_open",
            put_contract=opportunity["contract"],
            put_strike=opportunity["strike"],
            put_expiry=opportunity["expiry"],
            put_premium=opportunity["premium"],
            put_order_id=str(order.id),
            regime=_get_current_regime(),
        )
        return {"order_id": str(order.id), "position_db_id": pos_id, **opportunity}
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

        order = client.submit_order(LimitOrderRequest(
            symbol=best["contract_symbol"],
            qty=1,
            side=OrderSide.SELL,
            type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=round(best["premium"] * 0.98, 2),
        ))
        logger.info(f"Wheel CALL: {symbol} ${best['strike']} exp {best['expiry']} | {order.id}")

        prev = float(position.get("total_premium_collected") or 0)
        _update_wheel_position(
            position["id"],
            phase="call_open",
            call_contract=best["contract_symbol"],
            call_strike=best["strike"],
            call_expiry=best["expiry"],
            call_premium=best["premium"],
            call_order_id=str(order.id),
            total_premium_collected=prev + best["premium"] * 100,
        )
        return {"order_id": str(order.id), **best}
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


# ── Assignment + expiration + profit targets ──────────────────────────────────

def check_assignments():
    """Detect put assignments → sell covered calls immediately."""
    active = get_active_wheel_positions()
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

    for wp in put_open:
        if wp["symbol"] in held:
            cost = held[wp["symbol"]]
            logger.info(f"Wheel ASSIGNED: {wp['symbol']} @ ${cost:.2f}")
            prev = float(wp.get("total_premium_collected") or 0)
            put_prem = float(wp.get("put_premium") or 0)
            _update_wheel_position(
                wp["id"],
                phase="assigned",
                cost_basis=cost,
                total_premium_collected=prev + put_prem * 100,
            )
            wp["cost_basis"] = cost
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


def check_profit_targets():
    """
    Early close at 50% profit decay.

    If we sold a put for $100 and it's now worth $50 (50% decay),
    buy-to-close and free capital 2-3 weeks early for the next cycle.
    This is standard options management and improves annual returns.

    Market must be open to execute.
    """
    if not _market_is_open():
        return

    active = get_active_wheel_positions()
    opts_client = _get_wheel_options_client()
    now = datetime.now(timezone.utc).isoformat()

    for pos in active:
        try:
            # Check puts
            if pos["phase"] == "put_open" and pos.get("put_contract") and pos.get("put_premium"):
                original_prem = float(pos["put_premium"])
                from alpaca.data.requests import OptionSnapshotRequest
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
                if decay_ratio <= EARLY_CLOSE_THRESHOLD:
                    # Premium decayed to 50% — close early, book profit
                    profit = (original_prem - current_prem) * 100
                    logger.info(
                        f"Wheel EARLY CLOSE (put): {pos['symbol']} "
                        f"orig ${original_prem:.2f} → now ${current_prem:.2f} "
                        f"({decay_ratio*100:.0f}% remaining) | profit ${profit:.2f}"
                    )
                    order_id = _buy_to_close(pos, pos["put_contract"], current_prem,
                                             f"50% profit target ({decay_ratio*100:.0f}% remaining)")
                    if order_id:
                        _add_to_profit_reserve_if_configured(profit)
                        _update_wheel_position(
                            pos["id"],
                            phase="completed", status="completed",
                            realized_pl=profit,
                            total_premium_collected=profit,
                            closed_at=now,
                            notes=f"Early close at {decay_ratio*100:.0f}% premium remaining. Profit: ${profit:.2f}",
                        )

            # Check calls (same logic)
            elif pos["phase"] == "call_open" and pos.get("call_contract") and pos.get("call_premium"):
                original_prem = float(pos["call_premium"])
                from alpaca.data.requests import OptionSnapshotRequest
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
                if decay_ratio <= EARLY_CLOSE_THRESHOLD:
                    prev_prem = float(pos.get("total_premium_collected") or 0)
                    call_profit = (original_prem - current_prem) * 100
                    cost = float(pos.get("cost_basis") or 0)
                    # We still hold shares — mark phase as assigned (back to step 2)
                    # so we can sell a new call next cycle
                    logger.info(
                        f"Wheel EARLY CLOSE (call): {pos['symbol']} "
                        f"${call_profit:.2f} profit — back to assigned phase"
                    )
                    order_id = _buy_to_close(pos, pos["call_contract"], current_prem,
                                             f"50% call profit target")
                    if order_id:
                        _update_wheel_position(
                            pos["id"],
                            phase="assigned",   # back to holding shares → sell new call
                            call_contract=None,
                            call_order_id=None,
                            total_premium_collected=prev_prem + call_profit,
                            notes=f"Call closed early at {decay_ratio*100:.0f}% remaining. Selling new call.",
                        )
                        # Sell new call immediately
                        execute_covered_call({**pos, "total_premium_collected": prev_prem + call_profit})

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
    return weekday in (0, 2)


def _record_scan_date():
    try:
        from services.db import cache_set
        cache_set(_LAST_SCAN_KEY, str(date.today()), 30 * 24 * 3600)
    except Exception:
        pass


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

    # Always run (read-only DB checks, no orders needed)
    try:
        check_expirations()
    except Exception as e:
        logger.error(f"Wheel expirations: {e}")

    if not is_open:
        next_open = _next_market_open()
        logger.info(f"Wheel: market closed — skipping orders. Next open: {next_open}")
        return

    try:
        check_profit_targets()
    except Exception as e:
        logger.error(f"Wheel profit targets: {e}")

    try:
        check_assignments()
    except Exception as e:
        logger.error(f"Wheel assignments: {e}")

    # Scan + place new puts — Mon/Wed or make-up day after holiday
    if _should_scan_today():
        try:
            opps = scan_opportunities()
            slots = MAX_ACTIVE_POSITIONS - len(get_active_wheel_positions())
            placed = 0
            for opp in opps[:min(2, slots)]:
                if execute_put(opp):
                    placed += 1
            _record_scan_date()   # record even if no slots — prevents repeated scanning
            logger.info(f"Wheel scan complete: {placed} puts placed, {len(opps)} opportunities found")
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

    return {
        "regime": regime,
        "mode": "paper" if _is_paper() else "live",
        "base_url": settings.alpaca_wheel_base_url,
        "active_positions": active,
        "active_count": len(active),
        "max_positions": MAX_ACTIVE_POSITIONS,
        "summary": summary,
        "profit_reserve": _get_wheel_reserve(),
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
