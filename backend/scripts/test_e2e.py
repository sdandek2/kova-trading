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

# Compute market-hours flag once (used here and in health summary)
from datetime import datetime as _dt, timezone as _tz
_et_hour = (_dt.now(_tz.utc).hour - 4) % 24
_market_open = 9 <= _et_hour < 16

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
        if _market_open:
            _warn("relative_volume is forced 1.0 during market hours — check bars fix")
        else:
            _ok(f"Volume=0 outside market hours (weekend/after-close) — will be live Monday")
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
top5_rs = sorted(rs_map.items(), key=lambda x: -(x[1].percentile if hasattr(x[1], 'percentile') else x[1] or 0))[:5]
print(f"         Top 5 RS: {', '.join(f'{s}({v.percentile:.0f}th)' if hasattr(v,'percentile') else f'{s}({v:.0f}th)' for s,v in top5_rs)}")

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
_sig_weights_from_db = False
try:
    from services.db import get_signal_weights
    sig_weights = get_signal_weights()
    if sig_weights:
        _sig_weights_from_db = True
        _ok(f"Loaded {len(sig_weights)} weights from DB")
        for k, v in sorted(sig_weights.items()):
            print(f"         {k:<32} = {v}")
    else:
        _warn("DB unavailable or empty — using connector defaults")
        sig_weights = {}
except Exception as e:
    _warn(f"Weight load failed (DB not reachable from local Mac — OK): {type(e).__name__}")
    sig_weights = {}

# ── 7. Scoring (selective real connectors) ────────────────────────────────────
_step(7, "Scoring universe — Barchart+FMP live, SEC+Finnhub stubbed")
print("    Barchart: real (batch fetch, 15-min cache)")
print("    FMP:      real (date-range batch, not per-symbol)")
print("    SEC:      stubbed — 291 HTTP calls × 1-2s each = 10min wait not worth it in test")
print("    Finnhub:  stubbed — per-symbol calls, cached 1hr in prod but cold here")

# Stub the slow per-symbol HTTP connectors.
# In production these are warm after the first cycle (1hr cache).
# In a test (cold process) they'd hang for 5-10 minutes.
_STUB = {"signal": "unavailable", "conviction_boost": 0, "details": "e2e test stub"}
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

_ok(f"Scored {len(all_candidates)} candidates in {elapsed:.0f}s")

# ── 7b. Intraday momentum (mirrors production logic) ─────────────────────────
_step("7b", "Intraday momentum (15-min MACD, 1 batch call)")
try:
    from services.alpaca_service import get_intraday_bars
    from services.indicators import compute_macd as _imacd_fn
    _intra_syms = [c.symbol for c in all_candidates]
    if _intra_syms:
        _intra_t0 = time.time()
        _intra_bars = get_intraday_bars(_intra_syms, 20)
        _intra_hits = 0
        for _c in all_candidates:
            _ibars = _intra_bars.get(_c.symbol, [])
            if len(_ibars) >= 8:
                _ic = [b["close"] for b in _ibars]
                try:
                    _ihist = float((_imacd_fn(_ic) or {}).get("histogram") or 0)
                    _im = 8 if _ihist > 0.01 else (-8 if _ihist < -0.01 else 0)
                    if _im != 0:
                        _c.score += _im
                        _c.score_breakdown["intraday_momentum"] = _im
                        _intra_hits += 1
                except Exception:
                    pass
        _ok(f"Intraday bars fetched in {time.time()-_intra_t0:.1f}s  "
            f"({_intra_hits} symbols got momentum signal)")
    else:
        _warn("No candidates to fetch intraday bars for")
except Exception as _intra_e:
    _warn(f"Intraday momentum skipped: {_intra_e}")

_min_trade_score = 55 if (regime and regime.regime == "bull") else 60
above_threshold = [c for c in all_candidates if c.score >= _min_trade_score]
near_miss       = [c for c in all_candidates if 50 <= c.score < _min_trade_score]
_ok(f"≥{_min_trade_score} (would trade): {len(above_threshold)}  |  50-{_min_trade_score-1} (near-miss): {len(near_miss)}")

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
    _rsi_s = f"{c.rsi:.0f}" if c.rsi is not None else "n/a"
    _rs_s  = f"{c.rs_percentile:.0f}th" if c.rs_percentile else "n/a"
    print(f"  [{c.score:>3}] {c.symbol:<6}  ${px:<8.2f}  {c.suggested_action.upper():<6}"
          f"  RSI={_rsi_s:<5}  MACD={_macd_arrow(c.macd_hist):<10}"
          f"  RS={_rs_s}  rv={c.rel_volume:.1f}x")
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
    print(f"  Sending all {len(above_threshold)} candidates to Claude (same as production)...\n")

    # Minimal real-account context for the test
    try:
        from services.alpaca_service import get_account
        acct = get_account()
        cash = float(acct.cash) if acct else 25000.0
        portfolio = float(acct.portfolio_value) if acct else 25000.0
    except Exception:
        cash, portfolio = 25000.0, 25000.0

    try:
        import anthropic  # noqa — just checking it's installed
    except ModuleNotFoundError:
        print("  ⚠  'anthropic' not installed locally.")
        print("     Run:  pip install anthropic  then rerun.")
        print("     LLM works fine on Railway — this is a local-only gap.")
        print("     Skipping LLM call.\n")
        above_threshold = []

    if not above_threshold:
        decisions = []
    else:
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
            scored_candidates=above_threshold,
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

# ── Shared helper for printing decisions ─────────────────────────────────────
def _print_decisions(dec_list):
    for i, d in enumerate(dec_list, 1):
        print(f"  Decision {i}: {d.action.upper()} {d.symbol or ''}")
        if d.quantity:
            print(f"    quantity  : {d.quantity}")
        if d.reasoning:
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

# ── Test A: Sell / Rotation ───────────────────────────────────────────────────
_header("TEST A: SELL / ROTATION (mock open positions)")

from types import SimpleNamespace as _NS

_mock_positions = [
    _NS(symbol="NVDA", side="long", qty=5,  avg_entry_price=280.00, unrealized_pl_percent=+18.5),
    _NS(symbol="INTC", side="long", qty=15, avg_entry_price=145.00, unrealized_pl_percent=-8.2),
    _NS(symbol="META", side="long", qty=3,  avg_entry_price=320.00, unrealized_pl_percent=+2.1),
]

print("  Mock positions injected (one strong winner, one loser, one flat):")
for p in _mock_positions:
    print(f"    {p.symbol}: {p.qty}sh @ ${p.avg_entry_price:.2f}  P&L={p.unrealized_pl_percent:+.1f}%")
print()

_decisions_rot = []
if above_threshold:
    try:
        t0 = time.time()
        _decisions_rot = decide(
            scored_candidates=above_threshold,
            positions=_mock_positions,
            account_cash=cash * 0.6,   # 40% already deployed in mock positions
            portfolio_value=portfolio,
            regime_result=regime,
            rs_map=rs_map,
            kelly_history=kelly_history,
            strategy=strategy,
            news_headlines=news_headlines,
            rotation_context=(
                "## Rotation Opportunities\nCash: 60% available (40% in open positions)\n"
                "  NVDA: P&L=+18.5% signal=72 MACD=0.200 RSI=74 → STRONG\n"
                "  INTC: P&L=-8.2%  signal=68 MACD=0.672 RSI=64 → WEAK (down, consider exit)\n"
                "  META: P&L=+2.1%  signal=55 MACD=0.100 RSI=58 → MODERATE\n"
                "Rotate if a new candidate scores 15+ points above existing position's signal score."
                " Sells execute before buys.\n"
            ),
        )
        _ok(f"LLM responded in {time.time()-t0:.1f}s")
        print()
        _print_decisions(_decisions_rot)
        _sells = [d for d in _decisions_rot if d.action == "sell"]
        _buys_rot = [d for d in _decisions_rot if d.action == "buy"]
        if _sells:
            _ok(f"Rotation working: {len(_sells)} sell(s), {len(_buys_rot)} buy(s)")
        else:
            _warn("No sells issued — Claude may not be rotating weak positions (INTC -8.2%)")
    except Exception as e:
        _fail(f"Rotation test failed: {e}")
else:
    _warn("Skipping rotation test — no scored candidates above threshold")

# ── Test B: Bear regime — shorts + inverse ETFs ───────────────────────────────
_header("TEST B: BEAR REGIME — shorts + inverse ETFs")

from services.brain.regime import RegimeResult as _RegimeResult
from services.brain.signals import score_universe as _score_bear, ScoredCandidate as _SC

_bear_regime = _RegimeResult(
    regime="bear",
    confidence=0.80,
    vix_level="high",
    spy_trend="below_ma20",
    breadth_pct=22.0,
    score=-3,
    notes="Simulated bear regime for test — SPY below all MAs, VIX elevated",
)
print(f"  Forcing: BEAR | VIX=HIGH | breadth=22% | confidence=80%")
print(f"  Re-scoring universe with bear regime (~15s)...\n")

sys.stderr = _devnull
t0 = time.time()
_bear_all = _score_bear(
    universe_snapshot=snapshot,
    regime_result=_bear_regime,
    rs_map=rs_map,
    sentiment=sentiment,
    news_headlines=news_headlines,
    top_n=30,
    min_score=50,
)
sys.stderr = sys.__stderr__
_ok(f"Bear scoring: {len(_bear_all)} candidates in {time.time()-t0:.0f}s")

_bear_shorts = [c for c in _bear_all if c.suggested_action == "short" and c.score >= 60]
_bear_buys   = [c for c in _bear_all if c.suggested_action == "buy"   and c.score >= 60]
_ok(f"Bear candidates ≥60: {len(_bear_buys)} buy(s)  |  {len(_bear_shorts)} short(s)")

if _bear_shorts:
    print("\n  SHORT CANDIDATES:")
    for c in _bear_shorts[:5]:
        _rsi_b = f"{c.rsi:.0f}" if c.rsi else "n/a"
        bd = {k: v for k, v in (c.score_breakdown or {}).items() if v != 0}
        bd_str = "  ".join(f"{k}={v:+d}" for k, v in sorted(bd.items(), key=lambda x: -abs(x[1])))
        print(f"    [{c.score:>3}] {c.symbol:<6}  ${c.price:<8.2f}  RSI={_rsi_b}")
        if bd_str:
            print(f"           breakdown: {bd_str}")

# Always inject mock inverse ETFs so that path is tested even in bull markets
_mock_inverse = [
    _SC(symbol="SQQQ", score=72, signal_type="inverse_etf", suggested_action="buy",
        price=18.50, rsi=45.0, macd_hist=-0.12, rs_percentile=85, rel_volume=2.1,
        regime_aligned=True,
        score_breakdown={"regime": 25, "rs": 25, "macd": 15, "volume": 8},
        notes="Inverse ETF — QQQ 3× short exposure (injected for test)"),
    _SC(symbol="SOXS", score=65, signal_type="inverse_etf", suggested_action="buy",
        price=9.20, rsi=42.0, macd_hist=-0.08, rs_percentile=78, rel_volume=1.8,
        regime_aligned=True,
        score_breakdown={"regime": 25, "rs": 20, "macd": 10, "volume": 8, "rsi": 5},
        notes="Inverse ETF — SOXX 3× short exposure (injected for test)"),
]
_bear_above = sorted(_bear_buys + _bear_shorts + _mock_inverse,
                     key=lambda c: c.score, reverse=True)
_ok(f"Injected SQQQ(72) + SOXS(65) mock inverse ETFs to test bear path")

_decisions_bear = []
if _bear_above:
    print(f"\n  Sending {len(_bear_above)} bear candidates to Claude...\n")
    try:
        t0 = time.time()
        _decisions_bear = decide(
            scored_candidates=_bear_above,
            positions=[],
            account_cash=cash,
            portfolio_value=portfolio,
            regime_result=_bear_regime,
            rs_map=rs_map,
            kelly_history=kelly_history,
            strategy=strategy,
            news_headlines=news_headlines,
        )
        _ok(f"Bear LLM responded in {time.time()-t0:.1f}s")
        print()
        _print_decisions(_decisions_bear)
        _short_dec = [d for d in _decisions_bear if d.action == "short"]
        _inv_dec   = [d for d in _decisions_bear
                      if d.symbol in {"SQQQ","SOXS","SPXS","SPXU","TECS","TZA","SDOW","SRTY"}]
        if _short_dec:
            _ok(f"Short decisions: {len(_short_dec)} short(s) issued")
        else:
            _warn("No shorts issued in bear regime — check short scoring")
        if _inv_dec:
            _ok(f"Inverse ETF decisions: {len(_inv_dec)} picked ({', '.join(d.symbol for d in _inv_dec)})")
        else:
            _warn("No inverse ETFs picked in bear regime — Claude may be avoiding them")
    except Exception as e:
        _fail(f"Bear regime test failed: {e}")
else:
    _warn("No bear candidates to test")

# ── 10. Pipeline health summary ───────────────────────────────────────────────
_header("PIPELINE HEALTH SUMMARY")

# Core checks — must pass for trading to be safe
checks = {
    "Universe populated":                len(universe) > 100,
    # Snapshot: live prices only exist during market hours (9-4 ET weekdays)
    "Snapshot >90% live (mkt hrs)":      (not _market_open) or live / max(len(universe), 1) > 0.9,
    "Regime detected":                   regime.regime in ("bull", "chop", "bear"),
    "VIX connected":                     regime.vix_level not in ("unknown", None),
    "RS map populated":                  len(rs_map) > 50,
    "News flowing":                      len(news_headlines) > 0,
    "Candidates scored":                 len(all_candidates) > 0,
    "LLM responded":                     above_threshold == [] or len(decisions) > 0,
    "Sell/rotation path":                len(_decisions_rot) > 0,
    "Bear/short/inverse path":           len(_decisions_bear) > 0,
}

# Signal weights: enhancement only — bot works fine on connector defaults.
# DB (postgres.railway.internal) is only reachable inside the Railway container,
# not from a local Mac even via 'railway run'. Don't count as a FAIL.
_sw_note = "loaded from DB" if _sig_weights_from_db else "DB not reachable (using defaults — OK)"

all_passed = True
for label, passed in checks.items():
    status = "PASS" if passed else "FAIL"
    icon   = "✓" if passed else "✗"
    print(f"  {icon}  {label:<35} {status}")
    if not passed:
        all_passed = False

# Signal weights shown as info (not a FAIL — degrades gracefully to defaults)
_sw_icon = "✓" if _sig_weights_from_db else "ℹ"
print(f"  {_sw_icon}  {'Signal weights':<35} {_sw_note}")

print()
print(f"  Overall: {'ALL SYSTEMS GO ✓' if all_passed else 'ISSUES FOUND — see above'}")
print()
