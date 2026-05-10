import os

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
        "max_position_pct": 0.20,       # max 20% of portfolio per trade
        "min_confidence": "medium",
        "prompt_modifier": (
            "Maximise returns. Accept medium-confidence trades. "
            "Actively use leveraged ETFs (SOXL, TQQQ, SPXL) and high-momentum stocks. "
            "Take larger positions on strong signals. Prioritise growth over safety."
        ),
    },
}

_SETTING_FILE = os.path.join(os.path.dirname(__file__), "..", "strategy_setting.txt")
_DEFAULT = "aggressive"


def _read_key() -> str:
    try:
        with open(_SETTING_FILE, "r") as f:
            key = f.read().strip().lower()
            if key in STRATEGIES:
                return key
    except FileNotFoundError:
        pass
    return _DEFAULT


def _write_key(key: str):
    with open(_SETTING_FILE, "w") as f:
        f.write(key)


def get_strategy() -> dict:
    return STRATEGIES[_read_key()]


def get_all_strategies() -> list[dict]:
    return list(STRATEGIES.values())


def set_strategy(key: str) -> bool:
    if key not in STRATEGIES:
        return False
    _write_key(key)
    return True
