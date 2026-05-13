"""
finance.py — Estimated tax summary and profit reserve endpoints.

Tax summary: reads realized P&L from position_log, calculates estimated
US short-term capital gains tax at the user's chosen bracket. No AI calls.

Profit reserve: returns current reserved cash balance and lets the user
reset it (e.g. after withdrawing the reserved amount).
"""

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone
from services.trading_engine import _RESERVE_CACHE_KEY

router = APIRouter(prefix="/api/finance", tags=["finance"])

# ── US 2024 short-term capital gains brackets (same as ordinary income) ──
TAX_BRACKETS = [
    {"label": "10%",  "rate": 0.10, "description": "Income up to $11,600"},
    {"label": "12%",  "rate": 0.12, "description": "Income $11,601 – $47,150"},
    {"label": "22%",  "rate": 0.22, "description": "Income $47,151 – $100,525"},
    {"label": "24%",  "rate": 0.24, "description": "Income $100,526 – $191,950"},
    {"label": "32%",  "rate": 0.32, "description": "Income $191,951 – $243,725"},
    {"label": "35%",  "rate": 0.35, "description": "Income $243,726 – $609,350"},
    {"label": "37%",  "rate": 0.37, "description": "Income over $609,350"},
]


def _get_ytd_pl() -> dict:
    """Query position_log for YTD realized gains and losses."""
    from services.db import _get_conn
    conn = _get_conn()
    year_start = datetime(datetime.now(timezone.utc).year, 1, 1, tzinfo=timezone.utc)

    result = {
        "total_gains": 0.0,
        "total_losses": 0.0,
        "net_pl": 0.0,
        "trade_count": 0,
        "winning_trades": 0,
        "losing_trades": 0,
    }

    if not conn:
        return result

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE realized_pl > 0) AS wins,
                    COUNT(*) FILTER (WHERE realized_pl < 0) AS losses,
                    COALESCE(SUM(realized_pl) FILTER (WHERE realized_pl > 0), 0) AS gains,
                    COALESCE(SUM(realized_pl) FILTER (WHERE realized_pl < 0), 0) AS loss_sum
                FROM position_log
                WHERE exit_time >= %s
                  AND exit_price IS NOT NULL
                  AND realized_pl IS NOT NULL
            """, (year_start,))
            row = cur.fetchone()
            if row:
                total, wins, losses, gains, loss_sum = row
                result["trade_count"]    = int(total or 0)
                result["winning_trades"] = int(wins or 0)
                result["losing_trades"]  = int(losses or 0)
                result["total_gains"]    = round(float(gains or 0), 2)
                result["total_losses"]   = round(float(loss_sum or 0), 2)
                result["net_pl"]         = round(result["total_gains"] + result["total_losses"], 2)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Tax summary DB query failed: {e}")

    return result


@router.get("/tax-summary")
def get_tax_summary(bracket_rate: float = 0.22):
    """
    Return YTD realized P&L and estimated tax at the requested bracket rate.
    bracket_rate: decimal (e.g. 0.22 for 22%). Defaults to 22%.
    All trading bot gains are short-term (held < 1 year).
    """
    pl = _get_ytd_pl()
    net = pl["net_pl"]

    # Tax is only owed on NET gains (losses offset gains)
    taxable_gain  = max(0.0, net)
    estimated_tax = round(taxable_gain * bracket_rate, 2)
    after_tax_gain = round(net - estimated_tax, 2)

    return {
        "year": datetime.now(timezone.utc).year,
        "ytd": {
            "total_gains":    pl["total_gains"],
            "total_losses":   pl["total_losses"],
            "net_pl":         net,
            "trade_count":    pl["trade_count"],
            "winning_trades": pl["winning_trades"],
            "losing_trades":  pl["losing_trades"],
        },
        "tax": {
            "bracket_rate":    bracket_rate,
            "taxable_gain":    taxable_gain,
            "estimated_tax":   estimated_tax,
            "after_tax_gain":  after_tax_gain,
            "gain_type":       "short_term",  # bot never holds > 1 year
            "disclaimer":      "Estimated only. Consult a tax professional for actual liability.",
        },
        "brackets": TAX_BRACKETS,
    }


@router.get("/reserve")
def get_reserve():
    """Return current profit reserve balance and settings."""
    from services.trading_engine import get_reserved_cash, _risk_settings
    reserved = get_reserved_cash()
    reserve_pct = float(_risk_settings.get("profit_reserve_pct", 0.0))
    return {
        "reserved_cash":    round(reserved, 2),
        "profit_reserve_pct": reserve_pct,
        "enabled":          reserve_pct > 0,
        "message":          f"${reserved:,.2f} set aside from profits — not available for trading."
                            if reserved > 0 else "No profits reserved yet.",
    }


class ReserveResetRequest(BaseModel):
    confirm: bool = False
    amount: Optional[float] = None   # if None, resets entire balance


@router.post("/reserve/reset")
def reset_reserve(req: ReserveResetRequest):
    """
    Reset (withdraw) from the profit reserve.
    - amount=None resets the entire balance (full withdrawal).
    - amount=X deducts X from the balance (partial withdrawal).
    Requires confirm=True to prevent accidental resets.
    """
    if not req.confirm:
        return {"error": "Pass confirm=true to reset the reserve balance."}

    from services.trading_engine import get_reserved_cash, add_to_reserve
    from services.db import cache_set
    current = get_reserved_cash()

    if req.amount is not None:
        if float(req.amount) < 0:
            return {"error": "amount must be non-negative"}
        deduct = min(float(req.amount), current)
        new_balance = round(current - deduct, 2)
    else:
        deduct = current
        new_balance = 0.0

    cache_set(_RESERVE_CACHE_KEY, new_balance, 365 * 24 * 3600)
    return {
        "withdrawn":     round(deduct, 2),
        "new_balance":   new_balance,
        "message":       f"${deduct:,.2f} withdrawn from reserve. New balance: ${new_balance:,.2f}",
    }
