"""
Quick local test for wheel universe data fetching.
Run: python3 test_wheel_universe_local.py

Tests 3 things that were broken:
  1. Dynamic screener — options_enabled asset count from Alpaca
  2. Bar data — HV30 + drawdown for a few symbols
  3. Live IV — GetOptionContractsRequest + OptionSnapshotRequest for a few symbols

Does NOT require DB or Redis. Uses ALPACA_WHEEL_* env vars.
Set them before running:
  export ALPACA_WHEEL_KEY=...
  export ALPACA_WHEEL_SECRET=...
  export ALPACA_WHEEL_BASE_URL=https://paper-api.alpaca.markets
"""

import os, sys, math
from datetime import datetime, timezone, timedelta, date

KEY    = os.environ.get("ALPACA_WHEEL_KEY")
SECRET = os.environ.get("ALPACA_WHEEL_SECRET")
BASE   = os.environ.get("ALPACA_WHEEL_BASE_URL", "https://paper-api.alpaca.markets")

if not KEY or not SECRET:
    print("ERROR: set ALPACA_WHEEL_KEY and ALPACA_WHEEL_SECRET env vars first")
    sys.exit(1)

IS_PAPER = "paper" in BASE

print(f"Using: {'PAPER' if IS_PAPER else 'LIVE'} account\n")

# ── Clients ──────────────────────────────────────────────────────────────────
from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient

trading_client = TradingClient(KEY, SECRET, paper=IS_PAPER)
data_client    = StockHistoricalDataClient(KEY, SECRET)
opts_client    = OptionHistoricalDataClient(KEY, SECRET)

TEST_SYMBOLS = ["MARA", "COIN", "PLTR", "MU", "AMD", "INTC", "SMCI", "NVDA"]

# ── Test 1: Dynamic screener — verify options contracts as eligibility proxy ──
print("=" * 60)
print("TEST 1: Options eligibility via GetOptionContractsRequest probe")
print("=" * 60)
try:
    from alpaca.trading.requests import GetOptionContractsRequest
    from alpaca.trading.enums import ContractType

    today = date.today()
    exp_start = today + timedelta(days=30)
    exp_end   = today + timedelta(days=60)

    eligible = []
    for sym in TEST_SYMBOLS:
        try:
            resp = trading_client.get_option_contracts(GetOptionContractsRequest(
                underlying_symbols=[sym],
                type=ContractType.PUT,
                expiration_date_gte=str(exp_start),
                expiration_date_lte=str(exp_end),
            ))
            contracts = getattr(resp, "option_contracts", None) or []
            if contracts:
                eligible.append(sym)
                print(f"  {sym}: {len(contracts)} contracts ✓")
            else:
                print(f"  {sym}: 0 contracts ✗")
        except Exception as e:
            print(f"  {sym}: error — {e}")

    print(f"\n  Eligible: {eligible}")
except Exception as e:
    print(f"  FAILED: {e}")

# ── Test 2: Bar data ──────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("TEST 2: Bar data (HV30 + drawdown)")
print("=" * 60)
try:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=190)

    from alpaca.data.enums import DataFeed
    bars_resp = data_client.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=TEST_SYMBOLS,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DataFeed.IEX,
    ))

    # BarSet uses .data dict not .get()
    bar_data = getattr(bars_resp, "data", bars_resp) if hasattr(bars_resp, "data") else {}
    for sym in TEST_SYMBOLS:
        bars = bar_data.get(sym, [])
        n = len(bars) if bars else 0
        if n >= 5:
            closes = [float(b.close) for b in bars]
            recent = closes[-31:]
            returns = [math.log(recent[i]/recent[i-1]) for i in range(1, len(recent)) if recent[i-1] > 0]
            if returns:
                mean = sum(returns)/len(returns)
                var  = sum((r-mean)**2 for r in returns) / max(len(returns)-1, 1)
                hv30 = round(math.sqrt(var * 252) * 100, 1)
            else:
                hv30 = 0.0
            peak = closes[0]
            max_dd = 0.0
            for c in closes:
                if c > peak: peak = c
                dd = (peak-c)/peak if peak > 0 else 0
                if dd > max_dd: max_dd = dd
            print(f"  {sym}: {n} bars | HV30={hv30}% | drawdown={round(max_dd*100,1)}%")
        else:
            print(f"  {sym}: {n} bars (too few — will use defaults)")
except Exception as e:
    print(f"  FAILED: {e}")

# ── Test 3: Live IV fetch ─────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("TEST 3: IV approximation from Alpaca option bid/ask (no greeks needed)")
print("=" * 60)
# Approach: get the ~0.25-delta put strike (95% of price), fetch its bid/ask,
# back-compute IV using: mid_price ≈ stock_price × iv × sqrt(dte/365) × 0.4
# (same formula used in scoring, reversed)
try:
    from alpaca.trading.requests import GetOptionContractsRequest
    from alpaca.trading.enums import ContractType
    from alpaca.data.requests import OptionSnapshotRequest, StockLatestQuoteRequest

    today = date.today()
    exp_start = today + timedelta(days=30)
    exp_end   = today + timedelta(days=60)

    # HV30 from test 2 for reference
    hv_map = {"MARA": 78.3, "COIN": 68.8, "PLTR": 56.3, "MU": 106.0,
               "AMD": 88.0, "INTC": 94.4, "SMCI": 101.6, "NVDA": 45.9,
               "AAPL": 21.1, "BAC": 21.5, "VZ": 20.9}
    for sym in ["MARA", "COIN", "PLTR", "MU", "AMD", "SMCI", "NVDA"]:
        try:
            # Use latest trade price (more reliable than ask on paper accounts)
            from alpaca.data.requests import StockLatestTradeRequest
            trades = data_client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=sym))
            trade = trades.get(sym) if trades else None
            stock_price = float(trade.price) if trade and trade.price else 0
            if stock_price <= 0:
                # fallback to quote
                q = data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=sym))
                quote = q.get(sym)
                stock_price = float(quote.ask_price or quote.bid_price or 0) if quote else 0
            if stock_price <= 0:
                print(f"  {sym}: couldn't get stock price")
                continue

            target_strike = stock_price  # ATM for IV estimation

            # Get contracts
            resp = trading_client.get_option_contracts(GetOptionContractsRequest(
                underlying_symbols=[sym],
                type=ContractType.PUT,
                expiration_date_gte=str(exp_start),
                expiration_date_lte=str(exp_end),
                strike_price_gte=str(round(stock_price * 0.85, 2)),
                strike_price_lte=str(round(stock_price * 1.15, 2)),
            ))
            contracts = getattr(resp, "option_contracts", None) or []
            if not contracts:
                print(f"  {sym}: no contracts in 85-115% strike range")
                continue

            # Pick contract closest to target_strike AND closest to 45 DTE
            contracts = sorted(
                contracts,
                key=lambda c: (abs((c.expiration_date - today).days - 45), abs(float(c.strike_price) - target_strike))
            )
            best = contracts[0]
            dte = (best.expiration_date - today).days
            strike = float(best.strike_price)

            # Get snapshot for bid/ask
            snap = opts_client.get_option_snapshot(
                OptionSnapshotRequest(symbol_or_symbols=best.symbol)
            ).get(best.symbol)

            if not snap or not snap.latest_quote:
                print(f"  {sym}: no snapshot for {best.symbol}")
                continue

            bid = float(snap.latest_quote.bid_price or 0)
            ask = float(snap.latest_quote.ask_price or 0)
            mid = (bid + ask) / 2

            if mid <= 0 or stock_price <= 0 or dte <= 0:
                print(f"  {sym}: mid=${mid:.2f} bid=${bid:.2f} ask=${ask:.2f} — can't compute IV")
                continue

            # Back-compute IV: mid ≈ stock_price × iv × sqrt(dte/365) × 0.4
            iv_approx = mid / (stock_price * math.sqrt(dte / 365) * 0.4)
            iv_hv30 = hv_map.get(sym, 30.0)
            ratio = (iv_approx * 100) / iv_hv30 if iv_hv30 > 0 else 1.0

            print(f"  {sym}: price=${stock_price:.2f} | {best.symbol} DTE={dte} strike=${strike:.0f}")
            print(f"    bid=${bid:.2f} ask=${ask:.2f} mid=${mid:.2f}")
            print(f"    IV≈{round(iv_approx*100,1)}% | IV/HV≈{round(ratio,2)}x {'✓' if iv_approx > 0.05 else '✗'}")

        except Exception as e:
            print(f"  {sym}: FAILED — {e}")

except Exception as e:
    print(f"  FAILED: {e}")

print("\nDone.")
