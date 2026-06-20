"""
Full end-to-end pipeline test — no stubs, real LLM call, no real trades.

Pipeline:
  1. Universe        → tradeable symbols
  2. Snapshot        → bars + live quotes (Alpaca)
  3. Regime          → bull/chop/bear (SPY MAs + VIX + breadth)
  4. RS ranking      → relative strength vs SPY
  5. News            → Alpaca headlines + sentiment map
  6. Signal weights  → load adaptive weights from DB
  7. Score           → full scoring with REAL connectors (no stubs)
  8. LLM decision    → Claude evaluates top candidates
  9. Summary         → full chain visible, no trade placed

Run:  railway run python3 backend/scripts/test_e2e.py
"""
import os, sys, time, logging, warnings
warnings.filterwarnings("ignore")

# Silence internal noise, keep our own output clean
for _log in ("yfinance", "urllib3", "httpx", "hpack", "alpaca", "services.db"):
    logging.getLogger(_log).setLevel(logging.CRITICAL)
logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")

import contextlib
_devnull = open(os.devnull, "w")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _header(title):
    print(f"\n{'='*65}")
    print(f"  {title}")
    print(f"{'='*65}")

def _step(n, label):
    print(f"\n[{n}] {label}...")

def _ok(msg):   print(f"    ✓  {msg}")
def _warn(msg): print(f"    ⚠  {msg}")
def _fail(msg): print(f"    ✗  {msg}")

_header("KOVA END-TO-END PIPELINE TEST — real data, real LLM, no trades")

# ── 1. Universe ───────────────────────────────────────────────────────────────
_step(1, "Universe")
t0 = time.time()
from services.alpaca_service import get_tradeable_universe
universe = get_tradeable_universe()
_ok(f"{len(universe)} symbols  ({time.time()-t0:.1f}s)")

# ── 2. Snapshot ───────────────────────────────────────────────────────────────
_step(2, "Market snapshot (bars + live quotes)")
t0 = time.time()
from services.alpaca_service import get_market_snapshot_light
symbols_with_spy = list(set(universe) | {"SPY"})
snapshot = get_market_snapshot_light(symbols_with_spy)
live = sum(1 for s in universe if snapshot.get(s, {}).get("current_price"))
missing = len(universe) - live
_ok(f"{live}/{len(universe)} symbols have live price  ({time.time()-t0:.1f}s)")
if missing:
    _warn(f"{missing} symbols missing price — will be skipped in scoring")

# Sample volume signal (was broken before fix)
_sample_syms = [s for s in ["AAPL","NVDA","TSLA","SPY"] if s in snapshot]
if _sample_syms:
    s = _sample_syms[0]
    d = snapshot[s]
    rv = d.get("relative_volume", "n/a")
    av = d.get("avg_volume", 0)
    vol = d.get("volume", 0)
    print(f"         Volume check ({s}): raw_vol={vol:,}  avg_vol={av:,}  rel_vol={rv}")
    if rv == 1.0 and vol == 0:
        _warn("relative_volume is forced 1.0 — bars fix may not be deployed yet")
    else:
        _ok(f"Volume signal is LIVE (rel_vol={rv})")

# ── 3. Regime ─────────────────────────────────────────────────────────────────
_step(3, "Regime detection")
spy_prices = (snapshot.get("SPY") or {}).get("closing_prices", [])
from services.brain.regime import detect_regime
regime = detect_regime(spy_prices, None, snapshot)
_ok(f"Regime={regime.regime.upper()}  confidence={regime.confidence:.0%}  "
    f"vix_level={regime.vix_level}  breadth={regime.breadth_pct:.0f}%")
if regime.notes:
    print(f"         Notes: {regime.notes}")

# ── 4. RS ranking ─────────────────────────────────────────────────────────────
_step(4, "Relative strength ranking")
from services.brain import rank_universe, get_rs_map
rs_ranks = rank_universe(snapshot, spy_prices)
rs_map = get_rs_map(rs_ranks)
_ok(f"{len(rs_map)} symbols ranked")
top5_rs = sorted(rs_map.items(), key=lambda x: -(x[1] or 0))[:5]
print(f"         Top 5 RS: {', '.join(f'{s}({v:.0f}th)' for s,v in top5_rs)}")

# ── 5. News + sentiment ───────────────────────────────────────────────────────
_step(5, "News + sentiment")
news_headlines = []
sentiment = {}
try:
    from services.alpaca_service import get_news
    articles = get_news(limit=50)
    for a in articles:
        _g = lambda f: a.get(f) if isinstance(a, dict) else getattr(a, f, None)
        hl = _g("headline") or ""
        syms = (a.get("symbols") if isinstance(a, dict) else getattr(a, "symbols", None)) or []
        if hl:
            news_headlines.append(hl)
        for s in syms:
            sentiment[s] = sentiment.get(s, 0) + 1
    _ok(f"{len(news_headlines)} headlines fetched, {len(sentiment)} symbols mentioned")
    # Show top news mentions
    top_news = sorted(sentiment.items(), key=lambda x: -x[1])[:5]
    if top_news:
        print(f"         Hot symbols: {', '.join(f'{s}(x{n})' for s,n in top_news)}")
    # Show sample headlines
    for hl in news_headlines[:3]:
        print(f"         • {hl[:80]}")
except Exception as e:
    _warn(f"News fetch failed: {e}")

# ── 6. Signal weights ─────────────────────────────────────────────────────────
_step(6, "Adaptive signal weights (from DB)")
try:
    from services.db import get_signal_weights
    sig_weights = get_signal_weights()
    if sig_weights:
        _ok(f"Loaded {len(sig_weights)} weights from DB")
        for k, v in sorted(sig_weights.items()):
            print(f"         {k:<32} = {v}")
    else:
        _warn("DB unavailable or empty — using connector defaults")
        sig_weights = {}
except Exception as e:
    _warn(f"Weight load failed: {e}")
    sig_weights = {}

# ── 7. Scoring (REAL connectors) ──────────────────────────────────────────────
_step(7, "Scoring universe — REAL connectors (may be slow for SEC/Finnhub)")
print("    (Connectors are live — SEC/Finnhub/Barchart will make real API calls)")
sys.stderr = _devnull
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
elapsed = time.time() - t0

above_threshold = [c for c in all_candidates if c.score >= 60]
near_miss       = [c for c in all_candidates if 50 <= c.score < 60]
_ok(f"Scored {len(all_candidates)} candidates in {elapsed:.0f}s")
_ok(f"≥60 (would trade): {len(above_threshold)}  |  50-59 (near-miss): {len(near_miss)}")

# ── 8. Score breakdown ────────────────────────────────────────────────────────
def _macd_arrow(h):
    if h is None: return "n/a"
    return f"{'▲' if h>0 else '▼'}{abs(h):.3f}"

print(f"\n{'─'*65}")
print("  CANDIDATES ≥60\n")
if not above_threshold:
    print("  None scored ≥60 today — check regime and signal conditions.")
for c in above_threshold:
    d = snapshot.get(c.symbol, {})
    ma50  = d.get("ma50")
    ma200 = d.get("ma200")
    px    = c.price
    print(f"  [{c.score:>3}] {c.symbol:<6}  ${px:<8.2f}  {c.suggested_action.upper():<6}"
          f"  RSI={c.rsi:.0f if c.rsi else 'n/a':<5}  MACD={_macd_arrow(c.macd_hist):<10}"
          f"  RS={c.rs_percentile:.0f if c.rs_percentile else '?'}th"
          f"  rv={c.rel_volume:.1f}x")
    bd = {k: v for k, v in (c.score_breakdown or {}).items() if v != 0}
    if bd:
        parts = "  ".join(f"{k}={v:+d}" for k, v in sorted(bd.items(), key=lambda x: -abs(x[1])))
        print(f"         breakdown: {parts}")
    if c.notes:
        print(f"         notes    : {c.notes}")
    print()

if near_miss:
    print(f"{'─'*65}")
    print("  NEAR-MISSES 50–59\n")
    for c in near_miss:
        print(f"  [{c.score:>3}] {c.symbol:<6}  ${c.price:<8.2f}  {c.suggested_action.upper()}")
    print()

# ── 9. LLM decision ───────────────────────────────────────────────────────────
_header("LLM DECISION (Claude evaluating top candidates)")

if not above_threshold:
    print("  Skipping LLM call — no candidates ≥60.")
    print("  This is what the live bot would do: hold, no trade.")
else:
    print(f"  Sending top {min(3, len(above_threshold))} candidates to Claude...\n")

    # Minimal real-account context for the test
    try:
        from services.alpaca_service import get_account
        acct = get_account()
        cash = float(acct.cash) if acct else 25000.0
        portfolio = float(acct.portfolio_value) if acct else 25000.0
    except Exception:
        cash, portfolio = 25000.0, 25000.0

    # Use Haiku for the test call — same logic, fraction of the cost
    import services.ai_client as _ai_client
    _ai_client.ask_ai_pro = _ai_client.ask_ai

    from services.brain.ai_brain import decide
    from services.db import get_recent_trade_outcomes

    kelly_history = []
    try:
        kelly_history = get_recent_trade_outcomes(limit=30) or []
    except Exception:
        pass

    strategy = {
        "max_position_pct": 0.15,
        "stop_loss_pct": 0.05,
        "take_profit_pct": 0.12,
    }

    t0 = time.time()
    decisions = decide(
        scored_candidates=above_threshold[:3],
        positions=[],
        account_cash=cash,
        portfolio_value=portfolio,
        regime_result=regime,
        rs_map=rs_map,
        kelly_history=kelly_history,
        strategy=strategy,
        news_headlines=news_headlines,
    )
    llm_elapsed = time.time() - t0
    _ok(f"LLM responded in {llm_elapsed:.1f}s")

    print()
    for i, d in enumerate(decisions, 1):
        print(f"  Decision {i}: {d.action.upper()} {d.symbol or ''}")
        if d.quantity:
            print(f"    quantity  : {d.quantity}")
        if hasattr(d, "limit_price") and d.limit_price:
            print(f"    limit_px  : ${d.limit_price:.2f}")
        if hasattr(d, "stop_loss") and d.stop_loss:
            print(f"    stop_loss : ${d.stop_loss:.2f}")
        if hasattr(d, "take_profit") and d.take_profit:
            print(f"    take_profit: ${d.take_profit:.2f}")
        if d.reasoning:
            # Print reasoning wrapped at 70 chars
            words = d.reasoning.split()
            line = "    reasoning : "
            for w in words:
                if len(line) + len(w) > 72:
                    print(line)
                    line = "               " + w + " "
                else:
                    line += w + " "
            if line.strip():
                print(line)
        print()

# ── 10. Pipeline health summary ───────────────────────────────────────────────
_header("PIPELINE HEALTH SUMMARY")

checks = {
    "Universe populated":       len(universe) > 100,
    "Snapshot >90% live":       live / max(len(universe), 1) > 0.9,
    "Regime detected":          regime.regime in ("bull", "chop", "bear"),
    "VIX connected":            regime.vix_level not in ("unknown", None),
    "RS map populated":         len(rs_map) > 50,
    "News flowing":             len(news_headlines) > 0,
    "Signal weights loaded":    len(sig_weights) > 0,
    "Candidates scored":        len(all_candidates) > 0,
    "LLM responded":            above_threshold == [] or len(decisions) > 0,
}

all_passed = True
for label, passed in checks.items():
    status = "PASS" if passed else "FAIL"
    icon   = "✓" if passed else "✗"
    print(f"  {icon}  {label:<35} {status}")
    if not passed:
        all_passed = False

print()
print(f"  Overall: {'ALL SYSTEMS GO ✓' if all_passed else 'ISSUES FOUND — see above'}")
print()
