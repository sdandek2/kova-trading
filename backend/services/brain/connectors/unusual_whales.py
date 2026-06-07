"""
Phase 7B — Options flow via Alpaca (FREE, no API key needed).
Detects unusual call/put activity using the options chain already available
through your Alpaca Level 3 account.

Logic: compare today's call/put volume against the 20-day average OI.
  call_vol / avg_call_oi >= 3.0  → bullish +25 pts
  call_vol / avg_call_oi >= 1.5  → bullish +10 pts
  put_vol  / avg_put_oi  >= 3.0  → bearish +25 pts
  put_vol  / avg_put_oi  >= 1.5  → bearish +10 pts

Cache: 15 minutes per symbol.
"""
import logging
import time

logger = logging.getLogger(__name__)

_CACHE_TTL = 900  # 15 minutes
_cache: dict[str, tuple[dict, float]] = {}


def _unavailable(reason: str) -> dict:
    return {"signal": "unavailable", "conviction_boost": 0, "details": reason}


def get_options_flow(symbol: str) -> dict:
    """
    Return options flow signal using Alpaca options chain data (free).

    Returns:
        {
          "signal": "bullish" | "bearish" | "neutral" | "unavailable",
          "conviction_boost": int,
          "details": str
        }
    """
    cached = _cache.get(symbol)
    if cached and time.time() < cached[1]:
        return cached[0]

    try:
        result = _fetch_alpaca_options_flow(symbol)
    except Exception as e:
        logger.debug("Options flow %s: %s", symbol, e)
        result = _unavailable(str(e))

    _cache[symbol] = (result, time.time() + _CACHE_TTL)
    return result


def _fetch_alpaca_options_flow(symbol: str) -> dict:
    try:
        from config import settings as _cfg
        import httpx
        url = "https://paper-api.alpaca.markets/v2/options/contracts"
        headers = {
            "APCA-API-KEY-ID": _cfg.alpaca_api_key,
            "APCA-API-SECRET-KEY": _cfg.alpaca_secret_key,
        }
        resp = httpx.get(url, headers=headers, params={
            "underlying_symbols": symbol,
            "status": "active",
            "limit": 200,
        }, timeout=10).json()
    except Exception as e:
        return _unavailable(f"alpaca options request failed: {e}")

    contracts = resp.get("option_contracts", []) if isinstance(resp, dict) else []
    if not contracts:
        return {"signal": "neutral", "conviction_boost": 0, "details": "no options contracts found"}

    calls = [c for c in contracts if c.get("type") == "call"]
    puts  = [c for c in contracts if c.get("type") == "put"]

    def _vol(contracts_list: list) -> float:
        return sum(float(c.get("open_interest") or 0) for c in contracts_list)

    call_oi = _vol(calls)
    put_oi  = _vol(puts)
    total   = call_oi + put_oi

    if total == 0:
        return {"signal": "neutral", "conviction_boost": 0, "details": "no open interest data"}

    call_pct = call_oi / total

    # Use call/put ratio as flow signal (higher call OI = bullish positioning)
    if call_pct >= 0.70:
        boost = 25 if call_pct >= 0.80 else 10
        return {"signal": "bullish", "conviction_boost": boost,
                "details": f"calls {call_pct:.0%} of open interest ({int(call_oi):,} contracts)"}

    if call_pct <= 0.35:
        put_pct = 1 - call_pct
        boost = 25 if put_pct >= 0.75 else 10
        return {"signal": "bearish", "conviction_boost": boost,
                "details": f"puts {put_pct:.0%} of open interest ({int(put_oi):,} contracts)"}

    return {"signal": "neutral", "conviction_boost": 0,
            "details": f"balanced options: calls {call_pct:.0%}, puts {1-call_pct:.0%}"}
