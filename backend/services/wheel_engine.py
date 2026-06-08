"""
wheel_engine.py — Options Wheel Bot execution engine

Strategy:
  Phase 1 (put_open):    Sell cash-secured put. Collect premium.
  Phase 2 (assigned):    Put got assigned. We own 100 shares at put strike.
  Phase 3 (call_open):   Sell covered call above cost basis. Collect more premium.
  Phase 4 (completed):   Call expired or called away. Full cycle done.
  Repeat → perpetual premium income machine.

Full isolation from Kova:
  - Separate Alpaca account (ALPACA_WHEEL_KEY / ALPACA_WHEEL_SECRET)
  - ALPACA_WHEEL_PAPER=true (paper) or false (live) — change on Railway to go live
  - Zero imports from trading_engine, claude_service, or brain modules
  - Own tables: wheel_positions, wheel_universe, wheel_symbol_stats
  - strategy = 'wheel' tag on all position_log entries

AI usage:
  - Reads Kova's cached regime (read-only, no dependency on trading logic)
  - ask_ai() (Gemini Flash, free) for universe discovery (in wheel_universe.py)
  - No AI calls here — execution is pure math
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

# ── Execution config ───────────────────────────────────────────────────────────
MAX_ACTIVE_POSITIONS = 5          # max concurrent wheel positions
MIN_PREMIUM_YIELD = 0.015         # minimum premium / strike = 1.5%
MIN_DTE = 21                      # minimum days to expiry when opening
MAX_DTE = 45                      # maximum days to expiry when opening
TARGET_DELTA = 0.25               # ~25-delta put (OTM but earns meaningful premium)
ASSIGNMENT_CALL_BUFFER = 0.05     # sell covered call 5% above cost basis


# ── Alpaca client factory ──────────────────────────────────────────────────────

def _is_paper() -> bool:
    return settings.alpaca_wheel_paper.lower() not in ("false", "0", "no")


def _wheel_keys() -> tuple[str, str]:
    key = settings.alpaca_wheel_key or settings.alpaca_api_key
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


# ── Regime (read-only from Kova's cache) ──────────────────────────────────────

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
    """Adjust put delta based on regime — more conservative in bearish markets."""
    if regime == "bearish":
        return 0.15   # Further OTM
    elif regime == "bullish":
        return 0.30   # Closer, more premium
    return TARGET_DELTA


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
        logger.error(f"Wheel DB open_position error: {e}")
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
        logger.error(f"Wheel DB update_position error: {e}")


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
        logger.error(f"Wheel DB get_active_positions error: {e}")
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
                    COUNT(*) FILTER (WHERE status = 'active') as active_count,
                    COUNT(*) FILTER (WHERE status = 'completed') as completed_count,
                    COALESCE(SUM(total_premium_collected) FILTER (WHERE status = 'active'), 0) as active_premium,
                    COALESCE(SUM(total_premium_collected), 0) as total_premium_ever,
                    COALESCE(SUM(realized_pl) FILTER (WHERE status = 'completed'), 0) as total_realized_pl,
                    COALESCE(AVG(realized_pl) FILTER (WHERE status = 'completed'), 0) as avg_realized_pl
                FROM wheel_positions
            """)
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            return dict(zip(cols, row)) if row else {}
    except Exception as e:
        logger.error(f"Wheel DB get_summary error: {e}")
        return {}


# ── Scanner ───────────────────────────────────────────────────────────────────

def scan_opportunities() -> list[dict]:
    """
    Scan AI-selected universe for put-selling opportunities.
    Uses wheel_universe table — no hardcoded watchlist.
    Returns ranked opportunities WITHOUT placing orders.
    """
    regime = _get_current_regime()
    target_delta = _regime_adjusted_delta(regime)
    active = get_active_wheel_positions()
    active_symbols = {p["symbol"] for p in active}

    if len(active) >= MAX_ACTIVE_POSITIONS:
        logger.info(f"Wheel: at max positions ({MAX_ACTIVE_POSITIONS})")
        return []

    # Get universe from AI-discovered list (falls back to empty if not yet populated)
    from services.wheel_universe import get_active_universe
    universe = get_active_universe()

    if not universe:
        logger.warning("Wheel scan: universe is empty — run /wheel/universe/refresh first")
        return []

    today = date.today()
    expiry_min = today + timedelta(days=MIN_DTE)
    expiry_max = today + timedelta(days=MAX_DTE)
    opportunities = []

    trading_client = _get_wheel_trading_client()
    data_client = _get_wheel_data_client()

    for symbol in universe:
        if symbol in active_symbols:
            continue
        try:
            # Get current price
            from alpaca.data.requests import StockLatestQuoteRequest
            q = data_client.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=symbol)
            ).get(symbol)
            if not q:
                continue
            stock_price = float((q.ask_price + q.bid_price) / 2)
            if stock_price <= 0:
                continue

            # Get put contracts in DTE window
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

            opts_client = _get_wheel_options_client()
            best = None
            best_yield = 0.0

            for contract in contracts.option_contracts:
                try:
                    strike = float(contract.strike_price)
                    # Only consider strikes 3-20% below current price
                    ratio = strike / stock_price
                    if not (0.80 <= ratio <= 0.97):
                        continue

                    # Get option quote
                    from alpaca.data.requests import OptionSnapshotRequest
                    snaps = opts_client.get_option_snapshot(
                        OptionSnapshotRequest(symbol_or_symbols=contract.symbol)
                    )
                    snap = snaps.get(contract.symbol)
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
            logger.error(f"Wheel scan error {symbol}: {e}")

    opportunities.sort(key=lambda x: x["annual_yield_pct"], reverse=True)
    return opportunities


# ── Order execution ───────────────────────────────────────────────────────────

def execute_put(opportunity: dict) -> Optional[dict]:
    """Place cash-secured put. Returns order info or None."""
    try:
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderType

        client = _get_wheel_trading_client()
        regime = _get_current_regime()

        order = client.submit_order(LimitOrderRequest(
            symbol=opportunity["contract"],
            qty=1,
            side=OrderSide.SELL,
            type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=round(opportunity["premium"] * 0.98, 2),
        ))
        logger.info(f"Wheel PUT placed: {opportunity['symbol']} ${opportunity['strike']} exp {opportunity['expiry']} | {order.id}")

        pos_id = _open_wheel_position(
            symbol=opportunity["symbol"],
            phase="put_open",
            put_contract=opportunity["contract"],
            put_strike=opportunity["strike"],
            put_expiry=opportunity["expiry"],
            put_premium=opportunity["premium"],
            put_order_id=str(order.id),
            regime=regime,
        )
        return {"order_id": str(order.id), "position_db_id": pos_id, **opportunity}

    except Exception as e:
        logger.error(f"Wheel execute_put error: {e}")
        return None


def execute_covered_call(position: dict) -> Optional[dict]:
    """Sell covered call above cost basis after assignment."""
    try:
        from alpaca.trading.requests import GetOptionContractsRequest, LimitOrderRequest
        from alpaca.trading.enums import ContractType, OrderSide, TimeInForce, OrderType

        symbol = position["symbol"]
        cost_basis = position.get("cost_basis")
        if not cost_basis:
            logger.error(f"Wheel: no cost_basis for {symbol}")
            return None

        target_strike = float(cost_basis) * (1 + ASSIGNMENT_CALL_BUFFER)
        today = date.today()

        client = _get_wheel_trading_client()
        contracts = client.get_option_contracts(
            GetOptionContractsRequest(
                underlying_symbols=[symbol],
                type=ContractType.CALL,
                expiration_date_gte=str(today + timedelta(days=MIN_DTE)),
                expiration_date_lte=str(today + timedelta(days=MAX_DTE)),
            )
        )
        if not contracts or not contracts.option_contracts:
            logger.warning(f"Wheel: no call contracts for {symbol}")
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
                snaps = opts_client.get_option_snapshot(
                    OptionSnapshotRequest(symbol_or_symbols=contract.symbol)
                )
                snap = snaps.get(contract.symbol)
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
            logger.warning(f"Wheel: no suitable call for {symbol}")
            return None

        order = client.submit_order(LimitOrderRequest(
            symbol=best["contract_symbol"],
            qty=1,
            side=OrderSide.SELL,
            type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=round(best["premium"] * 0.98, 2),
        ))
        logger.info(f"Wheel CALL placed: {symbol} ${best['strike']} exp {best['expiry']} | {order.id}")

        prev_premium = float(position.get("total_premium_collected") or 0)
        _update_wheel_position(
            position["id"],
            phase="call_open",
            call_contract=best["contract_symbol"],
            call_strike=best["strike"],
            call_expiry=best["expiry"],
            call_premium=best["premium"],
            call_order_id=str(order.id),
            total_premium_collected=prev_premium + best["premium"] * 100,
        )
        return {"order_id": str(order.id), **best}

    except Exception as e:
        logger.error(f"Wheel execute_covered_call error: {e}")
        return None


# ── Assignment + expiration detection ─────────────────────────────────────────

def check_assignments():
    """Detect put assignments → sell covered calls."""
    active = get_active_wheel_positions()
    put_open = [p for p in active if p["phase"] == "put_open"]
    if not put_open:
        return

    try:
        client = _get_wheel_trading_client()
        held = {
            p.symbol: float(p.avg_entry_price)
            for p in client.get_all_positions()
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
    """Mark expired puts/calls as completed and log realized P&L."""
    today = date.today()
    active = get_active_wheel_positions()
    now = datetime.now(timezone.utc).isoformat()

    for pos in active:
        try:
            if pos["phase"] == "put_open" and pos.get("put_expiry"):
                expiry = pos["put_expiry"] if isinstance(pos["put_expiry"], date) \
                         else date.fromisoformat(str(pos["put_expiry"]))
                if expiry < today:
                    premium = float(pos.get("put_premium") or 0) * 100
                    logger.info(f"Wheel PUT expired worthless: {pos['symbol']} +${premium:.2f}")
                    _update_wheel_position(
                        pos["id"],
                        phase="completed", status="completed",
                        total_premium_collected=premium,
                        realized_pl=premium,
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
                    logger.info(f"Wheel CALL expired: {pos['symbol']} stock ${stock_pl:.2f} + prem ${total_prem:.2f} = ${total_pl:.2f}")
                    _update_wheel_position(
                        pos["id"],
                        phase="completed", status="completed",
                        realized_pl=total_pl,
                        closed_at=now,
                        notes=f"Full cycle. Stock: ${stock_pl:.2f}, Premiums: ${total_prem:.2f}",
                    )
        except Exception as e:
            logger.error(f"Wheel check_expirations pos {pos.get('id')}: {e}")


# ── Full cycle ─────────────────────────────────────────────────────────────────

def run_wheel_cycle():
    """
    Daily cycle: expirations → assignments → new puts (Mon/Wed only).
    Called by wheel_scheduler.
    """
    try:
        check_expirations()
    except Exception as e:
        logger.error(f"Wheel expirations: {e}")

    try:
        check_assignments()
    except Exception as e:
        logger.error(f"Wheel assignments: {e}")

    # Scan and trade on Mon + Wed only
    from datetime import datetime, timezone
    weekday = datetime.now(timezone.utc).weekday()
    if weekday in (0, 2):
        try:
            opps = scan_opportunities()
            slots = MAX_ACTIVE_POSITIONS - len(get_active_wheel_positions())
            for opp in opps[:min(2, slots)]:
                execute_put(opp)
        except Exception as e:
            logger.error(f"Wheel scan+execute: {e}")

    logger.info("Wheel cycle complete")


# ── Status (iOS dashboard) ─────────────────────────────────────────────────────

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
        "active_positions": active,
        "active_count": len(active),
        "max_positions": MAX_ACTIVE_POSITIONS,
        "summary": summary,
        "universe": universe,
        "universe_count": len(universe),
        "config": {
            "min_premium_yield_pct": MIN_PREMIUM_YIELD * 100,
            "min_dte": MIN_DTE,
            "max_dte": MAX_DTE,
            "target_delta": TARGET_DELTA,
            "max_positions": MAX_ACTIVE_POSITIONS,
        },
    }
