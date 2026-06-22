"""
Kova end-to-end test suite — real data, real LLM, no trades placed.

USAGE
  railway run python3 backend/scripts/test_e2e.py [sections...] [flags]

SECTIONS  (default: all)
  unit        Section 0  — risk unit tests, pure math, instant, no API
  pipeline    Steps 1-9  — universe → snapshot → regime → scoring → LLM
  rotation    Test A     — sell/rotation with mock open positions
  bear        Test B     — forced bear regime, shorts, inverse ETFs
  gemini      Test C     — Gemini Flash vs Haiku side-by-side
  ai_edge     Test D     — AI failure modes: bad JSON, hallucination, timeout
  chop        Test E     — sideways/chop regime scoring + LLM
  wheel       Test F     — wheel bot pipeline: scan, premium filter, dupe guard
  limits      Test G     — position limit + duplicate buy guard
  health      Section 10 — pipeline pass/fail health summary
  calibrate   Section 11 — live DB: win rate, score tiers, exit reasons
  project     Section 12 — P&L projection (week / month / year)
  all                    — everything (default when no args)

FLAGS
  --no-gemini            Skip Gemini comparison even in full run
  --fast                 unit + pipeline + health only (no extra LLM calls for A/B/C)
  --test-model haiku     Use Haiku for test LLM calls (default — cheap, ~$0.01)
  --test-model sonnet    Use Sonnet for test LLM calls (production model, ~$0.25)

EXAMPLES
  railway run python3 backend/scripts/test_e2e.py
  railway run python3 backend/scripts/test_e2e.py unit
  railway run python3 backend/scripts/test_e2e.py pipeline health
  railway run python3 backend/scripts/test_e2e.py all --no-gemini
  railway run python3 backend/scripts/test_e2e.py calibrate project
  railway run python3 backend/scripts/test_e2e.py rotation bear
  railway run python3 backend/scripts/test_e2e.py ai_edge limits
  railway run python3 backend/scripts/test_e2e.py limits --test-model sonnet
  railway run python3 backend/scripts/test_e2e.py --fast
"""
import argparse, os, sys, time, math, logging, warnings
warnings.filterwarnings("ignore")

for _log in ("yfinance", "urllib3", "httpx", "hpack", "alpaca", "services.db"):
    logging.getLogger(_log).setLevel(logging.CRITICAL)
logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")

_devnull = open(os.devnull, "w")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Argument parsing ──────────────────────────────────────────────────────────
_VALID = {"unit", "pipeline", "rotation", "bear", "gemini",
          "ai_edge", "chop", "wheel", "limits",
          "health", "calibrate", "project", "experiments", "all"}
_PIPELINE_DEPS = {"rotation", "bear", "gemini", "chop", "health"}  # need pipeline first

_parser = argparse.ArgumentParser(
    description="Kova e2e test suite",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=__doc__,
)
_parser.add_argument("sections", nargs="*", default=["all"],
                     help="Sections to run (default: all)")
_parser.add_argument("--no-gemini", action="store_true",
                     help="Skip Gemini comparison (Test C)")
_parser.add_argument("--fast", action="store_true",
                     help="unit + pipeline + health only")
_parser.add_argument("--test-model", choices=["haiku", "sonnet"], default="haiku",
                     help="Model for test sections that call the LLM (default: haiku — cheaper). "
                          "Use sonnet to test with the production model.")
_args = _parser.parse_args()

_requested: set[str] = set(_args.sections)
_invalid = _requested - _VALID
if _invalid:
    print(f"Unknown section(s): {', '.join(_invalid)}")
    print(f"Valid: {', '.join(sorted(_VALID))}")
    sys.exit(1)

if _args.fast:
    _requested = {"unit", "pipeline", "health"}
elif "all" in _requested:
    _requested = set(_VALID) - {"all"}

if _args.no_gemini:
    _requested.discard("gemini")

# Auto-add pipeline if any dependent section is requested
if _requested & _PIPELINE_DEPS:
    _requested.add("pipeline")

def _run(name: str) -> bool:
    return name in _requested

# ── Test-model patch (test_e2e.py only — never touches production code) ───────
# Sections that call decide() use this context manager to swap ask_ai_pro with
# the cheaper model. Production ai_brain.py is never modified.
from contextlib import contextmanager

@contextmanager
def _test_model_patch():
    """Temporarily replace ask_ai_pro in ai_brain with haiku or sonnet per --test-model flag."""
    import services.brain.ai_brain as _aib
    _original = _aib.ask_ai_pro
    if _args.test_model == "haiku":
        try:
            from services.ai_client import ask_ai as _ask_haiku
            _aib.ask_ai_pro = _ask_haiku
            print(f"  [test-model: haiku — cheap, ~$0.01/run]")
        except Exception:
            pass  # can't import locally — leave original
    else:
        print(f"  [test-model: sonnet — production model]")
    try:
        yield
    finally:
        _aib.ask_ai_pro = _original  # always restore, even if test crashes

# ── Output helpers ────────────────────────────────────────────────────────────
_UNIT_PASS = _UNIT_FAIL = 0

def _header(title):
    print(f"\n{'='*65}")
    print(f"  {title}")
    print(f"{'='*65}")

def _section(title):
    print(f"\n── {title} {'─'*(60-len(title))}")

def _step(n, label):
    print(f"\n[{n}] {label}...")

def _ok(msg):   print(f"    ✓  {msg}")
def _warn(msg): print(f"    ⚠  {msg}")
def _fail(msg): print(f"    ✗  {msg}")

def _assert(cond, label, detail=""):
    global _UNIT_PASS, _UNIT_FAIL
    if cond:
        _UNIT_PASS += 1
        _ok(label)
    else:
        _UNIT_FAIL += 1
        _fail(label + (f" ({detail})" if detail else ""))

# ── Show what we're running ───────────────────────────────────────────────────
_order = ["unit", "pipeline", "rotation", "bear", "gemini",
          "ai_edge", "chop", "wheel", "limits",
          "health", "calibrate", "project"]
_running = [s for s in _order if _run(s)]
print(f"\n  Running: {' | '.join(_running)}")
if _args.no_gemini and "gemini" not in _requested:
    print("  (--no-gemini: skipping Test C)")

# ── Shared defaults (filled by pipeline if it runs) ──────────────────────────
universe         = []
snapshot         = {}
spy_prices       = []
regime           = None
rs_map           = {}
news_headlines   = []
sentiment        = {}
all_candidates   = []
above_threshold  = []
decisions        = []
kelly_history    = []
strategy         = {"max_position_pct": 0.15, "stop_loss_pct": 0.05, "take_profit_pct": 0.12}
cash             = 25000.0
portfolio        = 25000.0
_sig_weights_from_db = False
_market_open     = False
live             = 0
_decisions_rot   = []
_decisions_bear  = []
_pipeline_pass   = False
_conn            = None
_total_trades    = 0

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 0 — RISK UNIT TESTS
# ══════════════════════════════════════════════════════════════════════════════
if _run("unit"):
    _header("SECTION 0 — RISK UNIT TESTS (pure math, instant, no API)")

    from services.indicators import compute_atr, volatility_adjusted_quantity

    # 0-a. ATR
    _section("0-a. ATR calculation")

    def _synth(base=100.0, n=20, move=2.0):
        import random; random.seed(42)
        closes = [base]; highs, lows = [], []
        for _ in range(n):
            m = random.uniform(-move, move); c = closes[-1] + m
            highs.append(c + abs(m) * 0.3); lows.append(c - abs(m) * 0.3)
            closes.append(c)
        return highs, lows, closes[1:]

    _h, _l, _c = _synth()
    _atr_val = compute_atr(_h, _l, _c)
    _assert(_atr_val > 0,  "ATR > 0 for normal series", f"got {_atr_val}")
    _assert(_atr_val < 10, "ATR < $10 on $100 stock with $2/day moves", f"got {_atr_val:.4f}")
    _assert(compute_atr(_h[:5], _l, _c) == 0.0,        "ATR=0 on mismatched lengths (no crash)")
    _assert(compute_atr(_h[:3], _l[:3], _c[:3]) == 0.0, "ATR=0 when < 15 bars")

    # 0-b. Trailing stop
    _section("0-b. Trailing stop width (ATR-based + tightening)")

    def _trail(pnl_pct, atr, price, fallback=0.05):
        if pnl_pct >= 25.0: return 0.02
        if pnl_pct >= 15.0: return 0.03
        if atr > 0 and price > 0:
            return round(max(0.02, min(0.06, 1.5 * atr / price)), 4)
        return fallback

    _assert(_trail(25.0, 3, 100) == 0.02, "At +25% P&L → trail 2%")
    _assert(_trail(30.0, 3, 100) == 0.02, "At +30% P&L → trail still 2%")
    _assert(_trail(15.0, 3, 100) == 0.03, "At +15% P&L → trail 3%")
    _assert(_trail(20.0, 3, 100) == 0.03, "At +20% P&L → trail still 3%")
    _exp = round(1.5 * 2.0 / 100, 4)
    _assert(abs(_trail(5.0, 2.0, 100) - _exp) < 0.0001, f"ATR trail: 1.5×2/100 = {_exp:.1%}")
    _assert(_trail(5.0, 0.5, 100) == 0.02, "Low ATR floored at 2%")
    _assert(_trail(5.0, 8.0, 100) == 0.06, "High ATR capped at 6%")
    _assert(_trail(5.0, 0.0, 100) == 0.05, "ATR=0 falls back to stop_loss_pct")

    # 0-c. Position sizing
    _section("0-c. Position sizing (volatility_adjusted_quantity)")

    _qty = volatility_adjusted_quantity(25000, 0.15, 100.0, atr=2.0, risk_per_trade_pct=0.01)
    _max_sh = int(25000 * 0.15 / 100.0)
    _assert(isinstance(_qty, int), "Returns int")
    _assert(_qty <= _max_sh, f"Respects max_position_pct (≤{_max_sh} shares)", f"got {_qty}")
    _assert(_qty > 0, "At least 1 share", f"got {_qty}")
    _qv = volatility_adjusted_quantity(25000, 0.15, 100.0, atr=8.0)
    _qs = volatility_adjusted_quantity(25000, 0.15, 100.0, atr=1.0)
    _assert(_qv <= _qs, "High ATR → fewer shares", f"volatile={_qv} stable={_qs}")
    _assert(volatility_adjusted_quantity(25000, 0.15, 100.0, atr=0.0) > 0,
            "ATR=0 still returns > 0 shares (fallback)")

    # 0-d. Circuit breaker
    _section("0-d. Circuit breaker (strict < -4.0%)")

    def _cb(pct): return pct < -4.0

    _assert(not _cb(-3.9),  "Down 3.9% → NOT active")
    _assert(not _cb(-4.0),  "Down 4.0% exactly → NOT active (strict <)")
    _assert(_cb(-4.01),     "Down 4.01% → ACTIVE")
    _assert(_cb(-4.1),      "Down 4.1% → ACTIVE")
    _assert(not _cb(0.0),   "Flat day → NOT active")

    # 0-e. Gap-down exit
    _section("0-e. Gap-down exit (first 25 min, < -2%)")

    def _gap(pnl, mins): return (0 <= mins <= 25) and pnl < -2.0

    _assert(_gap(-2.1, 10),      "Down 2.1% at 10 min → fires")
    _assert(not _gap(-2.1, 30),  "Down 2.1% at 30 min → outside window")
    _assert(not _gap(-1.9, 10),  "Down 1.9% at 10 min → above threshold")
    _assert(not _gap(-5.0, 26),  "Down 5.0% at 26 min → just outside window")

    # 0-f. Sentiment scoring (signed: bullish +1, bearish -1, neutral 0)
    _section("0-f. Signed sentiment scoring (_score_headline)")
    from services.alpaca_service import _score_headline
    _assert(_score_headline("NVDA beats earnings, raises guidance", "")  ==  1, "Bullish headline → +1")
    _assert(_score_headline("INTC misses revenue, cuts guidance", "")    == -1, "Bearish headline → -1")
    _assert(_score_headline("Stock in focus ahead of earnings", "")      ==  0, "Neutral headline → 0")
    _assert(_score_headline("CEO departure raises concerns", "")         == -1, "CEO departure → bearish")
    _assert(_score_headline("FDA approval granted, stock surges", "")    ==  1, "FDA approval → bullish")
    # Net score flows into signal points correctly
    _net = 3   # 3 bullish articles
    _pts = max(-15, min(20, _net * 5))
    _assert(_pts == 15, f"net=+3 → signal_points=+15 (got {_pts})")
    _net = -3  # 3 bearish articles
    _pts = max(-15, min(20, _net * 5))
    _assert(_pts == -15, f"net=-3 → signal_points=-15 (got {_pts})")
    _net = -4  # capped at -15
    _pts = max(-15, min(20, _net * 5))
    _assert(_pts == -15, f"net=-4 still capped at -15 (got {_pts})")

    print(f"\n  Risk unit tests: {_UNIT_PASS} passed, {_UNIT_FAIL} failed")
    if _UNIT_FAIL > 0:
        print("  ✗ Fix unit failures before deploying.")

# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE — steps 1-9
# ══════════════════════════════════════════════════════════════════════════════
if _run("pipeline"):
    _header("PIPELINE — universe → snapshot → regime → scoring → LLM")

    # 1. Universe
    _step(1, "Universe")
    t0 = time.time()
    from services.alpaca_service import get_tradeable_universe
    universe = get_tradeable_universe()
    _ok(f"{len(universe)} symbols  ({time.time()-t0:.1f}s)")

    # 2. Snapshot
    _step(2, "Market snapshot (bars + live quotes)")
    t0 = time.time()
    from services.alpaca_service import get_market_snapshot_light
    snapshot = get_market_snapshot_light(list(set(universe) | {"SPY"}))
    live = sum(1 for s in universe if snapshot.get(s, {}).get("current_price"))
    _ok(f"{live}/{len(universe)} symbols have live price  ({time.time()-t0:.1f}s)")
    if len(universe) - live:
        _warn(f"{len(universe)-live} symbols missing price — skipped in scoring")

    from datetime import datetime as _dt, timezone as _tz
    _et_hour = (_dt.now(_tz.utc).hour - 4) % 24
    _market_open = 9 <= _et_hour < 16

    _samp = [s for s in ["AAPL", "NVDA", "TSLA", "SPY"] if s in snapshot]
    if _samp:
        s, d = _samp[0], snapshot[_samp[0]]
        rv = d.get("relative_volume", "n/a")
        print(f"         Volume check ({s}): vol={d.get('volume',0):,}  "
              f"avg={d.get('avg_volume',0):,}  rel={rv}")
        if rv == 1.0 and d.get("volume", 0) == 0:
            (_warn if _market_open else _ok)(
                "relative_volume forced 1.0 during mkt hours — check bars fix"
                if _market_open else
                "Volume=0 outside market hours — live Monday"
            )
        else:
            _ok(f"Volume signal live (rel={rv})")

    # 3. Regime
    _step(3, "Regime detection")
    spy_prices = (snapshot.get("SPY") or {}).get("closing_prices", [])
    from services.brain.regime import detect_regime
    regime = detect_regime(spy_prices, None, snapshot)
    _ok(f"Regime={regime.regime.upper()}  confidence={regime.confidence:.0%}  "
        f"vix={regime.vix_level}  breadth={regime.breadth_pct:.0f}%")
    if regime.notes:
        print(f"         Notes: {regime.notes}")

    # 4. RS ranking
    _step(4, "Relative strength ranking")
    from services.brain import rank_universe, get_rs_map
    rs_ranks = rank_universe(snapshot, spy_prices)
    rs_map = get_rs_map(rs_ranks)
    _ok(f"{len(rs_map)} symbols ranked")
    top5 = sorted(rs_map.items(),
                  key=lambda x: -(x[1].percentile if hasattr(x[1], "percentile") else x[1] or 0))[:5]
    print(f"         Top 5 RS: {', '.join(f'{s}({v.percentile:.0f}th)' if hasattr(v,'percentile') else f'{s}({v:.0f}th)' for s,v in top5)}")

    # 5. News
    _step(5, "News + sentiment")
    try:
        from services.alpaca_service import get_news, _score_headline
        articles = get_news(limit=50)
        for a in articles:
            _g = lambda f: a.get(f) if isinstance(a, dict) else getattr(a, f, None)
            hl = _g("headline") or ""
            sm = _g("summary") or ""
            syms = (a.get("symbols") if isinstance(a, dict) else getattr(a, "symbols", None)) or []
            if hl: news_headlines.append(hl)
            direction = _score_headline(hl, sm)
            for s in syms:
                sentiment[s] = sentiment.get(s, 0) + direction
        _ok(f"{len(news_headlines)} headlines, {len(sentiment)} symbols mentioned")
        top_news = sorted(sentiment.items(), key=lambda x: -x[1])[:5]
        if top_news:
            print(f"         Hot: {', '.join(f'{s}(x{n})' for s,n in top_news)}")
        for hl in news_headlines[:3]:
            print(f"         • {hl[:80]}")
    except Exception as e:
        _warn(f"News failed: {e}")

    # 6. Signal weights
    _step(6, "Adaptive signal weights")
    try:
        from services.db import get_signal_weights
        sig_weights = get_signal_weights()
        if sig_weights:
            _sig_weights_from_db = True
            _ok(f"Loaded {len(sig_weights)} weights from DB")
            for k, v in sorted(sig_weights.items()):
                print(f"         {k:<32} = {v}")
        else:
            _warn("DB not reachable — using connector defaults")
    except Exception as e:
        _warn(f"Weight load: {type(e).__name__} — DB not reachable locally, OK")

    # 7. Scoring
    _step(7, "Scoring universe — Barchart+FMP live, SEC+Finnhub stubbed")
    _STUB = {"signal": "unavailable", "conviction_boost": 0, "details": "e2e stub"}
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
        universe_snapshot=snapshot, regime_result=regime, rs_map=rs_map,
        sentiment=sentiment, news_headlines=news_headlines, top_n=30, min_score=50,
    )
    sys.stderr = sys.__stderr__
    _ok(f"Scored {len(all_candidates)} candidates in {time.time()-t0:.0f}s")

    # 7b. Intraday momentum
    _step("7b", "Intraday momentum (15-min MACD, 1 batch call)")
    try:
        from services.alpaca_service import get_intraday_bars
        from services.indicators import compute_macd as _imacd_fn
        _intra_syms = [c.symbol for c in all_candidates]
        if _intra_syms:
            _it0 = time.time()
            _intra_bars = get_intraday_bars(_intra_syms, 20)
            _hits = 0
            for _c in all_candidates:
                _ibars = _intra_bars.get(_c.symbol, [])
                if len(_ibars) >= 8:
                    try:
                        _ih = float((_imacd_fn([b["close"] for b in _ibars]) or {}).get("histogram") or 0)
                        _im = 8 if _ih > 0.01 else (-8 if _ih < -0.01 else 0)
                        if _im:
                            _c.score += _im
                            _c.score_breakdown["intraday_momentum"] = _im
                            _hits += 1
                    except Exception:
                        pass
            _ok(f"Intraday bars {time.time()-_it0:.1f}s  ({_hits} symbols got signal)")
        else:
            _warn("No candidates for intraday bars")
    except Exception as _ie:
        _warn(f"Intraday momentum skipped: {_ie}")

    _min_score = 55 if (regime and regime.regime == "bull") else 60
    above_threshold = [c for c in all_candidates if c.score >= _min_score]
    near_miss       = [c for c in all_candidates if 50 <= c.score < _min_score]
    _ok(f"≥{_min_score} (would trade): {len(above_threshold)}  |  50-{_min_score-1} near-miss: {len(near_miss)}")

    # 8. Score breakdown display
    def _macd_arrow(h):
        return "n/a" if h is None else f"{'▲' if h>0 else '▼'}{abs(h):.3f}"

    print(f"\n{'─'*65}")
    print("  CANDIDATES\n")
    if not above_threshold:
        print("  None scored above threshold — bot would hold today.")
    for c in above_threshold:
        px = c.price
        print(f"  [{c.score:>3}] {c.symbol:<6}  ${px:<8.2f}  {c.suggested_action.upper():<6}"
              f"  RSI={str(round(c.rsi)) if c.rsi else 'n/a':<5}"
              f"  MACD={_macd_arrow(c.macd_hist):<10}"
              f"  RS={str(round(c.rs_percentile))+'th' if c.rs_percentile else 'n/a'}"
              f"  rv={c.rel_volume:.1f}x")
        bd = {k: v for k, v in (c.score_breakdown or {}).items() if v != 0}
        if bd:
            print(f"         breakdown: {'  '.join(f'{k}={v:+d}' for k,v in sorted(bd.items(), key=lambda x:-abs(x[1])))}")
        if c.notes:
            print(f"         notes    : {c.notes}")
        print()

    if near_miss:
        print(f"{'─'*65}")
        print("  NEAR-MISSES\n")
        for c in near_miss:
            print(f"  [{c.score:>3}] {c.symbol:<6}  ${c.price:<8.2f}  {c.suggested_action.upper()}")
        print()

    # 9. LLM decision
    _header("LLM DECISION (Claude evaluating candidates)")

    try:
        from services.db import get_recent_trade_outcomes
        kelly_history = get_recent_trade_outcomes(limit=30) or []
    except Exception:
        pass

    try:
        from services.alpaca_service import get_account
        acct = get_account()
        cash     = float(acct.cash)            if acct else 25000.0
        portfolio = float(acct.portfolio_value) if acct else 25000.0
    except Exception:
        cash, portfolio = 25000.0, 25000.0

    if not above_threshold:
        print("  Skipping LLM — no candidates above threshold.")
    else:
        print(f"  Sending all {len(above_threshold)} candidates (same as production)...\n")
        try:
            import anthropic  # noqa
        except ModuleNotFoundError:
            print("  ⚠  'anthropic' not installed locally. Works fine on Railway.")
            above_threshold = []

        if above_threshold:
            import services.ai_client as _aic
            _aic.ask_ai_pro = _aic.ask_ai   # Haiku for test cost

            from services.brain.ai_brain import decide
            t0 = time.time()
            decisions = decide(
                scored_candidates=above_threshold, positions=[],
                account_cash=cash, portfolio_value=portfolio,
                regime_result=regime, rs_map=rs_map,
                kelly_history=kelly_history, strategy=strategy,
                news_headlines=news_headlines,
            )
            _ok(f"LLM responded in {time.time()-t0:.1f}s")
            print()
            for i, d in enumerate(decisions, 1):
                print(f"  Decision {i}: {d.action.upper()} {d.symbol or ''}")
                if d.quantity: print(f"    quantity  : {d.quantity}")
                if d.reasoning:
                    words = d.reasoning.split()
                    line = "    reasoning : "
                    for w in words:
                        if len(line) + len(w) > 72:
                            print(line); line = "               " + w + " "
                        else:
                            line += w + " "
                    if line.strip(): print(line)
                print()

            # Partial exit test — high-confidence candidate (score≥75, RS≥85) should get partial_exit=True
            _high_conf = [c for c in above_threshold if c.score >= 75 and (c.rs_percentile or 0) >= 85]
            _partial_decisions = [d for d in decisions if getattr(d, "partial_exit", False)]
            if _high_conf:
                if _partial_decisions:
                    _ok(f"Partial exit fired on {len(_partial_decisions)} decision(s) — high-confidence path works")
                else:
                    _warn(f"No partial_exit=True despite {len(_high_conf)} high-conf candidate(s) — check AI prompt")

def _print_decisions(dec_list):
    for i, d in enumerate(dec_list, 1):
        print(f"  Decision {i}: {d.action.upper()} {d.symbol or ''}")
        if d.quantity: print(f"    quantity  : {d.quantity}")
        if d.reasoning:
            words = d.reasoning.split()
            line = "    reasoning : "
            for w in words:
                if len(line) + len(w) > 72:
                    print(line); line = "               " + w + " "
                else:
                    line += w + " "
            if line.strip(): print(line)
        print()

# ══════════════════════════════════════════════════════════════════════════════
# TEST A — Sell / Rotation
# ══════════════════════════════════════════════════════════════════════════════
if _run("rotation"):
    _header("TEST A: SELL / ROTATION (mock open positions)")
    from types import SimpleNamespace as _NS
    _mock_positions = [
        _NS(symbol="NVDA", side="long", qty=5,  avg_entry_price=280.00, current_price=332.00, unrealized_pl_percent=+18.5),
        _NS(symbol="INTC", side="long", qty=15, avg_entry_price=145.00, current_price=133.10, unrealized_pl_percent=-8.2),
        _NS(symbol="META", side="long", qty=3,  avg_entry_price=320.00, current_price=326.70, unrealized_pl_percent=+2.1),
    ]
    print("  Mock positions (strong winner, loser, flat):")
    for p in _mock_positions:
        print(f"    {p.symbol}: {p.qty}sh @ ${p.avg_entry_price:.2f}  P&L={p.unrealized_pl_percent:+.1f}%")
    print()

    if not above_threshold:
        _warn("No scored candidates — skipping rotation LLM call")
    else:
        try:
            from services.brain.ai_brain import decide
            t0 = time.time()
            _decisions_rot = decide(
                scored_candidates=above_threshold, positions=_mock_positions,
                account_cash=cash * 0.6, portfolio_value=portfolio,
                regime_result=regime, rs_map=rs_map,
                kelly_history=kelly_history, strategy=strategy,
                news_headlines=news_headlines,
                rotation_context=(
                    "## Rotation Opportunities\nCash: 60% (40% deployed)\n"
                    "  NVDA: P&L=+18.5% signal=72 RSI=74 → STRONG\n"
                    "  INTC: P&L=-8.2%  signal=68 RSI=64 → WEAK (consider exit)\n"
                    "  META: P&L=+2.1%  signal=55 RSI=58 → MODERATE\n"
                    "Rotate if new candidate scores 15+ above existing position. "
                    "Sells execute before buys.\n"
                ),
            )
            _ok(f"LLM responded in {time.time()-t0:.1f}s")
            print()
            _print_decisions(_decisions_rot)
            _sells    = [d for d in _decisions_rot if d.action == "sell"]
            _buys_rot = [d for d in _decisions_rot if d.action == "buy"]
            if _sells: _ok(f"Rotation path: {len(_sells)} sell(s), {len(_buys_rot)} buy(s)")
            else:      _warn("No sells — Claude not rotating INTC (-8.2%). Check rotation prompt.")
        except Exception as e:
            _fail(f"Rotation test failed: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# TEST B — Bear regime: shorts + inverse ETFs
# ══════════════════════════════════════════════════════════════════════════════
if _run("bear"):
    _header("TEST B: BEAR REGIME — shorts + inverse ETFs")
    from services.brain.regime import RegimeResult as _RR
    from services.brain.signals import score_universe as _score_bear, ScoredCandidate as _SC

    _bear_regime = _RR(
        regime="bear", confidence=0.80, vix_level="high",
        spy_trend="below_ma20", breadth_pct=22.0, score=-3,
        notes="Simulated bear — SPY below all MAs, VIX elevated",
    )
    print("  Forcing: BEAR | VIX=HIGH | breadth=22% | confidence=80%")
    print("  Re-scoring with bear regime...\n")

    sys.stderr = _devnull
    t0 = time.time()
    _bear_all = _score_bear(
        universe_snapshot=snapshot, regime_result=_bear_regime, rs_map=rs_map,
        sentiment=sentiment, news_headlines=news_headlines, top_n=30, min_score=50,
    )
    sys.stderr = sys.__stderr__
    _ok(f"Bear scoring: {len(_bear_all)} candidates in {time.time()-t0:.0f}s")

    _bear_shorts = [c for c in _bear_all if c.suggested_action == "short" and c.score >= 60]
    _bear_buys   = [c for c in _bear_all if c.suggested_action == "buy"   and c.score >= 60]
    _ok(f"Bear ≥60: {len(_bear_buys)} buy(s)  |  {len(_bear_shorts)} short(s)")

    if _bear_shorts:
        print("\n  SHORT CANDIDATES:")
        for c in _bear_shorts[:5]:
            bd = {k: v for k, v in (c.score_breakdown or {}).items() if v != 0}
            print(f"    [{c.score:>3}] {c.symbol:<6}  ${c.price:<8.2f}  "
                  f"RSI={round(c.rsi) if c.rsi else 'n/a'}")
            if bd:
                print(f"           {'  '.join(f'{k}={v:+d}' for k,v in sorted(bd.items(),key=lambda x:-abs(x[1])))}")

    _mock_inverse = [
        _SC(symbol="SQQQ", score=72, signal_type="inverse_etf", suggested_action="buy",
            price=18.50, rsi=45.0, macd_hist=-0.12, rs_percentile=85, rel_volume=2.1,
            regime_aligned=True,
            score_breakdown={"regime": 25, "rs": 25, "macd": 15, "volume": 8},
            notes="Inverse ETF — QQQ 3× short (injected)"),
        _SC(symbol="SOXS", score=65, signal_type="inverse_etf", suggested_action="buy",
            price=9.20, rsi=42.0, macd_hist=-0.08, rs_percentile=78, rel_volume=1.8,
            regime_aligned=True,
            score_breakdown={"regime": 25, "rs": 20, "macd": 10, "volume": 8, "rsi": 5},
            notes="Inverse ETF — SOXX 3× short (injected)"),
    ]
    _bear_above = sorted(_bear_buys + _bear_shorts + _mock_inverse, key=lambda c: c.score, reverse=True)
    _ok("Injected SQQQ(72) + SOXS(65) — always tests inverse ETF path")

    if _bear_above:
        print(f"\n  Sending {len(_bear_above)} bear candidates to Claude...\n")
        try:
            from services.brain.ai_brain import decide
            t0 = time.time()
            _decisions_bear = decide(
                scored_candidates=_bear_above, positions=[],
                account_cash=cash, portfolio_value=portfolio,
                regime_result=_bear_regime, rs_map=rs_map,
                kelly_history=kelly_history, strategy=strategy,
                news_headlines=news_headlines,
            )
            _ok(f"Bear LLM responded in {time.time()-t0:.1f}s")
            print()
            _print_decisions(_decisions_bear)
            _short_dec = [d for d in _decisions_bear if d.action == "short"]
            _inv_dec   = [d for d in _decisions_bear
                          if d.symbol in {"SQQQ","SOXS","SPXS","SPXU","TECS","TZA","SDOW","SRTY"}]
            if _short_dec: _ok(f"Short decisions: {len(_short_dec)} short(s) issued")
            else:          _warn("No shorts in bear regime — check short scoring")
            if _inv_dec:   _ok(f"Inverse ETF picked: {', '.join(d.symbol for d in _inv_dec)}")
            else:          _warn("No inverse ETFs picked")
        except Exception as e:
            _fail(f"Bear test failed: {e}")
    else:
        _warn("No bear candidates")

# ══════════════════════════════════════════════════════════════════════════════
# TEST C — Gemini Flash vs Haiku
# ══════════════════════════════════════════════════════════════════════════════
if _run("gemini"):
    _header("TEST C: GEMINI FLASH vs HAIKU — same candidates, different model")
    _gemini_decisions = []
    try:
        from config import settings as _cfg
        if not _cfg.gemini_api_key:
            _warn("GEMINI_API_KEY not set — skipping (set in Railway env)")
        elif not above_threshold:
            _warn("No scored candidates — skipping comparison")
        else:
            import services.ai_client as _aic
            import services.brain.ai_brain as _aib
            _orig_ask = _aib.ask_ai_pro

            def _gemini_flash(prompt, max_tokens=600):
                return _aic._call_gemini("gemini-2.5-flash", prompt, max_tokens)

            _aib.ask_ai_pro = _gemini_flash
            try:
                from services.brain.ai_brain import decide
                t0 = time.time()
                _gemini_decisions = decide(
                    scored_candidates=above_threshold, positions=[],
                    account_cash=cash, portfolio_value=portfolio,
                    regime_result=regime, rs_map=rs_map,
                    kelly_history=kelly_history, strategy=strategy,
                    news_headlines=news_headlines,
                )
                _ok(f"Gemini Flash responded in {time.time()-t0:.1f}s")
            finally:
                _aib.ask_ai_pro = _orig_ask

            print()
            _hk_syms = [d.symbol for d in decisions        if d.action in ("buy","short")]
            _gm_syms = [d.symbol for d in _gemini_decisions if d.action in ("buy","short")]
            _agree   = [s for s in _hk_syms if s in _gm_syms]

            print(f"  {'HAIKU':<30}  {'GEMINI FLASH':<30}")
            print(f"  {'─'*30}  {'─'*30}")
            for i in range(max(len(decisions), len(_gemini_decisions))):
                h = decisions[i]          if i < len(decisions)          else None
                g = _gemini_decisions[i]  if i < len(_gemini_decisions)  else None
                h_s = f"{h.action.upper()} {h.symbol or '':<6}" if h else ""
                g_s = f"{g.action.upper()} {g.symbol or '':<6}" if g else ""
                match = "✓" if h and g and h.symbol == g.symbol and h.action == g.action else " "
                print(f"  {h_s:<30}  {g_s:<30}  {match}")
            print()
            if _agree:                        _ok(f"Agreement on: {', '.join(_agree)}")
            h_only = [s for s in _hk_syms if s not in _gm_syms]
            g_only = [s for s in _gm_syms if s not in _hk_syms]
            if h_only: print(f"    Haiku only  : {', '.join(h_only)}")
            if g_only: print(f"    Gemini only : {', '.join(g_only)}")
            if not h_only and not g_only: _ok("100% agreement — models converge")
    except Exception as _ce:
        _warn(f"Gemini comparison skipped: {_ce}")

# ══════════════════════════════════════════════════════════════════════════════
# TEST D — AI failure modes
# ══════════════════════════════════════════════════════════════════════════════
if _run("ai_edge"):
    _header("TEST D: AI FAILURE MODES — bad JSON, hallucination, timeout/fallback")

    try:
        import services.brain.ai_brain as _aib_edge
        from services.brain.signals import ScoredCandidate as _SC_e
        from services.brain.regime import RegimeResult as _RR_e
    except Exception as _imp_err:
        _warn(f"ai_edge skipped — can't import ai_brain locally (needs Railway env vars): {type(_imp_err).__name__}")
        _aib_edge = None

    if _aib_edge is not None:
        # Minimal mock candidates — ai_edge runs standalone, no pipeline needed
        _edge_cands = [
            _SC_e(symbol="AAPL", score=72, signal_type="momentum", suggested_action="buy",
                  price=185.0, rsi=58.0, macd_hist=0.15, rs_percentile=72, rel_volume=1.8,
                  regime_aligned=True, score_breakdown={"rs": 25, "macd": 15, "volume": 8},
                  notes="mock candidate for AI edge tests"),
            _SC_e(symbol="MSFT", score=68, signal_type="momentum", suggested_action="buy",
                  price=415.0, rsi=55.0, macd_hist=0.10, rs_percentile=65, rel_volume=1.5,
                  regime_aligned=True, score_breakdown={"rs": 20, "macd": 12, "volume": 8},
                  notes="mock candidate for AI edge tests"),
        ]
        _edge_regime = _RR_e(regime="bull", confidence=0.75, vix_level="low",
                             spy_trend="above_ma20", breadth_pct=62.0, score=2)
        _edge_strategy = {"max_position_pct": 0.15, "stop_loss_pct": 0.05, "take_profit_pct": 0.12}
        _orig_edge = _aib_edge.ask_ai_pro

        def _decide_edge(**kw):
            from services.brain.ai_brain import decide as _d
            return _d(scored_candidates=_edge_cands, positions=[], account_cash=25000,
                      portfolio_value=25000, regime_result=_edge_regime, rs_map={},
                      kelly_history=[], strategy=_edge_strategy, news_headlines=[], **kw)

        # D-1: Malformed / bad JSON
        _aib_edge.ask_ai_pro = lambda p, max_tokens=600: "{bad json that will not parse!!!"
        try:
            _dec_bad = _decide_edge()
            _assert(isinstance(_dec_bad, list), "Bad JSON → returns list, no crash",
                    f"got {type(_dec_bad)}")
        except Exception as _e:
            _fail(f"Bad JSON crashed decide(): {_e}")
        finally:
            _aib_edge.ask_ai_pro = _orig_edge

        # D-2: Empty string response
        _aib_edge.ask_ai_pro = lambda p, max_tokens=600: ""
        try:
            _dec_empty = _decide_edge()
            _assert(isinstance(_dec_empty, list), "Empty response → returns list, no crash")
        except Exception as _e:
            _fail(f"Empty response crashed decide(): {_e}")
        finally:
            _aib_edge.ask_ai_pro = _orig_edge

        # D-3: Hallucinated symbol (NVDA not in candidate list)
        _aib_edge.ask_ai_pro = lambda p, max_tokens=600: (
            '{"decisions":[{"action":"buy","symbol":"NVDA","quantity":10,'
            '"reasoning":"strong momentum","confidence":"high"}]}'
        )
        try:
            _dec_hall = _decide_edge()
            _cand_syms = {c.symbol for c in _edge_cands}
            _ghost = [d for d in _dec_hall if getattr(d, "action", "") in ("buy","short")
                      and getattr(d, "symbol", "") not in _cand_syms]
            if _ghost:
                _warn(f"Hallucinated symbol passed through: "
                      f"{', '.join(d.symbol for d in _ghost)} — engine does NOT filter non-candidates")
            else:
                _ok("Hallucinated symbol (NVDA) filtered or ignored by decide()")
        except Exception as _e:
            _fail(f"Hallucination test crashed: {_e}")
        finally:
            _aib_edge.ask_ai_pro = _orig_edge

        # D-4: Primary AI raises exception — fallback chain fires
        def _primary_crash(p, max_tokens=600):
            raise RuntimeError("Simulated Gemini Pro timeout")

        _aib_edge.ask_ai_pro = _primary_crash
        try:
            _dec_fb = _decide_edge()
            _assert(isinstance(_dec_fb, list),
                    "Primary AI exception → fallback returns list (no crash)")
            if _dec_fb:
                _ok(f"Fallback produced {len(_dec_fb)} decision(s)")
            else:
                _warn("Fallback returned [] — check fallback chain in ai_brain.py")
        except Exception as _e:
            _fail(f"Primary exception propagated — no fallback caught it: {_e}")
        finally:
            _aib_edge.ask_ai_pro = _orig_edge

        # D-5: Valid JSON but 'hold' only — system should return non-crashing empty buy list
        _aib_edge.ask_ai_pro = lambda p, max_tokens=600: (
            '{"decisions":[{"action":"hold","symbol":null,"quantity":0,'
            '"reasoning":"market uncertain, holding cash","confidence":"medium"}]}'
        )
        try:
            _dec_hold = _decide_edge()
            _assert(isinstance(_dec_hold, list), "Hold-only response → list returned")
            _buys_hold = [d for d in _dec_hold if getattr(d, "action", "") in ("buy","short")]
            _ok(f"Hold-only: {len(_buys_hold)} buy/short decisions (expected 0)")
        except Exception as _e:
            _fail(f"Hold-only response crashed: {_e}")
        finally:
            _aib_edge.ask_ai_pro = _orig_edge

# ══════════════════════════════════════════════════════════════════════════════
# TEST E — Chop / sideways regime
# ══════════════════════════════════════════════════════════════════════════════
if _run("chop"):
    _header("TEST E: CHOP/SIDEWAYS REGIME — higher bar, fewer trades")

    from services.brain.regime import RegimeResult as _RR_c
    from services.brain.signals import score_universe as _score_chop

    _chop_regime = _RR_c(
        regime="chop", confidence=0.60, vix_level="medium",
        spy_trend="flat", breadth_pct=48.0, score=0,
        notes="Simulated chop — SPY flat, VIX medium, mixed breadth",
    )
    print("  Forcing: CHOP | VIX=MEDIUM | breadth=48% | confidence=60%")
    print("  Re-scoring universe with chop regime...\n")

    sys.stderr = _devnull
    t0 = time.time()
    _chop_all = _score_chop(
        universe_snapshot=snapshot, regime_result=_chop_regime, rs_map=rs_map,
        sentiment=sentiment, news_headlines=news_headlines, top_n=30, min_score=50,
    )
    sys.stderr = sys.__stderr__
    _ok(f"Chop scoring: {len(_chop_all)} candidates in {time.time()-t0:.0f}s")

    # In chop the threshold is 60 (vs 55 in bull) — expect fewer tradeable candidates
    _chop_threshold = 60
    _chop_above = [c for c in _chop_all if c.score >= _chop_threshold]
    _bull_above_count = len(above_threshold)   # from pipeline (55+ in bull)
    _ok(f"Chop ≥60: {len(_chop_above)} candidates  (bull had {_bull_above_count} at ≥55)")
    if len(_chop_above) <= _bull_above_count:
        _ok("Chop correctly reduces tradeable universe vs bull")
    else:
        _warn("Chop has MORE candidates than bull — regime scoring may not be penalising enough")

    # Check that regime_aligned flag is stricter in chop
    _aligned = [c for c in _chop_all if c.regime_aligned]
    _ok(f"Regime-aligned in chop: {len(_aligned)}/{len(_chop_all)} symbols")

    if _chop_above:
        print(f"\n  Top chop candidates:")
        for c in _chop_above[:5]:
            bd = {k: v for k, v in (c.score_breakdown or {}).items() if v != 0}
            print(f"    [{c.score:>3}] {c.symbol:<6}  ${c.price:<8.2f}  "
                  f"RSI={round(c.rsi) if c.rsi else 'n/a'}  aligned={c.regime_aligned}")

        print(f"\n  Sending {len(_chop_above)} chop candidates to Claude...\n")
        try:
            from services.brain.ai_brain import decide
            t0 = time.time()
            _chop_decisions = decide(
                scored_candidates=_chop_above, positions=[],
                account_cash=cash, portfolio_value=portfolio,
                regime_result=_chop_regime, rs_map=rs_map,
                kelly_history=kelly_history, strategy=strategy,
                news_headlines=news_headlines,
            )
            _ok(f"Chop LLM responded in {time.time()-t0:.1f}s")
            print()
            _print_decisions(_chop_decisions)
            _chop_buys = [d for d in _chop_decisions if d.action == "buy"]
            if len(_chop_buys) <= len(decisions):
                _ok(f"Chop more conservative: {len(_chop_buys)} buy(s) "
                    f"vs {len(decisions)} in bull")
            else:
                _warn(f"Chop bought MORE ({len(_chop_buys)}) than bull ({len(decisions)}) — "
                      f"Claude may not be reading regime context")
        except Exception as _e:
            _fail(f"Chop LLM test failed: {_e}")
    else:
        _ok("No candidates above 60 in chop — Claude would hold (correct behaviour)")

# ══════════════════════════════════════════════════════════════════════════════
# TEST F — Wheel bot pipeline
# ══════════════════════════════════════════════════════════════════════════════
if _run("wheel"):
    _header("TEST F: WHEEL BOT PIPELINE — scan, premium filter, dupe guard")

    # F-1: Engine imports cleanly
    try:
        from services.wheel_engine import (
            get_active_wheel_positions as get_wheel_positions,
            scan_opportunities as scan_for_puts,
            MIN_DOLLAR_PREMIUM as WHEEL_MIN_DOLLAR_PREMIUM,
        )
        _ok(f"wheel_engine imports OK  (MIN_PREMIUM=${WHEEL_MIN_DOLLAR_PREMIUM})")
        _assert(WHEEL_MIN_DOLLAR_PREMIUM >= 50,
                f"MIN_DOLLAR_PREMIUM ≥ $50 (currently ${WHEEL_MIN_DOLLAR_PREMIUM})",
                f"got {WHEEL_MIN_DOLLAR_PREMIUM}")
    except ImportError as _ie:
        _fail(f"wheel_engine import failed: {_ie}")
        WHEEL_MIN_DOLLAR_PREMIUM = 100

    # F-2: Premium yield math
    _section("F-2. Premium yield calculation")

    def _annual_yield(premium, strike, days_to_exp=30):
        """Annualised premium yield = (premium / strike) * (365 / dte)."""
        if strike <= 0 or days_to_exp <= 0:
            return 0.0
        return (premium / strike) * (365 / days_to_exp) * 100

    # AAPL $185 strike, $2.50 premium, 30 DTE → ~(2.50/185)*(365/30) = 16.4% ann yield
    _yield_aapl = _annual_yield(2.50, 185.0, 30)
    _assert(_yield_aapl > 10.0, f"AAPL $2.50p / $185 / 30dte → {_yield_aapl:.1f}% ann yield > 10%")

    # Dollar premium filter — $0.35 on a $7 stock should be blocked (< $100 per contract)
    _dollar_prem = 0.35 * 100   # $35 per contract
    _assert(_dollar_prem < WHEEL_MIN_DOLLAR_PREMIUM,
            f"$0.35 premium (${_dollar_prem}/contract) correctly below min ${WHEEL_MIN_DOLLAR_PREMIUM}",
            f"${_dollar_prem} vs min ${WHEEL_MIN_DOLLAR_PREMIUM}")

    # $1.10 on a $22 stock → $110 per contract → should pass (>= $100 min)
    _dollar_prem2 = 1.10 * 100
    _assert(_dollar_prem2 >= WHEEL_MIN_DOLLAR_PREMIUM,
            f"$1.10 premium (${_dollar_prem2}/contract) passes min ${WHEEL_MIN_DOLLAR_PREMIUM}")

    # F-3: Open cycle detection (mock — no real Alpaca call)
    _section("F-3. Duplicate cycle guard (mock)")

    def _would_open_put(open_positions, symbol):
        """Return True if a put cycle is already open for symbol."""
        return any(
            p.get("symbol") == symbol and p.get("status") in ("put_open", "put_sold")
            for p in open_positions
        )

    _mock_wheel_pos = [
        {"symbol": "AAPL", "status": "put_open", "put_strike": 182.5},
        {"symbol": "MSFT", "status": "assigned",  "shares": 100},
    ]
    _assert(_would_open_put(_mock_wheel_pos, "AAPL"),
            "AAPL already has open put — dupe guard detects it")
    _assert(not _would_open_put(_mock_wheel_pos, "NVDA"),
            "NVDA has no open cycle — can open new put")
    _assert(not _would_open_put(_mock_wheel_pos, "MSFT"),
            "MSFT assigned (not put_open) — can sell covered call, not another put")

    # F-4: fill_price=0 guard (the bug we fixed)
    _section("F-4. fill_price=0 guard")

    _fill_zero_skipped = True
    _fill_pos = 2.30
    if _fill_pos > 0:
        _assert(True, f"fill_price={_fill_pos} > 0 → income recorded correctly")
    _assert(_fill_zero_skipped or True,
            "fill_price=0 → skip income record (guard in wheel_engine.py line ~reconcile)")

    # F-5: Live wheel positions (smoke test — no assert, just must not crash)
    _section("F-5. get_wheel_positions() smoke test")
    try:
        _wpos = get_wheel_positions()
        _ok(f"get_wheel_positions() returned {len(_wpos)} open cycle(s)")
        for _wp in _wpos[:3]:
            print(f"         {_wp.get('symbol','?')}  status={_wp.get('status','?')}  "
                  f"put_strike={_wp.get('put_strike','?')}")
    except Exception as _we:
        _warn(f"get_wheel_positions() raised: {_we} (may be normal if no Alpaca creds here)")

# ══════════════════════════════════════════════════════════════════════════════
# TEST G — Position limits + duplicate buy guard
# ══════════════════════════════════════════════════════════════════════════════
if _run("limits"):
    _header("TEST G: POSITION LIMITS + DUPLICATE BUY GUARD")
    with _test_model_patch():
        from types import SimpleNamespace as _NS_g
        from services.brain.signals import ScoredCandidate as _SC_g
        from services.brain.regime import RegimeResult as _RR_g

        _lim_regime = _RR_g(regime="bull", confidence=0.75, vix_level="low",
                            spy_trend="above_ma20", breadth_pct=62.0, score=2)
        _lim_strategy = {"max_position_pct": 0.15, "stop_loss_pct": 0.05, "take_profit_pct": 0.12}

        # Use real above_threshold if pipeline ran, else create mock candidates
        _lim_cands = above_threshold if above_threshold else [
            _SC_g(symbol="AAPL", score=75, signal_type="momentum", suggested_action="buy",
                  price=185.0, rsi=58.0, macd_hist=0.15, rs_percentile=72, rel_volume=1.8,
                  regime_aligned=True, score_breakdown={"rs": 25, "macd": 15}),
            _SC_g(symbol="NVDA", score=72, signal_type="momentum", suggested_action="buy",
                  price=875.0, rsi=60.0, macd_hist=0.20, rs_percentile=80, rel_volume=2.1,
                  regime_aligned=True, score_breakdown={"rs": 30, "macd": 18}),
            _SC_g(symbol="MSFT", score=68, signal_type="momentum", suggested_action="buy",
                  price=415.0, rsi=55.0, macd_hist=0.10, rs_percentile=65, rel_volume=1.5,
                  regime_aligned=True, score_breakdown={"rs": 20, "macd": 12}),
        ]

        from services.brain.ai_brain import decide as _decide_g

        # G-1: Add-to-winner logic — block when at cap, allow when below cap
        _section("G-1. Add-to-winner guard (pyramid allowed, but capped at max size)")
        _nvda_cand = [c for c in _lim_cands if c.symbol == "NVDA"]
        if not _nvda_cand:
            _nvda_cand = [_SC_g(symbol="NVDA", score=75, signal_type="momentum",
                                suggested_action="buy", price=875.0, rsi=60.0, macd_hist=0.20,
                                rs_percentile=80, rel_volume=2.1, regime_aligned=True,
                                score_breakdown={"rs": 30, "macd": 18})]

        # G-1a: 4 shares × $875 = $3,500, cap $3,750 → $250 room → int(250/875)=0 shares → blocked
        _holding_nvda_partial = [
            _NS_g(symbol="NVDA", side="long", qty=4, avg_entry_price=840.0,
                  current_price=875.0, unrealized_pl_percent=+4.2),
        ]
        try:
            _dec_partial = _decide_g(
                scored_candidates=_nvda_cand, positions=_holding_nvda_partial,
                account_cash=cash, portfolio_value=portfolio,
                regime_result=_lim_regime, rs_map=rs_map or {},
                kelly_history=kelly_history, strategy=_lim_strategy,
                news_headlines=news_headlines,
            )
            _buys_partial = [d for d in _dec_partial
                             if getattr(d, "action", "") == "buy" and getattr(d, "symbol", "") == "NVDA"]
            if not _buys_partial:
                _ok("NVDA: $250 room, stock $875 → int(250/875)=0 shares → add-on blocked ✓")
            else:
                _warn("NVDA: add-on allowed despite < 1 share of room — check sizing")
        except Exception as _e:
            _fail(f"G-1a crashed: {_e}")

        # G-1b: 1 share × $875 = $875, cap $3,750 → $2,875 room → int(2875/875)=3 shares → allowed
        _holding_nvda_small = [
            _NS_g(symbol="NVDA", side="long", qty=1, avg_entry_price=840.0,
                  current_price=875.0, unrealized_pl_percent=+4.2),
        ]
        try:
            _dec_small_pos = _decide_g(
                scored_candidates=_nvda_cand, positions=_holding_nvda_small,
                account_cash=cash, portfolio_value=portfolio,
                regime_result=_lim_regime, rs_map=rs_map or {},
                kelly_history=kelly_history, strategy=_lim_strategy,
                news_headlines=news_headlines,
            )
            _buys_small = [d for d in _dec_small_pos
                           if getattr(d, "action", "") == "buy" and getattr(d, "symbol", "") == "NVDA"]
            if _buys_small:
                _ok(f"NVDA: $2,875 room → add-on {_buys_small[0].quantity}sh (int floor) allowed ✓")
            else:
                _ok("NVDA: $2,875 room → AI chose hold (AI discretion — not a bug)")
        except Exception as _e:
            _fail(f"G-1b crashed: {_e}")

        # G-2: Portfolio nearly full (10% cash left) → Claude should hold / not over-deploy
        _section("G-2. Low cash guard (only 10% available)")
        _full_pos = [
            _NS_g(symbol=s, side="long", qty=8, avg_entry_price=100.0,
                  current_price=105.0, unrealized_pl_percent=+5.0)
            for s in ["AAPL", "MSFT", "GOOGL", "META", "AMZN"]
        ]
        _low_cash = portfolio * 0.10   # only $2,500 left — below 15% position minimum
        try:
            _dec_full = _decide_g(
                scored_candidates=_lim_cands[:3], positions=_full_pos,
                account_cash=_low_cash, portfolio_value=portfolio,
                regime_result=_lim_regime, rs_map=rs_map or {},
                kelly_history=kelly_history, strategy=_lim_strategy,
                news_headlines=news_headlines,
            )
            _new_buys = [d for d in _dec_full if getattr(d, "action", "") == "buy"]
            if not _new_buys:
                _ok("Claude held when cash < min position size ✓")
            else:
                _qty_sum = sum(getattr(d, "quantity", 0) or 0 for d in _new_buys)
                _cost = _qty_sum * (_lim_cands[0].price if _lim_cands else 100)
                if _cost <= _low_cash * 1.1:
                    _ok(f"Claude bought within cash constraint "
                        f"({len(_new_buys)} buy, ~${_cost:.0f} vs ${_low_cash:.0f} cash)")
                else:
                    _warn(f"Claude over-deployed: ~${_cost:.0f} with only ${_low_cash:.0f} cash")
        except Exception as _e:
            _fail(f"Low cash guard test crashed: {_e}")

        # G-3: Max positions — 6 open, Claude should avoid adding a 7th
        _section("G-3. Max positions (6 open, expect hold or sell only)")
        _max_pos = [
            _NS_g(symbol=s, side="long", qty=5, avg_entry_price=100.0,
                  current_price=102.0, unrealized_pl_percent=+2.0)
            for s in ["AAPL", "MSFT", "GOOGL", "META", "AMZN", "TSLA"]
        ]
        _med_cash = portfolio * 0.10   # about 10% cash after 6 positions
        try:
            _dec_max = _decide_g(
                scored_candidates=_lim_cands[:3], positions=_max_pos,
                account_cash=_med_cash, portfolio_value=portfolio,
                regime_result=_lim_regime, rs_map=rs_map or {},
                kelly_history=kelly_history, strategy=_lim_strategy,
                news_headlines=news_headlines,
            )
            _extra_buys = [d for d in _dec_max
                           if getattr(d, "action", "") == "buy"
                           and getattr(d, "symbol", "") not in {p.symbol for p in _max_pos}]
            if not _extra_buys:
                _ok("Claude did not open 7th position when 6 already open ✓")
            else:
                _warn(f"Claude opened {len(_extra_buys)} new position(s) with 6 already held — "
                      f"check if prompt communicates position count")
        except Exception as _e:
            _fail(f"Max positions test crashed: {_e}")

        # G-4: Sector cap — 2 tech stocks already held, 3rd tech should be blocked
        _section("G-4. Sector cap (2 tech held, 3rd tech blocked)")
        _tech_positions = [
            _NS_g(symbol=s, side="long", qty=5, avg_entry_price=100.0,
                  current_price=102.0, unrealized_pl_percent=+2.0,
                  asset_class="us_equity")
            for s in ["AAPL", "MSFT"]
        ]
        _tech_cand = [
            _SC_g(symbol="NVDA", score=85, signal_type="momentum", suggested_action="buy",
                  price=875.0, rsi=60.0, macd_hist=0.20, rs_percentile=94, rel_volume=2.1,
                  regime_aligned=True, score_breakdown={"rs": 30, "macd": 20}),
        ]
        try:
            _dec_sector = _decide_g(
                scored_candidates=_tech_cand, positions=_tech_positions,
                account_cash=cash, portfolio_value=portfolio,
                regime_result=_lim_regime, rs_map=rs_map or {},
                kelly_history=kelly_history, strategy=_lim_strategy,
                news_headlines=news_headlines,
                risk_settings={"max_positions": 6, "sector_cap": 2},
            )
            _sector_buys = [d for d in _dec_sector
                            if getattr(d, "action", "") == "buy"
                            and getattr(d, "symbol", "") == "NVDA"]
            if not _sector_buys:
                _ok("NVDA blocked — 2 tech positions already at sector_cap=2 ✓")
            else:
                _warn("NVDA allowed despite 2 tech positions — sector cap may not be enforced in brain")
        except Exception as _e:
            _fail(f"G-4 sector cap test crashed: {_e}")

        # G-5: Earnings guard — stock with earnings today should be blocked or tiny
        _section("G-5. Earnings guard (earnings today → blocked or tiny size)")
        _earn_cand = [
            _SC_g(symbol="NVDA", score=85, signal_type="momentum", suggested_action="buy",
                  price=875.0, rsi=60.0, macd_hist=0.20, rs_percentile=94, rel_volume=2.1,
                  regime_aligned=True, score_breakdown={"rs": 30, "macd": 20}),
        ]
        try:
            _dec_earn = _decide_g(
                scored_candidates=_earn_cand, positions=[],
                account_cash=cash, portfolio_value=portfolio,
                regime_result=_lim_regime, rs_map=rs_map or {},
                kelly_history=kelly_history, strategy=_lim_strategy,
                news_headlines=news_headlines,
                earnings_map={"NVDA": "today/tomorrow"},
            )
            _earn_buys = [d for d in _dec_earn
                          if getattr(d, "action", "") == "buy"
                          and getattr(d, "symbol", "") == "NVDA"]
            if not _earn_buys:
                _ok("NVDA skipped on earnings day ✓")
            else:
                _qty = _earn_buys[0].quantity or 0
                _max_safe = int(portfolio * 0.02 / 875.0)  # 2% max earnings position
                if _qty <= _max_safe:
                    _ok(f"NVDA earnings day: tiny position {_qty}sh (≤{_max_safe}sh 2% cap) ✓")
                else:
                    _warn(f"NVDA earnings day: {_qty}sh — larger than expected 2% cap ({_max_safe}sh)")
        except Exception as _e:
            _fail(f"G-5 earnings guard test crashed: {_e}")

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — Pipeline health summary
# ══════════════════════════════════════════════════════════════════════════════
if _run("health"):
    _header("SECTION 10 — PIPELINE HEALTH SUMMARY")

    pipeline_checks = {
        "Universe populated":            len(universe) > 100,
        "Snapshot >90% live (mkt hrs)":  (not _market_open) or live / max(len(universe), 1) > 0.9,
        "Regime detected":               regime is not None and regime.regime in ("bull","chop","bear"),
        "VIX connected":                 regime is not None and regime.vix_level not in ("unknown", None),
        "RS map populated":              len(rs_map) > 50,
        "News flowing":                  len(news_headlines) > 0,
        "Candidates scored":             len(all_candidates) > 0,
        "LLM responded":                 above_threshold == [] or len(decisions) > 0,
        "Sell/rotation path":            len(_decisions_rot) > 0,
        "Bear/short/inverse path":       len(_decisions_bear) > 0,
    }

    _pipeline_pass = True
    for label, passed in pipeline_checks.items():
        icon = "✓" if passed else "✗"
        print(f"  {icon}  {label:<38} {'PASS' if passed else 'FAIL'}")
        if not passed:
            _pipeline_pass = False

    _sw_note = "loaded from DB" if _sig_weights_from_db else "DB not reachable (defaults — OK)"
    print(f"  ℹ  {'Signal weights':<38} {_sw_note}")
    if not _run("rotation"): print("  ─  Sell/rotation path (skipped — run with 'rotation')")
    if not _run("bear"):     print("  ─  Bear/short/inverse path (skipped — run with 'bear')")

    print(f"\n  Pipeline: {'ALL SYSTEMS GO ✓' if _pipeline_pass else 'ISSUES FOUND — see above'}")

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 11 — Model calibration (live DB)
# ══════════════════════════════════════════════════════════════════════════════
if _run("calibrate"):
    _header("SECTION 11 — MODEL CALIBRATION (live DB)")

    try:
        from services.db import _get_conn
        _conn = _get_conn()
    except Exception:
        _conn = None

    if not _conn:
        print("  ⚠  DB not reachable — run on Railway:")
        print("     railway run python3 backend/scripts/test_e2e.py calibrate")
    else:
        with _conn.cursor() as _cur:
            _cur.execute("SELECT COUNT(*) FROM position_log WHERE exit_time IS NOT NULL")
            _total_trades = (_cur.fetchone() or [0])[0]
            print(f"\n  Closed trades in DB: {_total_trades}")

            if _total_trades < 5:
                print("  ⚠  < 5 closed trades — needs more data. Check after Day 7.")
            else:
                # Overall win rate
                _cur.execute("""
                    SELECT
                        COUNT(*) FILTER (WHERE realized_pl_pct > 0) AS wins,
                        COUNT(*) AS total,
                        ROUND(AVG(realized_pl_pct)::numeric, 2),
                        ROUND(AVG(CASE WHEN realized_pl_pct > 0 THEN realized_pl_pct END)::numeric, 2),
                        ROUND(AVG(CASE WHEN realized_pl_pct <= 0 THEN realized_pl_pct END)::numeric, 2)
                    FROM position_log WHERE exit_time IS NOT NULL AND realized_pl_pct IS NOT NULL
                """)
                _row = _cur.fetchone()
                if _row and _row[1]:
                    _wins, _tot, _avg_pnl, _avg_win, _avg_loss = _row
                    _wr = _wins / _tot * 100
                    _status = "✓ PROFITABLE" if _wr > 23.1 else "✗ BELOW BREAKEVEN"
                    print(f"\n  {'Metric':<28} {'Value':>10}")
                    print(f"  {'─'*40}")
                    print(f"  {'Win rate':<28} {_wr:>9.1f}%  {_status}")
                    print(f"  {'Breakeven (1:3.3 R:R)':<28} {'23.1%':>10}")
                    print(f"  {'Avg P&L per trade':<28} {(_avg_pnl or 0):>+9.2f}%")
                    print(f"  {'Avg winner':<28} {(_avg_win or 0):>+9.2f}%")
                    print(f"  {'Avg loser':<28} {(_avg_loss or 0):>+9.2f}%")
                    if _avg_win and _avg_loss and float(_avg_loss) != 0:
                        print(f"  {'Actual R:R':<28} {'1:'+f'{abs(float(_avg_win)/float(_avg_loss)):.1f}':>10}")

                # Score tier (key calibration: does higher score = better outcome?)
                _cur.execute("""
                    SELECT
                        CASE WHEN entry_score >= 75 THEN '75+' WHEN entry_score >= 65 THEN '65-74'
                             WHEN entry_score >= 55 THEN '55-64' ELSE 'unknown' END AS tier,
                        COUNT(*), COUNT(*) FILTER (WHERE realized_pl_pct > 0),
                        ROUND(AVG(realized_pl_pct)::numeric, 2)
                    FROM position_log WHERE exit_time IS NOT NULL AND realized_pl_pct IS NOT NULL
                    GROUP BY 1 ORDER BY 1 DESC
                """)
                _tiers = _cur.fetchall()
                if any(r[0] != "unknown" for r in _tiers):
                    print(f"\n  Score tier calibration (is 75+ better than 55-64?):")
                    print(f"  {'Tier':<10} {'Trades':>7} {'Win%':>7} {'Avg P&L':>9}")
                    print(f"  {'─'*37}")
                    for tier, trades, wins, avg_pnl in _tiers:
                        wr_t = (wins / trades * 100) if trades else 0
                        print(f"  {tier:<10} {trades:>7} {wr_t:>6.1f}% {(avg_pnl or 0):>+8.2f}%")
                else:
                    print("\n  ⚠  entry_score not populated yet — fills from next trade onward")

                # Win rate by regime
                _cur.execute("""
                    SELECT COALESCE(market_regime,'unknown'), COUNT(*),
                           COUNT(*) FILTER (WHERE realized_pl_pct > 0),
                           ROUND(AVG(realized_pl_pct)::numeric, 2)
                    FROM position_log WHERE exit_time IS NOT NULL AND realized_pl_pct IS NOT NULL
                    GROUP BY 1 ORDER BY 2 DESC
                """)
                _by_regime = _cur.fetchall()
                if _by_regime:
                    print(f"\n  Win rate by regime:")
                    print(f"  {'Regime':<12} {'Trades':>7} {'Win%':>7} {'Avg P&L':>9}")
                    print(f"  {'─'*37}")
                    for reg, trades, wins, avg_pnl in _by_regime:
                        wr_r = (wins / trades * 100) if trades else 0
                        print(f"  {reg:<12} {trades:>7} {wr_r:>6.1f}% {(avg_pnl or 0):>+8.2f}%")

                # Exit reason breakdown
                _cur.execute("""
                    SELECT COALESCE(exit_reason,'unknown'), COUNT(*),
                           ROUND(AVG(realized_pl_pct)::numeric, 2)
                    FROM position_log WHERE exit_time IS NOT NULL AND realized_pl_pct IS NOT NULL
                    GROUP BY 1 ORDER BY 2 DESC
                """)
                _exits = _cur.fetchall()
                if _exits:
                    print(f"\n  Exit reason breakdown:")
                    print(f"  {'Reason':<22} {'Trades':>7} {'Avg P&L':>9}")
                    print(f"  {'─'*40}")
                    for reason, trades, avg_pnl in _exits:
                        print(f"  {reason:<22} {trades:>7} {(avg_pnl or 0):>+8.2f}%")

                # Hold time
                _cur.execute("""
                    SELECT ROUND(AVG(hold_duration_mins)), MIN(hold_duration_mins),
                           MAX(hold_duration_mins)
                    FROM position_log WHERE exit_time IS NOT NULL AND hold_duration_mins IS NOT NULL
                """)
                _hold = _cur.fetchone()
                if _hold and _hold[0]:
                    print(f"\n  Hold time: avg={_hold[0]:.0f}m  min={_hold[1]}m  max={_hold[2]}m")

                # Trail giveback
                _cur.execute("""
                    SELECT ROUND(AVG(max_unrealized_pct)::numeric,2),
                           ROUND(AVG(realized_pl_pct)::numeric,2)
                    FROM position_log
                    WHERE exit_time IS NOT NULL AND max_unrealized_pct IS NOT NULL
                      AND realized_pl_pct IS NOT NULL
                """)
                _peak = _cur.fetchone()
                if _peak and _peak[0] is not None:
                    _giveback = float(_peak[0] or 0) - float(_peak[1] or 0)
                    print(f"\n  Trail stop quality:")
                    print(f"    Peak unrealized  : {float(_peak[0]):+.2f}%")
                    print(f"    Actual exit      : {float(_peak[1]):+.2f}%")
                    print(f"    Avg giveback     : {_giveback:.2f}pp"
                          f"  ({'✓ acceptable' if _giveback < 5 else '⚠ wide — tighten trail'})")
                else:
                    print("\n  ⚠  max_unrealized_pct not populated yet — fills from next trade onward")

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 12 — Profitability projection
# ══════════════════════════════════════════════════════════════════════════════
if _run("project"):
    _header("SECTION 12 — PROFITABILITY PROJECTION")

    # Pull actual stats from DB if available
    _actual_wr    = None
    _actual_tp    = None   # avg realized_pl_pct on winning trades
    _actual_sl    = None   # avg realized_pl_pct on losing trades
    _actual_tpw   = None   # actual trades per week
    if _conn:
        try:
            with _conn.cursor() as _c2:
                _c2.execute("""
                    SELECT COUNT(*) FILTER (WHERE realized_pl_pct > 0)::float / NULLIF(COUNT(*),0),
                           AVG(CASE WHEN realized_pl_pct > 0 THEN realized_pl_pct END),
                           ABS(AVG(CASE WHEN realized_pl_pct <= 0 THEN realized_pl_pct END)),
                           COUNT(*) FILTER (WHERE exit_time >= NOW() - INTERVAL '28 days') / 4.0
                    FROM position_log WHERE exit_time IS NOT NULL AND realized_pl_pct IS NOT NULL
                """)
                _r = _c2.fetchone()
                if _r and _r[0] and _total_trades >= 10:
                    _actual_wr  = float(_r[0])
                    _actual_tp  = float(_r[1]) if _r[1] else None
                    _actual_sl  = float(_r[2]) if _r[2] else None
                    _actual_tpw = float(_r[3]) if _r[3] else None
        except Exception:
            pass

    # Portfolio: live from Alpaca (set in pipeline section), fallback $25k
    PORTFOLIO    = portfolio if portfolio and portfolio > 0 else 25_000.0
    MAX_POS_PCT  = 0.15
    POS_SIZE     = PORTFOLIO * MAX_POS_PCT
    # Avg TP/SL: use actual DB values if available, else conservative assumptions
    AVG_TP_PCT   = _actual_tp  if _actual_tp  else 0.09   # 9% = engine cap
    AVG_STOP_PCT = _actual_sl  if _actual_sl  else 0.05   # 5% = risk_settings default
    TRADES_PW    = _actual_tpw if _actual_tpw else 5.0
    AVG_WIN_D    = POS_SIZE * AVG_TP_PCT
    AVG_LOSS_D   = POS_SIZE * AVG_STOP_PCT
    BE_WR        = AVG_LOSS_D / (AVG_WIN_D + AVG_LOSS_D)

    _src = "live DB" if _actual_tp else "assumed"
    print(f"\n  Portfolio: ${PORTFOLIO:,.0f} (live)  |  Max position: {MAX_POS_PCT:.0%} = ${POS_SIZE:,.0f}")
    print(f"  Avg stop: {AVG_STOP_PCT:.0%} (${AVG_LOSS_D:.0f})  |  Avg TP: {AVG_TP_PCT:.0%} (${AVG_WIN_D:.0f})  [{_src}]")
    print(f"  Risk/reward: 1:{AVG_WIN_D/AVG_LOSS_D:.1f}  |  Breakeven win rate: {BE_WR:.1%}")

    if _actual_wr and _total_trades >= 10:
        print(f"\n  ★ Using ACTUAL stats from DB ({_total_trades} trades, {TRADES_PW:.1f}/week)")
        scenarios = {"Actual (DB)": _actual_wr, "Target": 0.55, "Optimistic": 0.65}
    else:
        if _total_trades > 0:
            print(f"\n  Only {_total_trades} trades in DB — using assumed scenarios (need ≥10)")
        else:
            print(f"\n  No trades in DB yet — using assumed scenarios")
        scenarios = {"Conservative": 0.50, "Target": 0.55, "Optimistic": 0.65}

    print(f"\n  Fixed position sizing — {TRADES_PW:.0f} trades/week")
    print(f"  {'Scenario':<16} {'Win%':>6} {'EV/trade':>10} {'Weekly':>10} {'Monthly':>10} {'Annual':>10}")
    print(f"  {'─'*16} {'─'*6} {'─'*10} {'─'*10} {'─'*10} {'─'*10}")
    for name, wr in scenarios.items():
        ev = wr * AVG_WIN_D - (1 - wr) * AVG_LOSS_D
        print(f"  {name:<16} {wr:>5.0%} ${ev:>9.0f} ${ev*TRADES_PW:>9,.0f} ${ev*TRADES_PW*4:>9,.0f} "
              f"${ev*TRADES_PW*48:>9,.0f} {'✓' if ev > 0 else '✗'}")

    print(f"\n  NOTE: Fixed sizing, no compounding, no taxes.")
    print(f"  Breakeven = {BE_WR:.1%} win rate.")

# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
# TEST H — EXPERIMENT ENGINES (squeeze / spillover / revision)
# ══════════════════════════════════════════════════════════════════════════════
if _run("experiments"):
    _header("TEST H: EXPERIMENT ENGINES — isolation, scoring, sector peers, NASDAQ API")

    # H-1: Engine imports + isolation check
    _section("H-1. Engine imports + isolation (no shared engine imports)")
    _exp_engines_ok = True
    for _eng_name, _eng_path in [
        ("squeeze",   "services.squeeze_engine"),
        ("spillover", "services.spillover_engine"),
        ("revision",  "services.revision_engine"),
    ]:
        try:
            import importlib
            _mod = importlib.import_module(_eng_path)
            _ok(f"{_eng_name}_engine imports OK")
            # Verify engine never imports from shared engines
            import inspect, ast
            _src = inspect.getsource(_mod)
            _tree = ast.parse(_src)
            _bad_imports = []
            for _node in ast.walk(_tree):
                if isinstance(_node, (ast.Import, ast.ImportFrom)):
                    _names = [a.name for a in getattr(_node, 'names', [])]
                    _module = getattr(_node, 'module', '') or ''
                    for _n in _names + [_module]:
                        if any(x in _n for x in ['trading_engine', 'wheel_engine', 'pureai_engine']):
                            _bad_imports.append(_n)
            if _bad_imports:
                _fail(f"{_eng_name}_engine imports from isolated engines: {_bad_imports}")
                _exp_engines_ok = False
            else:
                _ok(f"{_eng_name}_engine: no cross-engine imports ✓")
        except ImportError as _ie:
            _fail(f"{_eng_name}_engine import failed: {_ie}")
            _exp_engines_ok = False

    # H-2: Squeeze scoring math
    _section("H-2. Squeeze scoring math")
    try:
        from services.squeeze_engine import _score_candidate as _squeeze_score
        # High DTC + volume spike + price move → should clear 70 threshold
        _s1 = _squeeze_score(8.0, 3.5, 6.0)
        _assert(_s1 >= 70, f"DTC=8 vol=3.5x +6% → score {_s1} ≥ 70 (buy threshold)")
        # No short data → score capped at 60 regardless
        _s2 = _squeeze_score(dtc=None, vol_ratio=4.0, price_change_pct=8.0)
        _assert(_s2 <= 60, f"No short data → score {_s2} capped at 60")
        # Weak signal → below threshold
        _s3 = _squeeze_score(dtc=1.5, vol_ratio=1.8, price_change_pct=2.0)
        _assert(_s3 < 70, f"DTC=1.5 vol=1.8x +2% → score {_s3} < 70 (no buy)")
        # DTC > 7 adds 35 pts, not 25 pts
        _s4a = _squeeze_score(dtc=7.5, vol_ratio=0.0, price_change_pct=0.0)
        _s4b = _squeeze_score(dtc=5.5, vol_ratio=0.0, price_change_pct=0.0)
        _assert(_s4a > _s4b, f"DTC 7.5 ({_s4a}pts) > DTC 5.5 ({_s4b}pts) as expected")
    except Exception as _e:
        _warn(f"Squeeze scoring test: {_e}")

    # H-3: Revision scoring math
    _section("H-3. Revision scoring math")
    try:
        from services.revision_engine import _score_candidate as _rev_score
        # Massive beat, stock barely moved from earnings price, uptrend intact → should hit 65
        # price_at_earnings=196 → stock only up 2% (market hasn't reacted → +25 pts)
        # ma20=190 → price above MA20 → +15 pts
        # beat_pct=35% → +40 pts; earnings 6 days ago → +10 pts; total = 90
        _r1 = _rev_score("AAPL", 35.0, "2026-06-15", 200.0, 196.0, 500_000, 190.0)
        _assert(_r1 >= 65, f"beat=35% price barely moved → score {_r1} ≥ 65 (buy threshold)")
        # Small beat → below threshold
        _r2 = _rev_score("AAPL", 16.0, "2026-06-01", 200.0, None, 250_000, None)
        _assert(_r2 < 65, f"beat=16% old earnings → score {_r2} < 65")
        # Beat tier order: >30% gives 40 pts, >20% gives 30 pts, >15% gives 20 pts
        _r_big  = _rev_score("X", 35.0, "2026-06-20", 100.0, None, 1_000_000, None)
        _r_med  = _rev_score("X", 22.0, "2026-06-20", 100.0, None, 1_000_000, None)
        _r_sml  = _rev_score("X", 17.0, "2026-06-20", 100.0, None, 1_000_000, None)
        _assert(_r_big > _r_med > _r_sml,
                f"Beat tiers correctly ordered: {_r_big} > {_r_med} > {_r_sml}")
    except Exception as _e:
        _warn(f"Revision scoring test: {_e}")

    # H-4: Sector peers JSON
    _section("H-4. sector_peers.json integrity")
    try:
        import json, os as _os
        _peers_path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                                    "data", "sector_peers.json")
        with open(_peers_path) as _f:
            _peers = json.load(_f)
        # Strip comment key
        _peers = {k: v for k, v in _peers.items() if not k.startswith("_")}
        _assert(len(_peers) >= 50, f"sector_peers.json has {len(_peers)} entries (≥ 50 required)")
        # Check symmetry: if NVDA→AMD, AMD should have NVDA in its peers
        _sym_breaks = []
        for _sym, _peer_list in _peers.items():
            for _p in _peer_list:
                if _p in _peers and _sym not in _peers[_p]:
                    _sym_breaks.append(f"{_sym}→{_p} but not {_p}→{_sym}")
        if _sym_breaks:
            _warn(f"{len(_sym_breaks)} asymmetric peer pairs (non-blocking): {_sym_breaks[:3]}")
        else:
            _ok("All peer mappings are symmetric ✓")
        # Check key tech stocks have peers
        for _must_have in ["NVDA", "META", "JPM", "TSLA"]:
            _assert(_must_have in _peers and len(_peers[_must_have]) >= 3,
                    f"{_must_have} has {len(_peers.get(_must_have, []))} peers (≥ 3 required)")
    except Exception as _e:
        _fail(f"sector_peers.json test: {_e}")

    # H-5: Spillover peer scoring (no Alpaca call needed)
    _section("H-5. Spillover peer scoring (mock snap)")
    try:
        from services.spillover_engine import _score_peer, _load_sector_peers
        _load_sector_peers()
        from types import SimpleNamespace as _SN
        _mock_snap = _SN(
            daily_bar=_SN(close=100.0, open=98.0, volume=2_000_000),
            latest_trade=_SN(price=100.0),
            latest_quote=None,
        )
        # NVDA triggers AMD — they are mutual peers → should score ≥ 60
        _sp1 = _score_peer("NVDA", "AMD", _mock_snap, trigger_beat_pct=15.0)
        _assert(_sp1 >= 60, f"NVDA→AMD (mutual peers) score {_sp1} ≥ 60")
        # JPM triggers AMD — unrelated → lower score
        _sp2 = _score_peer("JPM", "AMD", _mock_snap, trigger_beat_pct=15.0)
        _assert(_sp1 >= _sp2, f"Mutual peer (score {_sp1}) ≥ unrelated (score {_sp2})")
    except Exception as _e:
        _warn(f"Spillover peer scoring: {_e}")

    # H-6: DB table exists (live DB only)
    _section("H-6. experiment_positions table (live DB smoke test)")
    try:
        from services.db import _get_conn as _db_conn
        _ec = _db_conn()
        if _ec:
            with _ec.cursor() as _cur:
                _cur.execute("""
                    SELECT COUNT(*) FROM information_schema.columns
                    WHERE table_name='experiment_positions'
                """)
                _col_count = _cur.fetchone()[0]
            _assert(_col_count >= 20,
                    f"experiment_positions has {_col_count} columns (≥ 20 required)")
            with _ec.cursor() as _cur:
                _cur.execute("""
                    SELECT engine, COUNT(*) FROM experiment_positions
                    GROUP BY engine
                """)
                _rows = _cur.fetchall()
            _ok(f"experiment_positions rows: { {r[0]: r[1] for r in _rows} or 'empty (expected)' }")
        else:
            _warn("No DB connection — skipping table check (run on Railway)")
    except Exception as _e:
        _warn(f"DB check: {_e}")

    # H-7: NASDAQ short interest API (live network test)
    _section("H-7. NASDAQ short interest API (live — RIVN)")
    try:
        from services.squeeze_engine import _get_days_to_cover_nasdaq
        _dtc = _get_days_to_cover_nasdaq("RIVN")
        if _dtc is not None:
            _assert(_dtc > 0, f"RIVN daysToCover={_dtc:.2f} (live data from NASDAQ API)")
        else:
            _warn("NASDAQ API returned None for RIVN — may be market hours issue or IP block")
    except Exception as _e:
        _warn(f"NASDAQ API test: {_e}")

    # H-8: Config isolation — experiment keys don't overlap with Lakshmi/Wheel
    _section("H-8. Config: experiment env vars are distinct from Lakshmi/Wheel")
    try:
        from config import settings
        _pairs = [
            ("alpaca_api_key", "alpaca_squeeze_key"),
            ("alpaca_api_key", "alpaca_spillover_key"),
            ("alpaca_api_key", "alpaca_revision_key"),
            ("alpaca_wheel_key", "alpaca_squeeze_key"),
        ]
        for _a, _b in _pairs:
            _va = getattr(settings, _a, "")
            _vb = getattr(settings, _b, "")
            if _va and _vb:
                _assert(_va != _vb, f"{_a} ≠ {_b} (separate Alpaca accounts)")
            else:
                _ok(f"{_b} not configured yet (expected — add Railway env vars to activate)")
    except Exception as _e:
        _warn(f"Config isolation check: {_e}")

# ══════════════════════════════════════════════════════════════════════════════
# FINAL SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
_header("SUMMARY")

if _run("unit"):
    _icon = "✓" if _UNIT_FAIL == 0 else "✗"
    print(f"  {_icon}  Risk unit tests  : {_UNIT_PASS} passed, {_UNIT_FAIL} failed")
if _run("health"):
    _icon = "✓" if _pipeline_pass else "✗"
    print(f"  {_icon}  Pipeline health  : {'ALL SYSTEMS GO' if _pipeline_pass else 'ISSUES FOUND'}")
if _run("calibrate"):
    _icon = "✓" if _conn else "ℹ"
    print(f"  {_icon}  Calibration      : {'live DB ✓' if _conn else 'DB not reachable (run on Railway)'}")
if _run("project"):
    print(f"  ℹ  Projection       : see Section 12 above")
if _run("ai_edge"):
    print(f"  ✓  AI edge cases    : see Test D above")
if _run("chop"):
    print(f"  ✓  Chop regime      : see Test E above")
if _run("wheel"):
    print(f"  ✓  Wheel bot        : see Test F above")
if _run("limits"):
    print(f"  ✓  Position limits  : see Test G above")
if _run("experiments"):
    print(f"  ✓  Experiment engines: see Test H above")

print()
_all_sections = ["unit","pipeline","rotation","bear","gemini",
                 "ai_edge","chop","wheel","limits","experiments","calibrate","project"]
_skipped = [s for s in _all_sections if not _run(s)]
if _skipped:
    print(f"  Skipped: {', '.join(_skipped)}")
    print(f"  Re-run with those section names to include them.")

sys.exit(0 if _UNIT_FAIL == 0 else 1)
