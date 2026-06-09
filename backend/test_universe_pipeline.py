"""
Shows how the 6230-stock Alpaca universe gets reduced at each pipeline stage.

Run:
  export ALPACA_WHEEL_KEY=...
  export ALPACA_WHEEL_SECRET=...
  python3 test_universe_pipeline.py
"""

import os, sys, math
from datetime import datetime, timezone, timedelta

KEY    = os.environ.get("ALPACA_WHEEL_KEY")
SECRET = os.environ.get("ALPACA_WHEEL_SECRET")
BASE   = os.environ.get("ALPACA_WHEEL_BASE_URL", "https://paper-api.alpaca.markets")

if not KEY or not SECRET:
    print("ERROR: set ALPACA_WHEEL_KEY and ALPACA_WHEEL_SECRET")
    sys.exit(1)

IS_PAPER = "paper" in BASE
os.environ.setdefault("ALPACA_WHEEL_KEY",      KEY)
os.environ.setdefault("ALPACA_WHEEL_SECRET",   SECRET)
os.environ.setdefault("ALPACA_WHEEL_BASE_URL", BASE)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.trading.requests import GetAssetsRequest
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.data.requests import StockSnapshotRequest

trading_client = TradingClient(KEY, SECRET, paper=IS_PAPER)
data_client    = StockHistoricalDataClient(KEY, SECRET)
opts_client    = OptionHistoricalDataClient(KEY, SECRET)

from services.wheel_universe import _STATIC_SEED, _WHEEL_EXCLUSIONS, _SECTOR_MAP, _DEFAULT_IV_HV_MIN, _DEFAULT_DRAWDOWN_MAX, _DEFAULT_IV_RANK_MIN, _DEFAULT_SCORE_MIN

def bar(label, n, total, detail=""):
    pct   = n / total * 100 if total else 0
    filled = int(pct / 2)
    bar   = "█" * filled + "░" * (50 - filled)
    print(f"  {label:<30} {n:>5} / {total:<5}  ({pct:5.1f}%)  {bar}  {detail}")

print("=" * 80)
print("WHEEL UNIVERSE PIPELINE — reduction at each stage")
print("=" * 80)

# ── Stage 1: Alpaca has_options ───────────────────────────────────────────────
print("\nSTAGE 1: Alpaca options-eligible universe (has_options)")
assets = list(trading_client.get_all_assets(GetAssetsRequest(
    asset_class=AssetClass.US_EQUITY,
    status=AssetStatus.ACTIVE,
    attributes="has_options",
)) or [])
s1_total = len(assets)
print(f"  Raw has_options assets: {s1_total}")

# ── Stage 2: tradable=True + ticker format (alpha, ≤5 chars) ─────────────────
s2 = [a for a in assets if getattr(a, "tradable", False) and getattr(a, "symbol","").isalpha() and len(getattr(a,"symbol","")) <= 5]
s2_syms = [a.symbol for a in s2]
print(f"\nSTAGE 2: tradable=True + alpha ticker ≤5 chars")
bar("after format filter", len(s2), s1_total)

# ── Stage 3: Remove exclusions (ETFs, leveraged, etc.) ───────────────────────
s3_syms = [s for s in s2_syms if s not in _WHEEL_EXCLUSIONS]
print(f"\nSTAGE 3: Remove wheel exclusions ({len(_WHEEL_EXCLUSIONS)} blocked symbols)")
bar("after exclusion filter", len(s3_syms), len(s2_syms))

# ── Stage 4: Price $5–$120 + Volume >500k via batch snapshots ────────────────
print(f"\nSTAGE 4: Price $5–$120 + Volume >500k  (batch snapshots, {len(s3_syms)} calls)")
MIN_PRICE, MAX_PRICE, MIN_VOLUME = 5.0, 120.0, 500_000

filtered  = []
too_cheap = too_exp = low_vol = no_data = 0
batch_size = 100
total_batches = (len(s3_syms) + batch_size - 1) // batch_size

for i in range(0, len(s3_syms), batch_size):
    batch = s3_syms[i:i+batch_size]
    bn    = i // batch_size + 1
    print(f"  Batch {bn}/{total_batches} ({len(batch)} symbols)...", end="\r")
    try:
        snaps = data_client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=batch))
        for sym in batch:
            snap = snaps.get(sym) if snaps else None
            if not snap or not snap.latest_trade:
                no_data += 1
                continue
            price = float(snap.latest_trade.price)
            vol   = float(snap.daily_bar.volume) if snap.daily_bar else 0
            if price < MIN_PRICE:
                too_cheap += 1
            elif price > MAX_PRICE:
                too_exp += 1
            elif vol < MIN_VOLUME:
                low_vol += 1
            else:
                filtered.append({"symbol": sym, "price": round(price,2), "volume_m": round(vol/1e6,1)})
    except Exception as e:
        no_data += len(batch)

print(f"  {'':80}")  # clear \r line
bar("passed price+volume", len(filtered), len(s3_syms))
print(f"    too cheap (<$5):    {too_cheap}")
print(f"    too expensive(>$120): {too_exp}")
print(f"    low volume (<500k): {low_vol}")
print(f"    no data:            {no_data}")

# ── Stage 5: Top 150 by volume ────────────────────────────────────────────────
filtered.sort(key=lambda x: x["volume_m"], reverse=True)
top150 = filtered[:150]
print(f"\nSTAGE 5: Top 150 by volume")
bar("after limit=150", len(top150), len(filtered))
print(f"  Volume range: {top150[-1]['volume_m']}M – {top150[0]['volume_m']}M")
print(f"  Top 20: {[s['symbol'] for s in top150[:20]]}")

# ── Stage 6: IV/HV scoring (just show what thresholds will do) ────────────────
print(f"\nSTAGE 6: IV/HV scoring + adaptive filters (thresholds, not run here)")
print(f"  Will score all {len(top150)} symbols for HV30 + live IV")
print(f"  Default filters: score≥{_DEFAULT_SCORE_MIN}, IV/HV≥{_DEFAULT_IV_HV_MIN}, DD≤{_DEFAULT_DRAWDOWN_MAX}%, IVrank≥{_DEFAULT_IV_RANK_MIN}")
print(f"  Auto-relax up to 4x if <6 pass")
print(f"  Top 20 go to AI for final 8-10 picks")

# ── Summary waterfall ─────────────────────────────────────────────────────────
print("\n" + "=" * 80)
print("FUNNEL SUMMARY")
print("=" * 80)
stages = [
    ("Alpaca has_options",        s1_total),
    ("Tradable + format filter",  len(s2_syms)),
    ("After exclusions",          len(s3_syms)),
    ("Price $5-$120 + vol >500k", len(filtered)),
    ("Top 150 by volume",         len(top150)),
    ("After IV/HV scoring",       "~20–40 (estimated)"),
    ("AI final picks",            "8–10"),
]
for label, n in stages:
    if isinstance(n, int):
        bar(label, n, s1_total)
    else:
        print(f"  {label:<30}  {n}")

print()
