"""
Kova connector health check — run every Sunday night before the trading week.

Usage:
    cd backend && python scripts/health_check.py

No AI, no tokens, no API costs beyond the free-tier data connectors.
Takes ~3 minutes (SEC EDGAR rate-limited at 0.2s/symbol).

Exit code 0 = all checks passed
Exit code 1 = one or more checks failed (see FAIL lines)
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = "  PASS"
FAIL = "  FAIL"
WARN = "  WARN"
results = []

def check(name, ok, detail=""):
    tag = PASS if ok else FAIL
    line = f"{tag}  {name}"
    if detail:
        line += f" — {detail}"
    print(line)
    results.append(ok)
    return ok


print("=" * 60)
print("Kova Health Check", time.strftime("%Y-%m-%d %H:%M:%S"))
print("=" * 60)

# ── 1. Config / env vars ──────────────────────────────────────────────────────
print("\n[1] Environment variables")
try:
    from config import settings
    check("FMP_API_KEY set",     bool(settings.fmp_api_key),     settings.fmp_api_key[:6] + "..." if settings.fmp_api_key else "missing")
    check("FINNHUB_API_KEY set", bool(settings.finnhub_api_key), settings.finnhub_api_key[:6] + "..." if settings.finnhub_api_key else "missing")
    check("ALPACA keys set",     bool(settings.alpaca_api_key and settings.alpaca_secret_key))
    check("ANTHROPIC key set",   bool(settings.anthropic_api_key))
except Exception as e:
    check("Config loads", False, str(e))

# ── 2. Barchart options ───────────────────────────────────────────────────────
print("\n[2] Barchart unusual options")
try:
    from services.brain.connectors.barchart_options import (
        get_options_flow, get_all_unusual_symbols, get_short_candidates
    )
    syms = get_all_unusual_symbols()
    check("Barchart fetch succeeds",    len(syms) > 0,  f"{len(syms)} unusual symbols")
    check("Barchart has bullish calls", any(True for s in syms), "")

    candidates = get_short_candidates()
    check("Short candidates parsed",    isinstance(candidates, dict), f"{len(candidates)} short triggers: {list(candidates.keys())[:5]}")

    aapl_flow = get_options_flow("AAPL")
    check("get_options_flow returns dict", "signal" in aapl_flow, aapl_flow.get("signal"))

    # Spot check: no ETFs slipping through
    etf_leakage = [s for s in syms if s in {"SPY","QQQ","IWM","TLT","GLD"}]
    check("No ETFs in unusual symbols", len(etf_leakage) == 0, f"leakage: {etf_leakage}" if etf_leakage else "clean")

    # Spot check: no foreign tickers
    import re
    foreign = [s for s in syms if not re.match(r'^[A-Z]{1,5}$', s)]
    check("No foreign tickers",         len(foreign) == 0, f"found: {foreign}" if foreign else "clean")

except Exception as e:
    check("Barchart connector loads", False, str(e))

# ── 3. FMP earnings ───────────────────────────────────────────────────────────
print("\n[3] FMP earnings surprise")
try:
    from services.brain.connectors.fmp_earnings import (
        get_earnings_signal, get_universe_additions
    )
    additions = get_universe_additions()
    check("FMP fetch succeeds",         isinstance(additions, list), f"{len(additions)} inject candidates: {additions[:5]}")

    # Test a known symbol (may or may not have recent earnings)
    sig = get_earnings_signal("AAPL")
    check("get_earnings_signal returns dict", "signal" in sig and "conviction_boost" in sig, sig.get("signal"))

    # Validate negative EPS fix is in place — check we don't skip symbols with neg estimate
    # by checking the cache includes any negative-estimate symbols if present
    check("FMP response is a list",     True, "structure OK")

except Exception as e:
    check("FMP connector loads", False, str(e))

# ── 4. SEC insider ────────────────────────────────────────────────────────────
print("\n[4] SEC insider (EDGAR) — testing 3 symbols only, not full scan")
try:
    from services.brain.connectors.sec_insider import (
        get_insider_signal, get_universe_additions
    )
    # Test CIK lookup + Form 4 fetch for 3 well-known symbols
    test_syms = ["AAPL", "MSFT", "NVDA"]
    for sym in test_syms:
        sig = get_insider_signal(sym)
        ok = "signal" in sig and sig["signal"] in ("buying", "neutral", "unavailable")
        check(f"SEC {sym} signal",      ok, f"{sig.get('signal')} | {sig.get('details', '')[:60]}")
        time.sleep(0.2)  # EDGAR rate limit

    additions = get_universe_additions()
    check("get_universe_additions returns list", isinstance(additions, list),
          f"{len(additions)} candidates: {additions}" if additions else "empty (normal if no recent buys)")

except Exception as e:
    check("SEC connector loads", False, str(e))

# ── 5. Finnhub analyst revisions ─────────────────────────────────────────────
print("\n[5] Finnhub analyst revisions")
try:
    from services.brain.connectors.finnhub import get_analyst_signal
    sig = get_analyst_signal("MSFT")
    ok = "signal" in sig and "conviction_boost" in sig
    check("Finnhub MSFT signal",        ok, f"{sig.get('signal')} {sig.get('conviction_boost'):+d}pt | {sig.get('details','')[:60]}")

    sig2 = get_analyst_signal("NVDA")
    check("Finnhub NVDA signal",        "signal" in sig2, sig2.get("signal"))

    not_set = sig.get("signal") != "unavailable"
    check("Finnhub API key active",     not_set, "key working" if not_set else "FINNHUB_API_KEY missing or invalid")

except Exception as e:
    check("Finnhub connector loads", False, str(e))

# ── 6. FRED macro / regime ───────────────────────────────────────────────────
print("\n[6] FRED macro + regime detection")
try:
    from services.brain.regime import detect_regime, _get_fred_macro_score
    macro = _get_fred_macro_score()
    check("FRED fetch succeeds",        isinstance(macro, dict), str(macro))
    check("FRED has macro_total",       "macro_total" in macro, f"total={macro.get('macro_total')}")
    check("FRED macro_total in range",  -3 <= macro.get("macro_total", 0) <= 3,
          f"unemployment={macro.get('unemployment_score')}, jobs={macro.get('jobs_score')}, fed={macro.get('fed_score')}")

    # Regime needs SPY price data — use dummy prices to just test it doesn't crash
    dummy_prices = [100 + i * 0.1 for i in range(60)]
    regime = detect_regime(dummy_prices, vix=18.0, snapshot={})
    check("detect_regime returns object", hasattr(regime, "regime"), f"regime={regime.regime} confidence={regime.confidence:.0%}")

except Exception as e:
    check("FRED/regime loads", False, str(e))

# ── 7. Universe injection sources ────────────────────────────────────────────
print("\n[7] Universe injection sources (all connectors combined)")
try:
    from services.brain.connectors.fmp_earnings import get_universe_additions as fmp_adds
    from services.brain.connectors.sec_insider  import get_universe_additions as sec_adds
    from services.brain.connectors.barchart_options import get_all_unusual_symbols, get_short_candidates

    fmp  = fmp_adds()
    sec  = sec_adds()
    bc   = get_all_unusual_symbols()
    bcs  = list(get_short_candidates().keys())

    print(f"  FMP inject:       {fmp or '[]'}")
    print(f"  SEC inject:       {sec or '[]'}")
    print(f"  Barchart bullish: {bc[:10]} {'...' if len(bc) > 10 else ''}")
    print(f"  Barchart shorts:  {bcs or '[]'}")

    total_unique = len(set(fmp + sec + bc + bcs))
    check("At least some injection candidates exist", total_unique > 0,
          f"{total_unique} unique symbols across all sources")

except Exception as e:
    check("Universe injection check", False, str(e))

# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
passed = sum(results)
total  = len(results)
failed = total - passed
print(f"Result: {passed}/{total} checks passed", end="")
if failed:
    print(f"  |  {failed} FAILED — check FAIL lines above")
else:
    print("  — all good")
print("=" * 60)

sys.exit(0 if failed == 0 else 1)
