from services.db import cache_get, cache_set

# Strategies define how aggressively the bot trades
STRATEGIES = {
    "conservative": {
        "key": "conservative",
        "name": "Conservative",
        "max_position_pct": 0.05,
        "min_confidence": "high",
        "default_take_profit_pct": 0.05,   # 5% TP
        "default_stop_loss_pct": 0.03,     # 3% trailing stop
        "prompt_modifier": (
            "Be very cautious. Only trade with very strong, high-confidence signals. "
            "Prefer defensive ETFs and blue chips. Avoid penny stocks and leveraged ETFs. "
            "Prioritise capital preservation over gains. "
            "Set take_profit_pct between 0.04-0.08 and stop_loss_pct between 0.02-0.04."
        ),
    },
    "balanced": {
        "key": "balanced",
        "name": "Balanced",
        "max_position_pct": 0.10,
        "min_confidence": "medium",
        "default_take_profit_pct": 0.10,   # 10% TP
        "default_stop_loss_pct": 0.04,     # 4% trailing stop
        "prompt_modifier": (
            "Balance risk and reward. Accept medium-to-high confidence trades. "
            "Mix growth stocks with ETFs. Moderate use of leveraged ETFs is fine. "
            "Aim for steady growth while managing downside. "
            "Set take_profit_pct between 0.08-0.15 and stop_loss_pct between 0.03-0.05."
        ),
    },
    "aggressive": {
        "key": "aggressive",
        "name": "Aggressive",
        "max_position_pct": 0.25,
        "min_confidence": "medium",
        "default_take_profit_pct": 0.15,   # 15% TP default — never cut winners early
        "default_stop_loss_pct": 0.05,     # 5% trailing stop
        "prompt_modifier": (
            "MAXIMISE RETURNS — high-conviction, maximum aggression. "
            "Trade on ANY medium-or-better signal. NEVER hold when there's a tradeable opportunity. "
            "Actively use 3x leveraged ETFs (SOXL, TQQQ, SPXL, UPRO) on bullish days. "
            "Take FULL-SIZE positions on high-confidence. Accept medium-confidence at 75% size. "
            "Enter early on breakouts — before the move, not after. "
            "Set take_profit_pct aggressively: 0.15-0.25 for high-conviction momentum plays, "
            "0.10-0.15 for medium signals, 0.20-0.30 for leveraged ETFs on strong days. "
            "Set stop_loss_pct at 0.04-0.06 (wider stops let winners breathe). "
            "Set partial_exit=true whenever upside target is 15%+ — sell half at target, let other half compound. "
            "Capital must work at ALL times. Idle cash = missed profit."
        ),
    },
}

_DEFAULT = "aggressive"
_CACHE_KEY = "user_pref:strategy"
_CACHE_TTL = 31536000  # 365 days in seconds


def _read_key() -> str:
    value = cache_get(_CACHE_KEY)
    if isinstance(value, str) and value in STRATEGIES:
        return value
    return _DEFAULT


def _write_key(key: str):
    cache_set(_CACHE_KEY, key, _CACHE_TTL)


def get_strategy() -> dict:
    return STRATEGIES[_read_key()]


def get_all_strategies() -> list[dict]:
    return list(STRATEGIES.values())


def set_strategy(key: str) -> bool:
    if key not in STRATEGIES:
        return False
    _write_key(key)
    return True
