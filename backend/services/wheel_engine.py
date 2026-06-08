"""
wheel_engine.py — Options Wheel Bot for Kova

Strategy:
  Phase 1: Sell cash-secured puts on quality stocks (premium income)
  Phase 2: If assigned (own 100 shares), sell covered calls above cost basis
  Repeat indefinitely — collect premium every cycle

Completely isolated from Kova equity trading:
  - Separate Alpaca paper account (ALPACA_WHEEL_KEY / ALPACA_WHEEL_SECRET)
  - Falls back to main Alpaca keys if wheel keys not set
  - Same DB (position_log + wheel_positions tables)
  - strategy = 'wheel' tag distinguishes all records

AI integration (light):
  - Reads Kova's current market regime before scanning
  - Bearish regime → go further OTM (safer strikes)
  - Bullish regime → normal strikes, full deployment
  - No Claude API calls per trade — regime is already computed by Kova
"""

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

# ── Wheel watchlist ────────────────────────────────────────────────────────────
# Stocks you'd be comfortable owning if assigned.
# Criteria: liquid options, price $5-$60, you believe in long-term.
WATCHLIST = [
    "SOFI",   # ~$15  — fintech, high IV, liquid options
    "PLTR",   # ~$25  — AI/data, high retail interest
    "HOOD",   # ~$25  — fintech, volatile = rich premiums
    "COIN",   # ~$25  — crypto proxy, high IV
    "UBER",   # ~$80  — but we'll go further OTM
    "SNAP",   # ~$10  — high IV
    "AMD",    # ~$110 — use spreads if price too high
    "RIVN",   # ~$12  — EV, high IV
    "F",      # ~$10  — Ford, stable, low premium but safe
    "PENN",   # ~$15  — gaming/sports betting
]

# ── Config ─────────────────────────────────────────────────────────────────────
MAX_ACTIVE_POSITIONS = 5          # max concurrent wheel positions on $25k
MIN_PREMIUM_YIELD = 0.015         # min premium / strike = 1.5% (annualizes to ~18%)
MIN_DTE = 21                      # minimum days to expiry
MAX_DTE = 45                      # maximum days to expiry
TARGET_DELTA = 0.25               # ~25 delta = OTM but meaningful premium
EARNINGS_AVOIDANCE_DAYS = 14      # skip stock if earnings within this window
ASSIGNMENT_CALL_BUFFER = 0.05     # sell covered call 5% above cost basis
STOP_LOSS_PCT = 0.20              # close put early if stock drops 20% from strike


# ── Alpaca client for wheel account ───────────────────────────────────────────

def _get_wheel_trading_client():
    """Return TradingClient using wheel keys (or main keys as fallback)."""
    from alpaca.trading.client import TradingClient
    key = settings.alpaca_wheel_key or settings.alpaca_api_key
    secret = settings.alpaca_wheel_secret or settings.alpaca_secret_key
    return TradingClient(key, secret, paper=True)


def _get_wheel_data_client():
    """Return StockHistoricalDataClient for market data."""
    from alpaca.data.historical import StockHistoricalDataClient
    key = settings.alpaca_wheel_key or settings.alpaca_api_key
    secret = settings.alpaca_wheel_secret or settings.alpaca_secret_key
    return StockHistoricalDataClient(key, secret)


def _get_wheel_options_client():
    """Return OptionHistoricalDataClient for options data."""
    from alpaca.data.historical.option import OptionHistoricalDataClient
    key = settings.alpaca_wheel_key or settings.alpaca_api_key
    secret = settings.alpaca_wheel_secret or settings.alpaca_secret_key
    return OptionHistoricalDataClient(key, secret)


# ── Market regime (from Kova's existing cache) ────────────────────────────────

def _get_current_regime() -> str:
    """
    Read Kova's cached market regime.
    Returns 'bullish', 'bearish', or 'neutral'.
    Defaults to 'neutral' if not available.
    """
    try:
        from services.db import cache_get
        regime_data = cache_get("market_regime")
        if regime_data and isinstance(regime_data, dict):
            return regime_data.get("regime", "neutral").lower()
        if regime_data and isinstance(regime_data, str):
            return regime_data.lower()
    except Exception:
        pass
    return "neutral"


def _regime_adjusted_delta(regime: str) -> float:
    """Adjust put delta based on market regime."""
    if regime == "bearish":
        return 0.15   # Further OTM — safer in downtrend
    elif regime == "bullish":
        return 0.30   # Closer to ATM — more premium, safer in uptrend
    return TARGET_DELTA   # neutral = standard 0.25


# ── DB helpers ────────────────────────────────────────────────────────────────

def _open_wheel_position(symbol: str, phase: str, put_contract: str,
                          put_strike: float, put_expiry, put_premium: float,
                          put_order_id: str, regime: str) -> Optional[int]:
    """Insert a new wheel position record. Returns row id."""
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            logger.warning("Wheel DB: no connection, skipping log")
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
    """Update fields on a wheel_positions row."""
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
    """Return all active wheel positions from DB."""
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
    """P&L summary for iOS Wheel tab."""
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


# ── Core scanner ──────────────────────────────────────────────────────────────

def scan_opportunities() -> list[dict]:
    """
    Scan watchlist for put-selling opportunities.
    Returns ranked list of opportunities with strike, premium, yield.
    Does NOT place orders — call execute_best_opportunity() for that.
    """
    regime = _get_current_regime()
    target_delta = _regime_adjusted_delta(regime)
    active = get_active_wheel_positions()
    active_symbols = {p["symbol"] for p in active}

    # Don't scan if at max positions
    if len(active) >= MAX_ACTIVE_POSITIONS:
        logger.info(f"Wheel: at max positions ({MAX_ACTIVE_POSITIONS}), skipping scan")
        return []

    opportunities = []
    today = date.today()
    expiry_min = today + timedelta(days=MIN_DTE)
    expiry_max = today + timedelta(days=MAX_DTE)

    trading_client = _get_wheel_trading_client()
    data_client = _get_wheel_data_client()

    for symbol in WATCHLIST:
        # Skip symbols already in active positions
        if symbol in active_symbols:
            continue

        try:
            # Get current stock price
            from alpaca.data.requests import StockLatestQuoteRequest
            quote_req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            quote_data = data_client.get_stock_latest_quote(quote_req)
            quote = quote_data.get(symbol)
            if not quote:
                continue
            stock_price = float((quote.ask_price + quote.bid_price) / 2)
            if stock_price <= 0:
                continue

            # Get put contracts in DTE window
            from alpaca.trading.requests import GetOptionContractsRequest
            from alpaca.trading.enums import ContractType
            contracts_req = GetOptionContractsRequest(
                underlying_symbols=[symbol],
                type=ContractType.PUT,
                expiration_date_gte=str(expiry_min),
                expiration_date_lte=str(expiry_max),
            )
            contracts = trading_client.get_option_contracts(contracts_req)
            if not contracts or not contracts.option_contracts:
                logger.debug(f"Wheel scan: no contracts found for {symbol}")
                continue

            # Find contracts near target delta (use strike distance as proxy)
            # Target strike ≈ stock_price * (1 - delta_buffer)
            # delta 0.25 ≈ strike 90-92% of current price
            target_strike = stock_price * (1 - target_delta * 0.8)

            best_contract = None
            best_yield = 0.0

            for contract in contracts.option_contracts:
                try:
                    strike = float(contract.strike_price)
                    # Only look at strikes within 5-20% below current price
                    strike_pct = strike / stock_price
                    if not (0.80 <= strike_pct <= 0.97):
                        continue

                    # Get option snapshot for premium
                    from alpaca.data.historical.option import OptionHistoricalDataClient
                    from alpaca.data.requests import OptionSnapshotRequest
                    opts_client = _get_wheel_options_client()
                    snap_req = OptionSnapshotRequest(symbol_or_symbols=contract.symbol)
                    snaps = opts_client.get_option_snapshot(snap_req)
                    snap = snaps.get(contract.symbol)
                    if not snap or not snap.latest_quote:
                        continue

                    ask = float(snap.latest_quote.ask_price or 0)
                    bid = float(snap.latest_quote.bid_price or 0)
                    if ask <= 0 or bid <= 0:
                        continue

                    # Use mid-price as achievable premium
                    premium = (ask + bid) / 2
                    premium_yield = premium / strike

                    if premium_yield < MIN_PREMIUM_YIELD:
                        continue

                    if premium_yield > best_yield:
                        best_yield = premium_yield
                        best_contract = {
                            "symbol": symbol,
                            "stock_price": round(stock_price, 2),
                            "contract": contract.symbol,
                            "strike": strike,
                            "expiry": str(contract.expiration_date),
                            "dte": (contract.expiration_date - today).days,
                            "premium": round(premium, 2),
                            "premium_yield": round(premium_yield * 100, 2),
                            "annual_yield": round(premium_yield * (365 / max((contract.expiration_date - today).days, 1)) * 100, 1),
                            "regime": regime,
                            "collateral_needed": round(strike * 100, 2),
                        }
                except Exception as inner_e:
                    logger.debug(f"Wheel scan contract error {symbol}: {inner_e}")
                    continue

            if best_contract:
                opportunities.append(best_contract)
                logger.info(
                    f"Wheel opportunity: {symbol} ${best_contract['strike']} put "
                    f"exp {best_contract['expiry']} | premium ${best_contract['premium']} "
                    f"({best_contract['premium_yield']}% yield, {best_contract['annual_yield']}% annual)"
                )

        except Exception as e:
            logger.error(f"Wheel scan error for {symbol}: {e}")
            continue

    # Rank by annual yield
    opportunities.sort(key=lambda x: x["annual_yield"], reverse=True)
    return opportunities


# ── Order execution ───────────────────────────────────────────────────────────

def execute_put(opportunity: dict) -> Optional[dict]:
    """
    Place a cash-secured put order for the given opportunity.
    Returns order details or None on failure.
    """
    try:
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderType

        trading_client = _get_wheel_trading_client()
        regime = _get_current_regime()

        # Place limit order at mid-price (slightly aggressive to fill)
        order_req = LimitOrderRequest(
            symbol=opportunity["contract"],
            qty=1,                        # 1 contract = 100 shares
            side=OrderSide.SELL,
            type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=round(opportunity["premium"] * 0.98, 2),  # 2% below mid to fill
        )
        order = trading_client.submit_order(order_req)
        logger.info(
            f"Wheel PUT placed: {opportunity['symbol']} ${opportunity['strike']} "
            f"exp {opportunity['expiry']} | order {order.id}"
        )

        # Log to wheel_positions
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

        return {
            "order_id": str(order.id),
            "symbol": opportunity["symbol"],
            "contract": opportunity["contract"],
            "strike": opportunity["strike"],
            "expiry": opportunity["expiry"],
            "premium": opportunity["premium"],
            "position_db_id": pos_id,
        }

    except Exception as e:
        logger.error(f"Wheel execute_put error: {e}")
        return None


def execute_covered_call(position: dict) -> Optional[dict]:
    """
    Sell a covered call above cost basis for an assigned position.
    Called after a put gets assigned (we now own 100 shares).
    """
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetOptionContractsRequest, LimitOrderRequest
        from alpaca.trading.enums import ContractType, OrderSide, TimeInForce, OrderType

        symbol = position["symbol"]
        cost_basis = position["cost_basis"]
        if not cost_basis:
            logger.error(f"Wheel: no cost_basis for {symbol}, can't sell call")
            return None

        # Target call strike 5% above cost basis
        target_call_strike = cost_basis * (1 + ASSIGNMENT_CALL_BUFFER)
        today = date.today()
        expiry_min = today + timedelta(days=MIN_DTE)
        expiry_max = today + timedelta(days=MAX_DTE)

        trading_client = _get_wheel_trading_client()
        contracts_req = GetOptionContractsRequest(
            underlying_symbols=[symbol],
            type=ContractType.CALL,
            expiration_date_gte=str(expiry_min),
            expiration_date_lte=str(expiry_max),
        )
        contracts = trading_client.get_option_contracts(contracts_req)
        if not contracts or not contracts.option_contracts:
            logger.warning(f"Wheel: no call contracts found for {symbol}")
            return None

        # Find call closest to target strike (at or above)
        best_contract = None
        best_premium = 0.0
        opts_client = _get_wheel_options_client()

        for contract in contracts.option_contracts:
            strike = float(contract.strike_price)
            if strike < target_call_strike:
                continue  # Must be above cost basis

            try:
                from alpaca.data.requests import OptionSnapshotRequest
                snap_req = OptionSnapshotRequest(symbol_or_symbols=contract.symbol)
                snaps = opts_client.get_option_snapshot(snap_req)
                snap = snaps.get(contract.symbol)
                if not snap or not snap.latest_quote:
                    continue
                ask = float(snap.latest_quote.ask_price or 0)
                bid = float(snap.latest_quote.bid_price or 0)
                if ask <= 0 or bid <= 0:
                    continue
                premium = (ask + bid) / 2
                if premium > best_premium:
                    best_premium = premium
                    best_contract = {"contract_symbol": contract.symbol, "strike": strike,
                                     "expiry": str(contract.expiration_date), "premium": premium}
            except Exception:
                continue

        if not best_contract:
            logger.warning(f"Wheel: no suitable call found for {symbol}")
            return None

        # Place the covered call
        order_req = LimitOrderRequest(
            symbol=best_contract["contract_symbol"],
            qty=1,
            side=OrderSide.SELL,
            type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            limit_price=round(best_contract["premium"] * 0.98, 2),
        )
        order = trading_client.submit_order(order_req)
        logger.info(
            f"Wheel CALL placed: {symbol} ${best_contract['strike']} "
            f"exp {best_contract['expiry']} | order {order.id}"
        )

        # Update DB
        _update_wheel_position(
            position["id"],
            phase="call_open",
            call_contract=best_contract["contract_symbol"],
            call_strike=best_contract["strike"],
            call_expiry=best_contract["expiry"],
            call_premium=best_contract["premium"],
            call_order_id=str(order.id),
            total_premium_collected=float(position.get("total_premium_collected") or 0) + best_contract["premium"] * 100,
        )

        return {"order_id": str(order.id), **best_contract}

    except Exception as e:
        logger.error(f"Wheel execute_covered_call error: {e}")
        return None


# ── Assignment detection ───────────────────────────────────────────────────────

def check_assignments():
    """
    Check all open put positions for assignment.
    If assigned → we now own 100 shares → sell covered call.
    Runs daily.
    """
    active = get_active_wheel_positions()
    put_positions = [p for p in active if p["phase"] == "put_open"]

    if not put_positions:
        return

    trading_client = _get_wheel_trading_client()

    try:
        # Get all current stock positions in wheel account
        alpaca_positions = trading_client.get_all_positions()
        held_symbols = {p.symbol: float(p.avg_entry_price) for p in alpaca_positions
                        if int(float(p.qty)) >= 100}
    except Exception as e:
        logger.error(f"Wheel check_assignments: failed to get positions: {e}")
        return

    for wheel_pos in put_positions:
        symbol = wheel_pos["symbol"]
        if symbol in held_symbols:
            # Assigned! We own 100 shares now.
            cost_basis = held_symbols[symbol]
            logger.info(f"Wheel ASSIGNED: {symbol} @ ${cost_basis:.2f}")

            # Update DB
            _update_wheel_position(
                wheel_pos["id"],
                phase="assigned",
                cost_basis=cost_basis,
                total_premium_collected=float(wheel_pos.get("total_premium_collected") or 0)
                    + float(wheel_pos.get("put_premium") or 0) * 100,
            )
            # Reload updated position
            wheel_pos["cost_basis"] = cost_basis

            # Immediately sell covered call
            execute_covered_call(wheel_pos)


def check_expirations():
    """
    Check if any puts or calls expired worthless (profitable outcome).
    Mark completed, log realized P&L.
    """
    today = date.today()
    active = get_active_wheel_positions()

    for pos in active:
        try:
            # Check if put expired worthless
            if pos["phase"] == "put_open" and pos.get("put_expiry"):
                expiry = pos["put_expiry"] if isinstance(pos["put_expiry"], date) else \
                         date.fromisoformat(str(pos["put_expiry"]))
                if expiry < today:
                    premium_collected = float(pos.get("put_premium") or 0) * 100
                    logger.info(f"Wheel PUT EXPIRED WORTHLESS: {pos['symbol']} | profit ${premium_collected:.2f}")
                    _update_wheel_position(
                        pos["id"],
                        phase="completed",
                        status="completed",
                        total_premium_collected=premium_collected,
                        realized_pl=premium_collected,
                        closed_at=datetime.now(timezone.utc).isoformat(),
                        notes="Put expired worthless — full premium kept",
                    )

            # Check if covered call expired or got called away
            elif pos["phase"] == "call_open" and pos.get("call_expiry"):
                expiry = pos["call_expiry"] if isinstance(pos["call_expiry"], date) else \
                         date.fromisoformat(str(pos["call_expiry"]))
                if expiry < today:
                    total_premium = float(pos.get("total_premium_collected") or 0)
                    cost_basis = float(pos.get("cost_basis") or 0)
                    call_strike = float(pos.get("call_strike") or 0)
                    # Stock called away at call strike: profit = (call_strike - cost_basis)*100 + premiums
                    stock_pl = (call_strike - cost_basis) * 100 if call_strike > 0 else 0
                    total_pl = stock_pl + total_premium
                    logger.info(
                        f"Wheel CALL EXPIRED: {pos['symbol']} | "
                        f"stock P&L ${stock_pl:.2f} + premium ${total_premium:.2f} = ${total_pl:.2f}"
                    )
                    _update_wheel_position(
                        pos["id"],
                        phase="completed",
                        status="completed",
                        realized_pl=total_pl,
                        closed_at=datetime.now(timezone.utc).isoformat(),
                        notes=f"Completed wheel cycle. Stock P&L: ${stock_pl:.2f}, Premiums: ${total_premium:.2f}",
                    )
        except Exception as e:
            logger.error(f"Wheel check_expirations error for pos {pos.get('id')}: {e}")


# ── Scheduler ─────────────────────────────────────────────────────────────────

_wheel_running = False


def run_wheel_cycle():
    """
    Full wheel cycle:
      1. Check expirations (daily)
      2. Check assignments (daily)
      3. Scan + execute new puts (if slots available)

    Called from the scheduler on:
      - Monday 9:45 AM ET (full scan + trade)
      - Daily 9:45 AM ET (assignments + expirations only)
      - Friday 4:30 PM ET (end of week cleanup)
    """
    now_et = datetime.now(timezone.utc)
    weekday = now_et.weekday()  # 0=Monday
    logger.info(f"Wheel cycle starting (weekday={weekday})")

    # Step 1: Check expirations — always
    try:
        check_expirations()
    except Exception as e:
        logger.error(f"Wheel check_expirations failed: {e}")

    # Step 2: Check assignments — always
    try:
        check_assignments()
    except Exception as e:
        logger.error(f"Wheel check_assignments failed: {e}")

    # Step 3: Scan + place new puts — Monday and Wednesday only
    if weekday in (0, 2):  # Monday, Wednesday
        try:
            opportunities = scan_opportunities()
            if opportunities:
                # Execute top 1-2 opportunities
                slots_available = MAX_ACTIVE_POSITIONS - len(get_active_wheel_positions())
                to_execute = opportunities[:min(2, slots_available)]
                for opp in to_execute:
                    result = execute_put(opp)
                    if result:
                        logger.info(f"Wheel: placed put for {opp['symbol']}")
            else:
                logger.info("Wheel scan: no qualifying opportunities found")
        except Exception as e:
            logger.error(f"Wheel scan+execute failed: {e}")

    logger.info("Wheel cycle complete")


def get_wheel_status() -> dict:
    """
    Full status for iOS Wheel tab.
    Returns active positions + summary P&L + last scan results.
    """
    regime = _get_current_regime()
    active = get_active_wheel_positions()
    summary = get_wheel_summary()

    # Serialize dates for JSON
    for pos in active:
        for k, v in pos.items():
            if isinstance(v, (date, datetime)):
                pos[k] = v.isoformat()

    return {
        "regime": regime,
        "active_positions": active,
        "active_count": len(active),
        "max_positions": MAX_ACTIVE_POSITIONS,
        "summary": summary,
        "watchlist": WATCHLIST,
        "config": {
            "min_premium_yield_pct": MIN_PREMIUM_YIELD * 100,
            "min_dte": MIN_DTE,
            "max_dte": MAX_DTE,
            "target_delta": TARGET_DELTA,
            "max_positions": MAX_ACTIVE_POSITIONS,
        },
    }
