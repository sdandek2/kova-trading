"""
sector_momentum.py — Maps stocks to sectors and scores sector momentum.
Used to boost/reduce conviction on trades based on whether the whole sector is moving.
"""
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

# Map of ticker -> sector name
STOCK_SECTOR_MAP = {
    # Tech / Software
    "AAPL": "Tech", "MSFT": "Tech", "GOOGL": "Tech", "META": "Tech",
    "AMZN": "Tech", "NVDA": "Semis", "AMD": "Semis", "INTC": "Semis",
    "TSM": "Semis", "AVGO": "Semis", "QCOM": "Semis", "AMAT": "Semis",
    "MU": "Semis", "ASML": "Semis", "LRCX": "Semis",
    # Leveraged ETFs -> their underlying sector
    "SOXL": "Semis", "SOXS": "Semis",
    "TQQQ": "Tech", "SQQQ": "Tech",
    "SPXL": "Broad", "SPXS": "Broad", "UPRO": "Broad",
    # Finance
    "JPM": "Finance", "BAC": "Finance", "GS": "Finance", "MS": "Finance",
    "WFC": "Finance", "C": "Finance", "BRK.B": "Finance",
    # Healthcare
    "JNJ": "Health", "UNH": "Health", "PFE": "Health", "MRNA": "Health",
    "ABBV": "Health", "LLY": "Health",
    # Energy
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy",
    # Consumer
    "TSLA": "Consumer", "AMZN": "Consumer", "HD": "Consumer", "NKE": "Consumer",
    # Broad market
    "SPY": "Broad", "QQQ": "Tech", "IWM": "Broad",
    # Crypto-related
    "MSTR": "Crypto", "COIN": "Crypto",
}

# Sector ETFs used to measure sector momentum
SECTOR_ETFS = {
    "Tech": "XLK",
    "Semis": "SOXX",
    "Finance": "XLF",
    "Health": "XLV",
    "Energy": "XLE",
    "Consumer": "XLY",
    "Broad": "SPY",
    "Crypto": "MSTR",  # Use MSTR as crypto proxy
}


def get_sector_for_symbol(symbol: str) -> str:
    """Return the sector name for a symbol, or 'Unknown'."""
    return STOCK_SECTOR_MAP.get(symbol.upper(), "Unknown")


def get_sector_momentum_scores(lookback_days: int = 3) -> dict[str, float]:
    """
    Return {sector_name: pct_change} for each sector over the last `lookback_days` days.
    Positive = sector is rising, negative = falling.
    Used to boost confidence when a stock's sector is also trending up.
    """
    from config import settings
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    scores = {}
    try:
        client = StockHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_secret_key)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=lookback_days + 3)  # +3 for weekends

        etf_symbols = list(set(SECTOR_ETFS.values()))
        bars = client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=etf_symbols,
                timeframe=TimeFrame.Day,
                feed="iex",
                start=start,
                end=end,
            )
        )
        bars_dict = bars.data if hasattr(bars, "data") else dict(bars)

        for sector, etf in SECTOR_ETFS.items():
            etf_bars = bars_dict.get(etf, [])
            if len(etf_bars) >= 2:
                change = (float(etf_bars[-1].close) - float(etf_bars[0].close)) / float(etf_bars[0].close) * 100
                scores[sector] = round(change, 2)
    except Exception as e:
        logger.warning(f"Could not compute sector momentum: {e}")

    return scores


def get_sector_context_for_symbols(symbols: list[str], scores: dict[str, float]) -> dict[str, dict]:
    """
    For each symbol, return its sector and whether that sector is bullish/bearish.
    Returns {symbol: {"sector": str, "sector_pct": float, "sector_signal": "bullish"|"bearish"|"neutral"}}
    """
    result = {}
    for sym in symbols:
        sector = get_sector_for_symbol(sym)
        pct = scores.get(sector, 0.0)
        if pct >= 1.5:
            signal = "bullish"
        elif pct <= -1.5:
            signal = "bearish"
        else:
            signal = "neutral"
        result[sym] = {"sector": sector, "sector_pct": pct, "sector_signal": signal}
    return result
