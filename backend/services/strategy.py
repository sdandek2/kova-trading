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
        "default_take_profit_pct": 0.07,   # 7% TP — realistic intraday target
        "default_stop_loss_pct": 0.03,     # 3% trailing stop
        "prompt_modifier": (
            "Balance risk and reward. Accept medium-to-high confidence trades. "
            "Mix growth stocks with ETFs. Moderate use of leveraged ETFs is fine. "
            "Aim for steady growth while managing downside. "
            "Set take_profit_pct between 0.05-0.12 and stop_loss_pct between 0.02-0.04. "
            "REQUIRED: take_profit_pct must be at least 2x stop_loss_pct. Skip any trade where R:R < 2:1."
        ),
    },
    "aggressive": {
        "key": "aggressive",
        "name": "Aggressive",
        "max_position_pct": 0.10,
        "min_confidence": "medium",
        "risk_per_trade_pct": 0.01,
        "sector_cap": 1,
        "default_take_profit_pct": 0.08,   # 8% TP — realistic intraday target
        "default_stop_loss_pct": 0.04,
        "prompt_modifier": (
            "Trade assertively, but only on medium-to-high confidence setups with clean momentum "
            "or clear mean-reversion edges. Avoid low-conviction churn. "
            "Leveraged ETFs are allowed only when the macro regime and price action both align. "
            "Take larger size on high-confidence setups, moderate size on medium-confidence setups, "
            "and skip low-confidence ideas. Do not stack correlated exposure recklessly. "
            "Prefer asymmetric entries over constant activity. Protect drawdown first, then press winners. "
            "Set take_profit_pct: 0.06-0.12 for strong stocks, 0.10-0.20 for leveraged ETFs only on strong trend days. "
            "NEVER set take_profit above 0.20 — unreachable targets cause positions to ride into stop losses. "
            "REQUIRED: take_profit_pct must be at least 2x stop_loss_pct. Skip any trade where R:R < 2:1. "
            "Set stop_loss_pct at 0.03-0.05 and use partial_exit=true when upside is meaningful. "
            "If the regime is volatile or bearish, reduce size and be selective rather than forcing trades."
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
