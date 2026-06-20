"""
Quick smoke-test for connectors + Alpaca bars.

Run locally:
  FMP_API_KEY=xxx ALPACA_API_KEY=xxx ALPACA_SECRET_KEY=xxx python backend/scripts/test_connectors.py

Run on Railway (all env vars already set):
  railway run python backend/scripts/test_connectors.py

Add your symbols to TEST_SYMBOLS below.
"""
import os, sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_SYMBOLS = ["SOXL", "TQQQ", "SPCX", "MU", "VCX", "VOO", "APLD", "RXRX"]

# ── TEST 1: Alpaca bars (MA50 / MA200 / 52w-high) ────────────────────────────
print("=" * 60)
print("TEST 1: Alpaca bars (MA50 / MA200 / 52w-high)")
print("  Free tier: IEX feed, 200 calls/min, 7+ years history")
print("  Limit: 1000 data points per response (total, not per symbol)")
print("=" * 60)

alpaca_key    = os.environ.get("ALPACA_API_KEY", "")
alpaca_secret = os.environ.get("ALPACA_SECRET_KEY", "")

if not alpaca_key or not alpaca_secret:
    print("  SKIP — ALPACA_API_KEY / ALPACA_SECRET_KEY not set\n")
else:
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        client = StockHistoricalDataClient(alpaca_key, alpaca_secret)
        end   = datetime.now()
        start = end - timedelta(days=310)  # ~200 trading days

        n_symbols = len(TEST_SYMBOLS)
        est_bars  = 213  # approx trading days in 310 calendar days
        print(f"  Requesting {n_symbols} symbols × ~{est_bars} bars = ~{n_symbols * est_bars} data points")
        print(f"  (limit=10000 set to avoid 1000-point default cap)\n")

        req  = StockBarsRequest(
            symbol_or_symbols=TEST_SYMBOLS,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            limit=10000,
        )
        bars = client.get_stock_bars(req).data

        for sym in TEST_SYMBOLS:
            sym_bars = bars.get(sym, [])
            if not sym_bars:
                print(f"  {sym}: NO DATA")
                continue
            closes = [b.close for b in sym_bars]
            highs  = [b.high  for b in sym_bars]
            n      = len(closes)
            price  = closes[-1]
            ma50   = round(sum(closes[-50:]) / 50, 2)   if n >= 50  else None
            ma200  = round(sum(closes[-200:]) / 200, 2) if n >= 200 else None
            h52    = max(highs)
            pct    = round((price - h52) / h52 * 100, 1)

            above_50  = ma50  and price > ma50
            above_200 = ma200 and price > ma200
            golden    = ma50 and ma200 and ma50 > ma200
            near_high = pct >= -5

            if near_high and above_50 and above_200:
                boost, label = 8, "STRONG TREND"
            elif above_50 and golden:
                boost, label = 4, "GOLDEN CROSS"
            else:
                boost, label = 0, "neutral"

            ma50_str  = f"${ma50}"  if ma50  else "n/a (< 50 bars)"
            ma200_str = f"${ma200}" if ma200 else "n/a (< 200 bars)"
            print(f"  {sym}: bars={n}  price=${price}  MA50={ma50_str}  MA200={ma200_str}"
                  f"  52wH=${round(h52,2)}  from52w={pct}%  -> boost={boost} ({label})")

    except Exception as e:
        print(f"  ERROR: {e}")

# ── TEST 2: quiver.py (FMP trend strength — limited to FMP whitelist) ─────────
print()
print("=" * 60)
print("TEST 2: quiver.py (FMP stable/quote — MA50/MA200/52wH)")
print("  Free tier: 250 calls/day, only FMP-whitelisted symbols work")
print("=" * 60)

fmp_key = os.environ.get("FMP_API_KEY", "")
if not fmp_key:
    print("  SKIP — FMP_API_KEY not set\n")
else:
    from services.brain.connectors.quiver import get_darkpool_signal
    for sym in TEST_SYMBOLS:
        result = get_darkpool_signal(sym)
        print(f"  {sym}: signal={result['signal']}  boost={result['conviction_boost']}  | {result['details']}")

# ── TEST 3: fmp.py (analyst price targets — limited to FMP whitelist) ─────────
print()
print("=" * 60)
print("TEST 3: fmp.py (analyst price targets) — DISABLED in scoring")
print("  Re-enable in signals.py once subscribed to FMP Starter ($20/mo)")
print("=" * 60)

if not fmp_key:
    print("  SKIP — FMP_API_KEY not set\n")
else:
    import httpx
    from services.brain.connectors.fmp import get_estimate_revision

    prices = {}
    for sym in TEST_SYMBOLS:
        try:
            r = httpx.get(
                "https://financialmodelingprep.com/stable/quote",
                params={"symbol": sym, "apikey": fmp_key},
                timeout=10,
            )
            data = r.json()
            prices[sym] = float((data[0] if isinstance(data, list) else data).get("price", 0))
        except Exception:
            prices[sym] = 0.0

    for sym in TEST_SYMBOLS:
        result = get_estimate_revision(sym, current_price=prices[sym])
        print(f"  {sym} @ ${prices[sym]:.2f}: signal={result['signal']}  boost={result['conviction_boost']}  | {result['details']}")

print()
print("All tests complete.")
