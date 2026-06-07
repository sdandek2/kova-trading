"""
Session 6 — SEC Form 4 Insider Trading via EDGAR (free, no API key).

Detects open-market insider BUY transactions and injects those stocks into
the tradeable universe. Only buys count — sells are ignored entirely because
insiders sell for routine reasons (taxes, RSU vesting, diversification, etc.)
but buying with cash is always intentional and bullish.

Filing requirement: insiders must file within 2 business days.
Data lag: typically 2–4 days from transaction to EDGAR visibility.

Signal:
  Net insider bought > $500K last 30 days → +15 conviction pts
  Net insider bought > $100K last 30 days → +8 conviction pts
  Sells: 0 pts (ignored)

Universe injection: symbols with qualifying buys are injected for 30 days.
"""
import logging
import time
from datetime import date, timedelta

import httpx

logger = logging.getLogger(__name__)

_EDGAR_BASE = "https://data.sec.gov"
_EDGAR_HEADERS = {"User-Agent": "Kova Trading kova@trading.com"}

# Cache for the full company ticker → CIK mapping (large, refreshed weekly)
_TICKER_CIK_MAP: dict[str, str] = {}  # symbol → 10-digit CIK string
_CIK_MAP_FETCHED: float = 0.0
_CIK_MAP_TTL = 7 * 86_400  # weekly

# Per-symbol insider buy cache
_INSIDER_CACHE: dict[str, dict] = {}
_CACHE_TTL = 3_600  # 1 hour

# Inject window: symbol → expiry timestamp
_inject_until: dict[str, float] = {}

# Amount thresholds
_THRESHOLD_HIGH = 500_000   # $500K → +15 pts
_THRESHOLD_LOW = 100_000    # $100K → +8 pts
_INJECT_WINDOW_DAYS = 30    # inject symbol into universe for 30 days post-buy


def _ensure_cik_map() -> None:
    """Load SEC company_tickers.json — maps ticker symbols to CIK numbers."""
    global _CIK_MAP_FETCHED
    if time.time() - _CIK_MAP_FETCHED < _CIK_MAP_TTL and _TICKER_CIK_MAP:
        return

    try:
        resp = httpx.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=_EDGAR_HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        # Data format: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
        _TICKER_CIK_MAP.clear()
        for entry in data.values():
            ticker = str(entry.get("ticker") or "").upper().strip()
            cik = str(entry.get("cik_str") or "")
            if ticker and cik:
                _TICKER_CIK_MAP[ticker] = cik.zfill(10)
        logger.info("SEC CIK map loaded: %d tickers", len(_TICKER_CIK_MAP))
        _CIK_MAP_FETCHED = time.time()
    except Exception as e:
        logger.warning("SEC CIK map load failed: %s", e)


def _fetch_form4_buys(cik: str, days_back: int = 30) -> dict:
    """
    Fetch recent Form 4 filings for a CIK and sum up net cash buys.

    Returns:
        {
          "net_bought_usd": float,  # total $ of open-market buys (sells excluded)
          "buy_count": int,
          "latest_buy_date": str,
          "largest_buy_usd": float,
        }
    """
    url = f"{_EDGAR_BASE}/submissions/CIK{cik}.json"
    try:
        resp = httpx.get(url, headers=_EDGAR_HEADERS, timeout=15)
        resp.raise_for_status()
        submission = resp.json()
    except Exception as e:
        logger.debug("SEC submissions CIK %s: %s", cik, e)
        return {"net_bought_usd": 0, "buy_count": 0, "latest_buy_date": "", "largest_buy_usd": 0}

    filings = submission.get("filings", {}).get("recent", {})
    forms = filings.get("form", [])
    filing_dates = filings.get("filingDate", [])
    accession_numbers = filings.get("accessionNumber", [])

    cutoff = date.today() - timedelta(days=days_back)
    net_bought = 0.0
    buy_count = 0
    latest_buy_date = ""
    largest_buy = 0.0

    for form, filing_date_str, accession in zip(forms, filing_dates, accession_numbers):
        if form != "4":
            continue
        try:
            filing_date = date.fromisoformat(filing_date_str)
        except Exception:
            continue
        if filing_date < cutoff:
            # Filings are reverse-chronological — break on first old filing
            break

        # Fetch the actual Form 4 XML to get transaction details
        acc_clean = accession.replace("-", "")
        xml_url = (
            f"{_EDGAR_BASE}/Archives/edgar/data/{int(cik)}"
            f"/{acc_clean}/form4.xml"
        )
        try:
            xml_resp = httpx.get(xml_url, headers=_EDGAR_HEADERS, timeout=10)
            if xml_resp.status_code != 200:
                # Try alternate filename format
                continue
            xml_text = xml_resp.text
        except Exception:
            continue

        # Parse transactions from XML
        # Look for nonDerivativeTransaction blocks
        import re
        transactions = re.findall(
            r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>",
            xml_text, re.DOTALL
        )

        for tx in transactions:
            # Transaction code: A=acquired open market, D=disposed, S=sale, etc.
            code_match = re.search(r"<transactionCode>(.*?)</transactionCode>", tx)
            if not code_match:
                continue
            code = code_match.group(1).strip()

            # Only count open-market purchases (code 'P') — ignore grants, awards (code 'A')
            if code != "P":
                continue

            shares_match = re.search(
                r"<transactionShares>.*?<value>(.*?)</value>", tx, re.DOTALL
            )
            price_match = re.search(
                r"<transactionPricePerShare>.*?<value>(.*?)</value>", tx, re.DOTALL
            )
            if not shares_match or not price_match:
                continue

            try:
                shares = float(shares_match.group(1))
                price = float(price_match.group(1))
                tx_value = shares * price
            except (ValueError, TypeError):
                continue

            net_bought += tx_value
            buy_count += 1
            if not latest_buy_date:
                latest_buy_date = filing_date_str
            if tx_value > largest_buy:
                largest_buy = tx_value

    return {
        "net_bought_usd": net_bought,
        "buy_count": buy_count,
        "latest_buy_date": latest_buy_date,
        "largest_buy_usd": largest_buy,
    }


# ── Public API ────────────────────────────────────────────────────────────────

def get_insider_signal(symbol: str) -> dict:
    """
    Return insider buying signal for a symbol.

    Returns:
        {
          "signal": "buying" | "neutral" | "unavailable",
          "conviction_boost": int,
          "details": str
        }
    """
    sym = symbol.upper().strip()
    cached = _INSIDER_CACHE.get(sym)
    if cached and time.time() < cached.get("_expires", 0):
        return {k: v for k, v in cached.items() if not k.startswith("_")}

    _ensure_cik_map()
    cik = _TICKER_CIK_MAP.get(sym)
    if not cik:
        result = {
            "signal": "unavailable",
            "conviction_boost": 0,
            "details": f"CIK not found for {sym}",
        }
        _INSIDER_CACHE[sym] = {**result, "_expires": time.time() + _CACHE_TTL}
        return result

    data = _fetch_form4_buys(cik, days_back=30)
    net = data["net_bought_usd"]
    count = data["buy_count"]
    latest = data["latest_buy_date"]

    if net >= _THRESHOLD_HIGH:
        boost = 15
        signal = "buying"
        detail = (f"insiders bought ${net:,.0f} open-market "
                  f"({count} txns, latest {latest})")
    elif net >= _THRESHOLD_LOW:
        boost = 8
        signal = "buying"
        detail = (f"insiders bought ${net:,.0f} open-market "
                  f"({count} txns, latest {latest})")
    else:
        boost = 0
        signal = "neutral"
        detail = f"no significant insider buying (${net:,.0f} in last 30 days)"

    # Register for universe injection if qualifying buy
    if boost > 0:
        _inject_until[sym] = time.time() + _INJECT_WINDOW_DAYS * 86400

    result = {"signal": signal, "conviction_boost": boost, "details": detail}
    _INSIDER_CACHE[sym] = {**result, "_expires": time.time() + _CACHE_TTL}
    return result


def get_universe_additions() -> list[str]:
    """
    Return list of symbols to inject into the tradeable universe.
    These are stocks with significant insider open-market buying in the last 30 days.
    Called by alpaca_service.get_tradeable_universe() once per cycle.
    """
    now = time.time()
    return [sym for sym, expiry in _inject_until.items() if expiry > now]


def scan_for_insider_buys(symbols: list[str]) -> dict[str, dict]:
    """
    Batch scan a list of symbols for insider buying activity.
    Rate-limited by EDGAR — use sparingly, max ~50 symbols/hour.
    Results are cached per symbol for 1 hour.

    Returns:
        {symbol: insider_signal_dict}
    """
    results = {}
    for sym in symbols:
        results[sym] = get_insider_signal(sym)
        time.sleep(0.2)  # EDGAR rate limit: ~5 requests/second max
    return results
