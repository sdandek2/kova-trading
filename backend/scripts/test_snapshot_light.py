"""
Test that snapshot_light now returns MA50/MA200/year_high and that
MACD actually fires (was silently 0 with only 21 bars before).

Run locally with Railway env vars:
  railway run python3 backend/scripts/test_snapshot_light.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

print("=" * 60)
print("STRESS TEST: snapshot_light — real universe")
print("  Batches of 30, 10-hour cache, MA50/MA200/year_high + MACD/RSI")
print("=" * 60)

print("\nFetching real universe (same as live trading)...")
import time
from services.alpaca_service import get_tradeable_universe
t0 = time.time()
TEST_SYMBOLS = get_tradeable_universe()
print(f"  Universe: {len(TEST_SYMBOLS)} symbols  ({time.time()-t0:.1f}s)")

from services.alpaca_service import get_market_snapshot_light

print(f"\nCall 1 (cold — fetches bars from Alpaca):")
t0 = time.time()
snap = get_market_snapshot_light(TEST_SYMBOLS)
t1 = time.time()
print(f"  Elapsed: {t1-t0:.1f}s")

print(f"\nCall 2 (warm — should hit cache, only fetches live quotes):")
t0 = time.time()
snap2 = get_market_snapshot_light(TEST_SYMBOLS)
t1 = time.time()
print(f"  Elapsed: {t1-t0:.1f}s  (should be much faster)")

# Analyze first call results
got_ma50  = sum(1 for s in TEST_SYMBOLS if snap.get(s, {}).get("ma50"))
got_ma200 = sum(1 for s in TEST_SYMBOLS if snap.get(s, {}).get("ma200"))
got_price = sum(1 for s in TEST_SYMBOLS if snap.get(s, {}).get("current_price"))
no_data   = [s for s in TEST_SYMBOLS if not snap.get(s)]

print(f"\nResults across {len(TEST_SYMBOLS)} symbols:")
print(f"  Got MA50:          {got_ma50}/{len(TEST_SYMBOLS)}")
print(f"  Got MA200:         {got_ma200}/{len(TEST_SYMBOLS)}")
print(f"  Got current_price: {got_price}/{len(TEST_SYMBOLS)}")
if no_data:
    print(f"  No data at all:    {no_data}")

print(f"\nAll {len(TEST_SYMBOLS)} symbols:")
all_passed = True
for sym in TEST_SYMBOLS:
    d = snap.get(sym)
    if not d:
        print(f"\n  {sym}: NO DATA in snapshot")
        all_passed = False
        continue

    closes    = d.get("closing_prices", [])
    ma50      = d.get("ma50")
    ma200     = d.get("ma200")
    year_high = d.get("year_high")
    price     = d.get("current_price") or (closes[-1] if closes else 0)

    # Compute MACD + RSI from the new bar depth
    from services.indicators import compute_rsi, compute_macd
    rsi       = compute_rsi(closes) if len(closes) >= 15 else None
    macd_data = compute_macd(closes) if len(closes) >= 35 else {}
    macd_hist = macd_data.get("histogram")

    # Trend strength signal
    trend_boost = 0
    trend_label = "neutral"
    if ma50 and price and year_high:
        above_50  = price > ma50
        above_200 = ma200 and price > ma200
        golden    = ma200 and ma50 > ma200
        near_high = (price - year_high) / year_high >= -0.05
        if near_high and above_50 and above_200:
            trend_boost, trend_label = 8, "STRONG TREND"
        elif above_50 and golden:
            trend_boost, trend_label = 4, "GOLDEN CROSS"

    # Checks
    has_ma50      = ma50 is not None
    has_ma200     = ma200 is not None
    has_year_high = year_high is not None
    macd_fires    = macd_hist is not None  # was always None before (< 35 bars)
    rsi_ok        = rsi is not None

    status = "PASS" if (has_ma50 and has_ma200 and macd_fires and rsi_ok) else "WARN"
    if status == "WARN":
        all_passed = False

    print(f"\n  [{status}] {sym}")
    print(f"    bars={len(closes)}  price=${price}")
    print(f"    MA50={ma50}  MA200={ma200}  year_high={year_high}")
    print(f"    RSI={rsi}  MACD_hist={macd_hist}  (fires={macd_fires})")
    print(f"    trend_boost={trend_boost} ({trend_label})")

print("\n" + "=" * 60)
print("RESULT:", "ALL PASSED" if all_passed else "SOME WARNINGS — check above")
print("=" * 60)
