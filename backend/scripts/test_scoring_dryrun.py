"""
Dry-run scoring against today's real universe.
Shows all candidates that score ≥60, with full signal breakdown.

Run locally:  python3 backend/scripts/test_scoring_dryrun.py
Run on prod:  railway run python3 backend/scripts/test_scoring_dryrun.py
"""
import os, sys, time, logging, warnings

# ── Silence noisy local output ────────────────────────────────────────────────
warnings.filterwarnings("ignore")
# db.py logs "Postgres unavailable" at WARNING — silence it locally
logging.getLogger("services.db").setLevel(logging.CRITICAL)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("urllib3").setLevel(logging.CRITICAL)
logging.getLogger("httpx").setLevel(logging.CRITICAL)
# yfinance also prints HTTP errors directly to stderr; redirect to /dev/null
import contextlib, io
_devnull = open(os.devnull, "w")
sys.stderr = _devnull

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Stub slow external connectors for local dry-run ───────────────────────────
# SEC EDGAR, Finnhub, Barchart each make one HTTP call per symbol (~291 calls).
# These supplementary signals (+8/+15 pts) are not the focus of this dry-run.
# On Railway the real calls fire (cached after first cycle).
_STUB = {"signal": "unavailable", "conviction_boost": 0, "details": "local dry-run stub"}
_STUB_SHORT: list = []

try:
    import services.brain.connectors.sec_insider as _sec
    _sec.get_insider_signal = lambda s: _STUB
except Exception:
    pass

try:
    import services.brain.connectors.finnhub as _fh
    _fh.get_recommendation_signal = lambda s: _STUB
except Exception:
    pass

try:
    import services.brain.connectors.barchart_options as _bc
    _bc.get_short_candidates = lambda: _STUB_SHORT
    _bc.get_options_flow = lambda s: _STUB
except Exception:
    pass

try:
    import services.brain.connectors.fmp_earnings as _fmp
    _fmp.get_earnings_signal = lambda s: _STUB
except Exception:
    pass

try:
    import services.brain.connectors.unusual_whales as _uw
    _uw.get_options_flow = lambda s: _STUB
except Exception:
    pass

sys.stderr = sys.__stderr__  # restore stderr for our own print output

print("=" * 70)
print("SCORING DRY-RUN — today's universe vs min_score=60")
print("(Connectors stubbed for speed: insider/finnhub/barchart/FMP/UW)")
print("=" * 70)

# ── 1. Universe ───────────────────────────────────────────────────────────────
print("\n[1] Fetching universe...")
t0 = time.time()
from services.alpaca_service import get_tradeable_universe, get_market_snapshot_light
universe = get_tradeable_universe()
print(f"    {len(universe)} symbols  ({time.time()-t0:.1f}s)")

# ── 2. Snapshot (bars + live quotes, SPY included) ────────────────────────────
print("\n[2] Fetching snapshot...")
t0 = time.time()
symbols_with_spy = list(set(universe) | {"SPY"})
snapshot = get_market_snapshot_light(symbols_with_spy)
live = sum(1 for s in universe if snapshot.get(s, {}).get("current_price"))
print(f"    {live}/{len(universe)} symbols have live price  ({time.time()-t0:.1f}s)")

# ── 3. SPY prices for regime ──────────────────────────────────────────────────
spy_prices = (snapshot.get("SPY") or {}).get("closing_prices", [])

# ── 4. Regime ─────────────────────────────────────────────────────────────────
print("\n[3] Detecting regime...")
from services.brain.regime import detect_regime
regime = detect_regime(spy_prices, None, snapshot)
print(f"    Regime: {regime.regime.upper()}  confidence={regime.confidence:.0%}"
      f"  vix_level={regime.vix_level}")
if regime.notes:
    print(f"    Notes : {regime.notes}")

# ── 5. RS map ─────────────────────────────────────────────────────────────────
print("\n[4] Computing RS ranks...")
from services.brain import rank_universe, get_rs_map
rs_ranks = rank_universe(snapshot, spy_prices)
rs_map   = get_rs_map(rs_ranks)
print(f"    RS map: {len(rs_map)} symbols ranked")

# ── 6. News + sentiment ───────────────────────────────────────────────────────
print("\n[5] Fetching news + sentiment...")
news_headlines: list = []
sentiment: dict = {}
try:
    from services.alpaca_service import get_news
    articles = get_news(limit=30)
    for a in articles:
        _g   = (lambda f: a.get(f) if isinstance(a, dict) else getattr(a, f, None))
        hl   = _g("headline") or ""
        syms = (a.get("symbols") if isinstance(a, dict) else getattr(a, "symbols", None)) or []
        if hl:
            news_headlines.append(hl)
        for s in syms:
            sentiment[s] = sentiment.get(s, 0) + 1
    print(f"    {len(news_headlines)} headlines, {len(sentiment)} symbols with news")
except Exception as e:
    print(f"    News fetch skipped: {e}")

# ── 7. Score ──────────────────────────────────────────────────────────────────
print("\n[6] Scoring universe (min_score=50 to also see near-misses)...")
sys.stderr = _devnull  # silence yfinance stderr again during scoring
t0 = time.time()
from services.brain.signals import score_universe
all_candidates = score_universe(
    universe_snapshot=snapshot,
    regime_result=regime,
    rs_map=rs_map,
    sentiment=sentiment,
    news_headlines=news_headlines,
    top_n=30,
    min_score=50,
)
sys.stderr = sys.__stderr__
above_60  = [c for c in all_candidates if c.score >= 60]
near_miss = [c for c in all_candidates if 50 <= c.score < 60]
print(f"    Scored {len(all_candidates)} candidates in {time.time()-t0:.1f}s")
print(f"    ≥60 (would trade) : {len(above_60)}")
print(f"    50-59 (near-miss) : {len(near_miss)}")

# ── 8. Print results ─────────────────────────────────────────────────────────
def _macd_str(h):
    if h is None:
        return "n/a"
    return f"{'▲' if h > 0 else '▼'}{abs(h):.3f}"

if not above_60:
    print("\n  No candidates scored ≥60 today.")
else:
    print(f"\n{'─'*70}")
    print(f"  CANDIDATES ≥60 (would be sent to Claude brain)\n")
    for c in above_60:
        rsi_str  = f"{c.rsi:.0f}" if c.rsi is not None else "n/a"
        macd_str = _macd_str(c.macd_hist)
        rs_pct   = f"{c.rs_percentile:.0f}th" if c.rs_percentile else "n/a"

        d         = snapshot.get(c.symbol, {})
        ma50      = d.get("ma50")
        ma200     = d.get("ma200")
        px        = c.price
        ma50_tag  = f">{ma50:.0f}" if ma50 and px > ma50 else (f"<{ma50:.0f}" if ma50 else "")
        ma200_tag = f">{ma200:.0f}" if ma200 and px > ma200 else (f"<{ma200:.0f}" if ma200 else "")

        print(f"  [{c.score:>3}] {c.symbol:<6}  ${px:<8.2f}  {c.suggested_action.upper():<6}"
              f"  RSI={rsi_str:<5}  MACD={macd_str:<10}"
              f"  RS={rs_pct}  MA50={ma50_tag}  MA200={ma200_tag}")

        breakdown = {k: v for k, v in (c.score_breakdown or {}).items() if v != 0}
        if breakdown:
            parts = "  ".join(
                f"{k}={v:+d}" for k, v in sorted(breakdown.items(), key=lambda x: -abs(x[1]))
            )
            print(f"         breakdown: {parts}")
        if c.notes:
            print(f"         notes    : {c.notes}")
        print()

if near_miss:
    print(f"{'─'*70}")
    print(f"  NEAR-MISSES 50–59 (tracked but not traded)\n")
    for c in near_miss:
        rsi_str = f"{c.rsi:.0f}" if c.rsi is not None else "n/a"
        print(f"  [{c.score:>3}] {c.symbol:<6}  ${c.price:<8.2f}  {c.suggested_action.upper():<6}  RSI={rsi_str}")
    print()

print("=" * 70)
print(f"  Regime: {regime.regime.upper()} | VIX: {regime.vix_level} | SPY bars: {len(spy_prices)}")
print("  NOTE: insider/finnhub/barchart/FMP/UW scores are 0 in this dry-run.")
print("  Run via 'railway run' to include live connector signals.")
print("=" * 70)
