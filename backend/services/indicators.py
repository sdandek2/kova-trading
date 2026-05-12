def compute_rsi(prices: list[float], period: int = 14) -> float:
    """Compute RSI using Wilder's smoothed moving average (standard definition)."""
    if len(prices) < period + 1:
        return 50.0  # neutral default

    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]

    # Seed with simple average over first period
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder smoothing for remaining bars
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def compute_macd(prices: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """Compute MACD using proper rolling EMA series (not non-overlapping window slices)."""
    if len(prices) < slow + signal:
        return {"macd": 0.0, "signal": 0.0, "histogram": 0.0}

    def ema_series(data: list[float], period: int) -> list[float]:
        k = 2 / (period + 1)
        result = [data[0]]
        for price in data[1:]:
            result.append(price * k + result[-1] * (1 - k))
        return result

    fast_ema = ema_series(prices, fast)
    slow_ema = ema_series(prices, slow)

    # Align: fast_ema is longer, trim to match slow_ema length
    offset = len(fast_ema) - len(slow_ema)
    macd_line = [f - s for f, s in zip(fast_ema[offset:], slow_ema)]

    signal_ema = ema_series(macd_line, signal)

    macd_val = macd_line[-1]
    signal_val = signal_ema[-1]

    return {
        "macd": round(macd_val, 4),
        "signal": round(signal_val, 4),
        "histogram": round(macd_val - signal_val, 4),
    }


def compute_moving_averages(prices: list[float]) -> dict:
    """Compute 20-day and 50-day simple moving averages."""
    result = {}
    if len(prices) >= 20:
        result["ma20"] = round(sum(prices[-20:]) / 20, 2)
    if len(prices) >= 50:
        result["ma50"] = round(sum(prices[-50:]) / 50, 2)
    return result


def compute_all(prices: list[float]) -> dict:
    """Compute all indicators from a price series."""
    return {
        "rsi": compute_rsi(prices),
        "macd": compute_macd(prices),
        "moving_averages": compute_moving_averages(prices),
    }


def compute_atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    """Average True Range — measures volatility."""
    if len(closes) < period + 1:
        return 0.0
    # Guard: mismatched list lengths crash the loop — return 0 so caller uses fallback sizing
    if len(highs) != len(closes) or len(lows) != len(closes):
        return 0.0

    true_ranges = []
    for i in range(1, len(closes)):
        high_low = highs[i] - lows[i]
        high_close = abs(highs[i] - closes[i-1])
        low_close = abs(lows[i] - closes[i-1])
        true_ranges.append(max(high_low, high_close, low_close))

    atr = sum(true_ranges[-period:]) / period
    return round(atr, 4)


def volatility_adjusted_quantity(
    portfolio_value: float,
    max_position_pct: float,
    current_price: float,
    atr: float,
    risk_per_trade_pct: float = 0.01,  # risk 1% of portfolio per trade
) -> int:
    """
    Kelly-inspired position sizing: risk a fixed % of portfolio per trade.
    Position size = (portfolio * risk_pct) / ATR
    High ATR (volatile) = fewer shares. Low ATR (stable) = more shares.
    """
    if atr <= 0 or current_price <= 0:
        # Fallback: use max position size
        return max(1, int((portfolio_value * max_position_pct) / current_price))

    risk_amount = portfolio_value * risk_per_trade_pct
    shares_by_risk = int(risk_amount / atr)
    shares_by_max = int((portfolio_value * max_position_pct) / current_price)

    # Take the smaller of risk-based and max-position-based sizing
    return max(1, min(shares_by_risk, shares_by_max))
