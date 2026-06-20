"""
Quick smoke-test for the FMP-based connectors.
Run locally:  FMP_API_KEY=xxx python backend/scripts/test_connectors.py
Run on Railway: railway run python backend/scripts/test_connectors.py
"""
import os, sys

# --- make backend importable ---
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

api_key = os.environ.get("FMP_API_KEY", "")
if not api_key:
    print("ERROR: FMP_API_KEY not set")
    sys.exit(1)

print(f"FMP_API_KEY present: {api_key[:6]}...{api_key[-4:]}\n")

TEST_SYMBOLS = ["AAPL", "NVDA", "MSFT"]

print("=" * 60)
print("TEST 1: quiver.py (institutional ownership via FMP profile)")
print("=" * 60)
from services.brain.connectors.quiver import get_darkpool_signal
for sym in TEST_SYMBOLS:
    result = get_darkpool_signal(sym)
    print(f"  {sym}: signal={result['signal']}  boost={result['conviction_boost']}  | {result['details']}")

print()
print("=" * 60)
print("TEST 2: fmp.py (analyst price target via FMP)")
print("=" * 60)
from services.brain.connectors.fmp import get_estimate_revision
import httpx

# Get live prices via FMP stable quote (same endpoint quiver.py uses)
prices = {}
for sym in TEST_SYMBOLS:
    try:
        r = httpx.get(
            "https://financialmodelingprep.com/stable/quote",
            params={"symbol": sym, "apikey": api_key},
            timeout=10,
        )
        data = r.json()
        prices[sym] = float((data[0] if isinstance(data, list) else data).get("price", 0))
    except Exception as e:
        prices[sym] = 0.0
        print(f"  {sym} price fetch failed: {e}")

for sym in TEST_SYMBOLS:
    result = get_estimate_revision(sym, current_price=prices[sym])
    print(f"  {sym} @ ${prices[sym]:.2f}: signal={result['signal']}  boost={result['conviction_boost']}  | {result['details']}")

print()
print("All connector tests complete.")
