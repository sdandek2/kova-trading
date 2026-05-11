from services.db import cache_get, cache_set

# Strategies define how aggressively the bot trades
STRATEGIES = {
    "conservative": {
        "key": "conservative",
        "name": "Conservative",
        "max_position_pct": 0.05,       # max 5% of portfolio per trade
        "min_confidence": "high",        # only high-confidence trades
        "prompt_modifier": (
            "Be very cautious. Only trade with very strong, high-confidence signals. "
            "Prefer defensive ETFs and blue chips. Avoid penny stocks and leveraged ETFs. "
            "Prioritise capital preservation over gains."
        ),
    },
    "balanced": {
        "key": "balanced",
        "name": "Balanced",
        "max_position_pct": 0.10,       # max 10% of portfolio per trade
        "min_confidence": "medium",
        "prompt_modifier": (
            "Balance risk and reward. Accept medium-to-high confidence trades. "
            "Mix growth stocks with ETFs. Moderate use of leveraged ETFs is fine. "
            "Aim for steady growth while managing downside."
        ),
    },
    "aggressive": {
        "key": "aggressive",
        "name": "Aggressive",
        "max_position_pct": 0.25,       # max 25% of portfolio per trade
        "min_confidence": "medium",
        "prompt_modifier": (
            "MAXIMISE RETURNS — this is a high-conviction, high-aggression strategy. "
            "Trade on ANY medium-or-better signal. Do NOT sit on the sidelines with 'hold' — always find the best available trade. "
            "Actively use 3x leveraged ETFs (SOXL, TQQQ, SPXL, UPRO) for market-aligned momentum. "
            "Prioritise stocks with the strongest 5-day momentum, RSI breakouts, and news catalysts. "
            "Take FULL-SIZE positions on high-confidence signals. Accept medium-confidence trades at 75% size. "
            "Speed matters — enter early on breakouts, not after the move. Profit targets are aggressive (10-20% moves). "
            "Capital must work at all times — idle cash is a missed opportunity. Prioritise GROWTH over safety."
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
