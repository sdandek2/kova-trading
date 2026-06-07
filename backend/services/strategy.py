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
        "max_position_pct": 0.15,
        "min_confidence": "medium",
        "risk_per_trade_pct": 0.015,       # 1.5% of portfolio risked per trade
        "sector_cap": 2,                   # allow 2 positions per sector
        "default_take_profit_pct": 0.20,   # 20% TP — let winners run far
        "default_stop_loss_pct": 0.04,     # 4% trailing stop — tight enough to protect capital
        "prompt_modifier": (
            "AGGRESSIVE GROWTH — your objective is strong risk-adjusted returns. "
            "Prioritise medium-to-high confidence trades. Low-confidence signals require a clear catalyst. "
            "Use 3x leveraged ETFs (SOXL, TQQQ, SPXL, UPRO, FNGU) only when market regime is bullish and VIX is low/normal. "
            "Take FULL-SIZE positions (15% of portfolio) on high-confidence. "
            "75% size on medium-confidence. Only trade low-confidence if there is a strong news catalyst. "
            "Enter on confirmed breakouts — wait for price to close above resistance, not just touch it. "
            "Set take_profit_pct: 0.20-0.30 for high-conviction, 0.15-0.20 for medium, "
            "0.25-0.40 for 3x leveraged ETFs on strong trend days. "
            "Set stop_loss_pct at 0.03-0.05 — protect capital, cut losers quickly. "
            "Set partial_exit=true on any trade with 20%+ upside target — bank half, let rest compound. "
            "Never hold a loser hoping for recovery — exit and redeploy. "
            "The goal is consistent profitable trades, not maximum trade count."
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
