"""
Phase 3 — Options Engine.

Determines instrument type (stock vs options) based on holding period and regime,
selects contracts, and builds multi-leg spreads.

Strategy routing:
  Intraday signal (hold < 1 day)     → stock
  Swing signal (hold 2-5 days)       → call/put (30-delta, 21-45 DTE)
  High conviction + bull regime      → bull call spread
  Chop regime + neutral              → iron condor
  Bearish signal                     → put (replaces stock short)

Contract selection rules:
  Delta:   0.30–0.40
  DTE:     21–45 days
  Spread:  bid-ask < 10% of mid
  Min DTE: 7 (never buy options with < 7 DTE)
"""
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ── Alpaca option imports (SDK v0.21+) ────────────────────────────────────────
try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import (
        GetOptionContractsRequest,
        OptionLegRequest,
        OptionOrderRequest,
    )
    from alpaca.trading.enums import (
        AssetClass,
        ContractType,
        ExerciseStyle,
        OrderSide,
        OrderType,
        PositionIntent,
        TimeInForce,
    )

    _ALPACA_OPTIONS_AVAILABLE = True
except ImportError:
    _ALPACA_OPTIONS_AVAILABLE = False
    logger.warning("Alpaca options SDK not available — options_engine will run in dry-run mode")

from config import settings


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class OptionContract:
    symbol: str          # OCC symbol e.g. AAPL240119C00150000
    underlying: str
    expiry: date
    strike: float
    contract_type: str   # "call" | "put"
    delta: Optional[float]
    bid: float
    ask: float
    mid: float
    open_interest: int
    dte: int


@dataclass
class OptionOrder:
    strategy: str        # "long_call" | "long_put" | "bull_call_spread" | "iron_condor"
    legs: list[dict]     # [{symbol, side, qty, order_type, limit_price}]
    max_risk: float      # maximum dollar loss possible
    rationale: str


# ── Alpaca client (lazy init) ─────────────────────────────────────────────────

_trading_client: Optional["TradingClient"] = None


def _get_client() -> "TradingClient":
    global _trading_client
    if _trading_client is None:
        if not _ALPACA_OPTIONS_AVAILABLE:
            raise RuntimeError("Alpaca options SDK not installed")
        _trading_client = TradingClient(
            settings.alpaca_api_key,
            settings.alpaca_secret_key,
            paper=getattr(settings, "alpaca_paper", True),
        )
    return _trading_client


# ── OCC symbol builder ────────────────────────────────────────────────────────

def build_occ_symbol(underlying: str, expiry: date, contract_type: str, strike: float) -> str:
    """Build OCC option symbol: AAPL240119C00150000"""
    ticker = underlying.upper().ljust(6)
    date_str = expiry.strftime("%y%m%d")
    cp = "C" if contract_type == "call" else "P"
    strike_int = int(round(strike * 1000))
    return f"{ticker}{date_str}{cp}{strike_int:08d}"


# ── Expiry selection ──────────────────────────────────────────────────────────

def _target_expiry_range(min_dte: int = 21, max_dte: int = 45) -> tuple[date, date]:
    today = date.today()
    return today + timedelta(days=min_dte), today + timedelta(days=max_dte)


def _fridays_in_range(start: date, end: date) -> list[date]:
    """Return all Fridays (standard option expiries) between start and end."""
    fridays = []
    d = start
    while d <= end:
        if d.weekday() == 4:  # Friday
            fridays.append(d)
        d += timedelta(days=1)
    return fridays


# ── Chain fetch ───────────────────────────────────────────────────────────────

def get_option_chain(
    symbol: str,
    expiry_range: tuple[int, int] = (21, 45),
    contract_type: Optional[str] = None,
) -> list[OptionContract]:
    """
    Fetch option chain from Alpaca for the given symbol.
    Returns contracts filtered to the DTE window, sorted by delta proximity to 0.35.
    """
    if not _ALPACA_OPTIONS_AVAILABLE:
        logger.warning("options_engine: Alpaca SDK unavailable, returning empty chain")
        return []

    min_dte, max_dte = expiry_range
    expiry_start, expiry_end = _target_expiry_range(min_dte, max_dte)

    try:
        client = _get_client()
        req = GetOptionContractsRequest(
            underlying_symbols=[symbol],
            expiration_date_gte=expiry_start,
            expiration_date_lte=expiry_end,
            type=ContractType.CALL if contract_type == "call" else (
                ContractType.PUT if contract_type == "put" else None
            ),
            style=ExerciseStyle.AMERICAN,
        )
        raw = client.get_option_contracts(req)
        contracts = raw.option_contracts if hasattr(raw, "option_contracts") else []

        today = date.today()
        result: list[OptionContract] = []
        for c in contracts:
            expiry = c.expiration_date if isinstance(c.expiration_date, date) else \
                datetime.strptime(str(c.expiration_date), "%Y-%m-%d").date()
            dte = (expiry - today).days

            if dte < 7:
                continue  # never trade < 7 DTE

            bid = float(c.bid_price or 0)
            ask = float(c.ask_price or 0)
            mid = (bid + ask) / 2 if bid and ask else 0

            # Liquidity filter: bid-ask spread < 10% of mid
            if mid > 0 and (ask - bid) / mid > 0.10:
                continue

            delta = float(c.delta) if c.delta is not None else None
            result.append(OptionContract(
                symbol=c.symbol,
                underlying=symbol,
                expiry=expiry,
                strike=float(c.strike_price),
                contract_type="call" if c.type == ContractType.CALL else "put",
                delta=delta,
                bid=bid,
                ask=ask,
                mid=mid,
                open_interest=int(c.open_interest or 0),
                dte=dte,
            ))

        return sorted(result, key=lambda x: abs((x.delta or 0) - 0.35))

    except Exception as e:
        logger.error("get_option_chain(%s): %s", symbol, e)
        return []


# ── Contract selection ────────────────────────────────────────────────────────

def select_contract(
    chain: list[OptionContract],
    action: str,          # "call" | "put"
    target_delta: float = 0.35,
) -> Optional[OptionContract]:
    """
    Pick the best single contract from a chain.
    Filters to the right type, finds closest delta to target, checks OI > 100.
    """
    candidates = [
        c for c in chain
        if c.contract_type == action
        and c.delta is not None
        and 0.20 <= abs(c.delta) <= 0.50
        and c.open_interest >= 100
        and c.mid > 0
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda x: abs(abs(x.delta or 0) - target_delta))


# ── Strategy builders ─────────────────────────────────────────────────────────

def build_bull_call_spread(
    symbol: str,
    expiry_range: tuple[int, int] = (21, 45),
) -> Optional[tuple[OptionContract, OptionContract]]:
    """
    Bull call spread: buy ATM call, sell OTM call (~0.20 delta).
    Defined risk, capped upside, better R/R than naked call in bull regime.
    Returns (long_leg, short_leg) or None if chain is thin.
    """
    chain = get_option_chain(symbol, expiry_range, contract_type="call")
    long_leg = select_contract(chain, "call", target_delta=0.35)
    if not long_leg:
        return None

    # Short leg: same expiry, higher strike (~0.20 delta)
    same_expiry = [c for c in chain if c.expiry == long_leg.expiry and c.strike > long_leg.strike]
    if not same_expiry:
        return None
    short_leg = min(same_expiry, key=lambda x: abs((x.delta or 0) - 0.20))
    return long_leg, short_leg


def build_iron_condor(
    symbol: str,
    expiry_range: tuple[int, int] = (21, 45),
) -> Optional[tuple[OptionContract, OptionContract, OptionContract, OptionContract]]:
    """
    Iron condor for chop regime: collect premium while price stays range-bound.
    Legs: sell put spread (below) + sell call spread (above).
    Returns (long_put, short_put, short_call, long_call) or None.
    """
    calls = get_option_chain(symbol, expiry_range, contract_type="call")
    puts = get_option_chain(symbol, expiry_range, contract_type="put")

    short_call = select_contract(calls, "call", target_delta=0.20)
    short_put = select_contract(puts, "put", target_delta=0.20)

    if not short_call or not short_put:
        return None

    # Wings: farther OTM (~0.10 delta), same expiry
    call_wings = [c for c in calls if c.expiry == short_call.expiry and c.strike > short_call.strike]
    put_wings = [c for c in puts if c.expiry == short_put.expiry and c.strike < short_put.strike]

    if not call_wings or not put_wings:
        return None

    long_call = min(call_wings, key=lambda x: abs((x.delta or 0) - 0.10))
    long_put = min(put_wings, key=lambda x: abs((x.delta or 0) - 0.10))

    return long_put, short_put, short_call, long_call


# ── Instrument routing ────────────────────────────────────────────────────────

def route_instrument(
    signal_type: str,
    holding_period: str,   # "intraday" | "swing"
    regime: str,           # "bull" | "bear" | "chop"
    conviction: int,       # 0-100
    suggested_action: str, # "buy" | "short"
) -> str:
    """
    Decide which instrument to use based on signal characteristics.
    Returns: "stock" | "long_call" | "long_put" | "bull_call_spread" | "iron_condor"
    """
    if holding_period == "intraday":
        return "stock"

    # Single-leg only: the exit manager (manage_option_positions) closes legs
    # independently, so multi-leg spreads could orphan a naked short leg.
    # Single-leg also works at options approval level 2.
    # Direction follows the SIGNAL, never the regime — a buy signal in a bear
    # regime is still a bullish bet (oversold bounce), so it gets a call.
    if suggested_action == "short":
        return "long_put"
    return "long_call"


# ── Order builders ────────────────────────────────────────────────────────────

def build_option_order(
    symbol: str,
    instrument: str,
    qty: int = 1,
    expiry_range: tuple[int, int] = (21, 45),
) -> Optional[OptionOrder]:
    """
    Build an OptionOrder ready to submit, based on instrument type.
    Returns None if chain is too thin to trade.
    """
    if instrument == "long_call":
        chain = get_option_chain(symbol, expiry_range, "call")
        contract = select_contract(chain, "call")
        if not contract:
            return None
        return OptionOrder(
            strategy="long_call",
            legs=[{
                "symbol": contract.symbol,
                "side": "buy",
                "qty": qty,
                "order_type": "limit",
                "limit_price": round(contract.ask, 2),
            }],
            max_risk=round(contract.ask * qty * 100, 2),
            rationale=(
                f"Buy {qty}x {contract.symbol} @ ${contract.ask:.2f} | "
                f"delta={contract.delta:.2f} DTE={contract.dte}"
            ),
        )

    if instrument == "long_put":
        chain = get_option_chain(symbol, expiry_range, "put")
        contract = select_contract(chain, "put")
        if not contract:
            return None
        return OptionOrder(
            strategy="long_put",
            legs=[{
                "symbol": contract.symbol,
                "side": "buy",
                "qty": qty,
                "order_type": "limit",
                "limit_price": round(contract.ask, 2),
            }],
            max_risk=round(contract.ask * qty * 100, 2),
            rationale=(
                f"Buy {qty}x {contract.symbol} @ ${contract.ask:.2f} | "
                f"delta={contract.delta:.2f} DTE={contract.dte}"
            ),
        )

    if instrument == "bull_call_spread":
        legs = build_bull_call_spread(symbol, expiry_range)
        if not legs:
            return None
        long_leg, short_leg = legs
        debit = round((long_leg.ask - short_leg.bid) * qty * 100, 2)
        max_profit = round((short_leg.strike - long_leg.strike - long_leg.ask + short_leg.bid) * qty * 100, 2)
        return OptionOrder(
            strategy="bull_call_spread",
            legs=[
                {"symbol": long_leg.symbol, "side": "buy", "qty": qty, "order_type": "limit", "limit_price": round(long_leg.ask, 2)},
                {"symbol": short_leg.symbol, "side": "sell", "qty": qty, "order_type": "limit", "limit_price": round(short_leg.bid, 2)},
            ],
            max_risk=debit,
            rationale=(
                f"Bull call spread: buy {long_leg.symbol} / sell {short_leg.symbol} | "
                f"debit=${debit:.0f} max_profit=${max_profit:.0f} DTE={long_leg.dte}"
            ),
        )

    if instrument == "iron_condor":
        result = build_iron_condor(symbol, expiry_range)
        if not result:
            return None
        long_put, short_put, short_call, long_call = result
        credit = round((short_call.bid + short_put.bid - long_call.ask - long_put.ask) * qty * 100, 2)
        wing_width = min(long_call.strike - short_call.strike, short_put.strike - long_put.strike)
        max_risk = round((wing_width * qty * 100) - max(credit, 0), 2)
        return OptionOrder(
            strategy="iron_condor",
            legs=[
                {"symbol": long_put.symbol, "side": "buy", "qty": qty, "order_type": "limit", "limit_price": round(long_put.ask, 2)},
                {"symbol": short_put.symbol, "side": "sell", "qty": qty, "order_type": "limit", "limit_price": round(short_put.bid, 2)},
                {"symbol": short_call.symbol, "side": "sell", "qty": qty, "order_type": "limit", "limit_price": round(short_call.bid, 2)},
                {"symbol": long_call.symbol, "side": "buy", "qty": qty, "order_type": "limit", "limit_price": round(long_call.ask, 2)},
            ],
            max_risk=max_risk,
            rationale=(
                f"Iron condor on {symbol}: credit=${credit:.0f} max_risk=${max_risk:.0f} DTE={short_call.dte}"
            ),
        )

    return None


# ── Order execution ───────────────────────────────────────────────────────────

def place_option_order(order: OptionOrder) -> dict:
    """
    Submit a (possibly multi-leg) option order to Alpaca.
    Single-leg → OptionOrderRequest; multi-leg → multi-leg OptionOrderRequest.
    Returns the Alpaca order response dict, or a dry-run dict if SDK unavailable.
    """
    if not _ALPACA_OPTIONS_AVAILABLE:
        logger.info("DRY RUN — would place: %s", order.rationale)
        return {"status": "dry_run", "strategy": order.strategy, "legs": order.legs}

    try:
        client = _get_client()

        if len(order.legs) == 1:
            leg = order.legs[0]
            req = OptionOrderRequest(
                symbol=leg["symbol"],
                qty=leg["qty"],
                side=OrderSide.BUY if leg["side"] == "buy" else OrderSide.SELL,
                type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                limit_price=leg["limit_price"],
            )
            resp = client.submit_order(req)

        else:
            legs = [
                OptionLegRequest(
                    symbol=leg["symbol"],
                    qty=leg["qty"],
                    side=OrderSide.BUY if leg["side"] == "buy" else OrderSide.SELL,
                    position_intent=(
                        PositionIntent.BTO if leg["side"] == "buy" else PositionIntent.STO
                    ),
                )
                for leg in order.legs
            ]
            # Net debit/credit limit: sum of leg prices (positive = debit, negative = credit)
            net_limit = sum(
                (leg["limit_price"] if l["side"] == "buy" else -leg["limit_price"])
                for leg, l in zip(order.legs, order.legs)
            )
            req = OptionOrderRequest(
                legs=legs,
                qty=order.legs[0]["qty"],
                type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                limit_price=round(abs(net_limit), 2),
            )
            resp = client.submit_order(req)

        return {"status": "submitted", "id": str(resp.id), "strategy": order.strategy}

    except Exception as e:
        logger.error("place_option_order(%s): %s", order.strategy, e)
        return {"status": "error", "error": str(e), "strategy": order.strategy}


# ── Top-level entry point ─────────────────────────────────────────────────────

def decide_and_place(
    symbol: str,
    signal_type: str,
    holding_period: str,
    regime: str,
    conviction: int,
    suggested_action: str,
    qty: int = 1,
) -> dict:
    """
    Full pipeline: route → build → place.
    Called by engine.py instead of placing a stock order when options are appropriate.
    """
    instrument = route_instrument(signal_type, holding_period, regime, conviction, suggested_action)

    if instrument == "stock":
        return {"instrument": "stock", "status": "delegate_to_alpaca_service"}

    logger.info(
        "Options route: %s → %s (regime=%s conviction=%d)",
        symbol, instrument, regime, conviction,
    )

    order = build_option_order(symbol, instrument, qty=qty)
    if not order:
        logger.warning("options_engine: no liquid contract found for %s %s — falling back to stock", symbol, instrument)
        return {"instrument": "stock", "status": "fallback_no_chain"}

    result = place_option_order(order)
    result["instrument"] = instrument
    result["rationale"] = order.rationale
    result["max_risk"] = order.max_risk
    return result


# ── Position management (exits) ───────────────────────────────────────────────
# Long options can't reuse the stock exit rules: a 4% stock stop fires on
# normal option noise (±30%/day is routine), and an unmanaged long option
# bleeds theta to zero. Rules here are premium-based instead.

import re as _re

_OCC_RE = _re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")

PREMIUM_STOP_PCT = float(os.environ.get("KOVA_OPT_STOP_PCT", "-50"))    # close at -50% premium
PREMIUM_TP_PCT   = float(os.environ.get("KOVA_OPT_TP_PCT", "100"))     # close at +100% premium
TIME_STOP_DTE    = int(os.environ.get("KOVA_OPT_TIME_STOP_DTE", "10")) # close at ≤10 DTE


def is_option_symbol(symbol: str) -> bool:
    return bool(_OCC_RE.match(symbol or ""))


def _occ_expiry(symbol: str) -> Optional[date]:
    m = _OCC_RE.match(symbol or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(2), "%y%m%d").date()
    except ValueError:
        return None


def manage_option_positions() -> list[dict]:
    """
    Review every open option position and close any that hit an exit rule:
      premium_stop  — unrealized P/L ≤ -50% of premium paid
      take_profit   — unrealized P/L ≥ +100%
      time_stop     — ≤ 10 DTE (theta burn accelerates; thesis had its chance)
    Returns a list of action dicts for logging.
    """
    if not _ALPACA_OPTIONS_AVAILABLE:
        return []
    actions: list[dict] = []
    try:
        client = _get_client()
        for p in client.get_all_positions():
            sym = p.symbol
            if not is_option_symbol(sym):
                continue
            pl_pct = float(p.unrealized_plpc or 0) * 100
            expiry = _occ_expiry(sym)
            dte = (expiry - date.today()).days if expiry else None

            reason = None
            if pl_pct <= PREMIUM_STOP_PCT:
                reason = f"premium_stop ({pl_pct:.0f}%)"
            elif pl_pct >= PREMIUM_TP_PCT:
                reason = f"take_profit ({pl_pct:+.0f}%)"
            elif dte is not None and dte <= TIME_STOP_DTE:
                reason = f"time_stop ({dte} DTE)"

            if not reason:
                continue
            try:
                client.close_position(sym)
                logger.info("Options exit: %s — %s (P/L %+.0f%%)", sym, reason, pl_pct)
                actions.append({"symbol": sym, "action": "close",
                                "reason": reason, "pl_pct": round(pl_pct, 1)})
            except Exception as ce:
                logger.warning("Options exit failed for %s: %s", sym, ce)
                actions.append({"symbol": sym, "action": "close_failed",
                                "reason": reason, "error": str(ce)})
    except Exception as e:
        logger.warning("manage_option_positions failed (non-fatal): %s", e)
    return actions
