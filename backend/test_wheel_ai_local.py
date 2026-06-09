"""
Side-by-side comparison: Code-based selection vs AI selection.

Both receive the same top-20 quant-scored candidates.

Run:
  export ALPACA_WHEEL_KEY=...
  export ALPACA_WHEEL_SECRET=...
  export ALPACA_WHEEL_BASE_URL=https://paper-api.alpaca.markets
  export GEMINI_API_KEY=...          # needed for AI step
  python3 test_wheel_ai_local.py
"""

import os, sys, math, json, requests

KEY        = os.environ.get("ALPACA_WHEEL_KEY")
SECRET     = os.environ.get("ALPACA_WHEEL_SECRET")
BASE       = os.environ.get("ALPACA_WHEEL_BASE_URL", "https://paper-api.alpaca.markets")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

if not KEY or not SECRET:
    print("ERROR: set ALPACA_WHEEL_KEY and ALPACA_WHEEL_SECRET env vars first")
    sys.exit(1)

IS_PAPER = "paper" in BASE
print(f"Using: {'PAPER' if IS_PAPER else 'LIVE'} account")
print(f"Gemini: {'available' if GEMINI_KEY else 'NOT SET — AI step will be skipped'}\n")

os.environ.setdefault("ALPACA_WHEEL_KEY",      KEY)
os.environ.setdefault("ALPACA_WHEEL_SECRET",   SECRET)
os.environ.setdefault("ALPACA_WHEEL_BASE_URL", BASE)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Clients ───────────────────────────────────────────────────────────────────
from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient

trading_client = TradingClient(KEY, SECRET, paper=IS_PAPER)
data_client    = StockHistoricalDataClient(KEY, SECRET)
opts_client    = OptionHistoricalDataClient(KEY, SECRET)

# ── Sector map ────────────────────────────────────────────────────────────────
# Import sector map from production module so tests always stay in sync
from services.wheel_universe import _SECTOR_MAP as SECTOR_MAP

# ── Step 1: Score candidates ──────────────────────────────────────────────────
print("=" * 72)
print("SCORING CANDIDATE POOL  (same data, both methods will use this)")
print("=" * 72)

from services.wheel_universe import (
    _get_candidate_pool, _score_candidates,
    _DEFAULT_IV_HV_MIN, _DEFAULT_DRAWDOWN_MAX,
    _DEFAULT_IV_RANK_MIN, _DEFAULT_SCORE_MIN, _MIN_CANDIDATES,
)

print("Fetching + scoring candidates (~30s)...")
candidates = _get_candidate_pool(data_client, trading_client=trading_client, limit=150)
scored     = _score_candidates(candidates, data_client, opts_client=opts_client, trading_client=trading_client)
print(f"Scored {len(scored)} symbols.")

# Apply adaptive filters
iv_hv_min    = _DEFAULT_IV_HV_MIN
drawdown_max = _DEFAULT_DRAWDOWN_MAX
iv_rank_min  = _DEFAULT_IV_RANK_MIN
score_min    = _DEFAULT_SCORE_MIN

def passes(s):
    return (
        s.get("score",0)        >= score_min
        and s.get("iv_hv_ratio",0) >= iv_hv_min
        and (s.get("max_drawdown",100) <= drawdown_max or s.get("max_drawdown",100) >= 100)
        and s.get("iv_rank",0)  >= iv_rank_min
    )

qualified = [s for s in scored if passes(s)]
relax = 0
while len(qualified) < _MIN_CANDIDATES and relax < 4:
    iv_hv_min    = max(iv_hv_min    - 0.15, 0.5)
    drawdown_max = min(drawdown_max + 5,    50.0)
    iv_rank_min  = max(iv_rank_min  - 5,     0.0)
    score_min    = max(score_min    - 5,    20.0)
    relax += 1
    qualified = [
        s for s in scored
        if s.get("score",0)        >= score_min
        and s.get("iv_hv_ratio",0) >= iv_hv_min
        and (s.get("max_drawdown",100) <= drawdown_max or s.get("max_drawdown",100) >= 100)
        and s.get("iv_rank",0)     >= iv_rank_min
    ]
if len(qualified) < _MIN_CANDIDATES:
    qualified = scored[:_MIN_CANDIDATES]

# Hard pre-filter: yield/cycle > 30% = binary event trap
safe = [c for c in qualified if c.get("expected_yield_pct", 0) <= 30.0]
if len(safe) < 8:
    safe = qualified  # fallback if universe is thin
top20 = safe[:20]

try:
    from services.wheel_engine import _get_current_regime
    regime = _get_current_regime()
except Exception:
    regime = "neutral"

# ── Print top-20 input table ──────────────────────────────────────────────────
# max_drawdown is stored as percentage (8.0 = 8%) — no *100 needed
print(f"\nTOP 20 CANDIDATES  (regime: {regime.upper()}, thresholds relaxed {relax}x)\n")
print(f"{'#':<3} {'Sym':<6} {'Score':>5} {'Yld/cyc':>8} {'IV/HV':>6} {'IVrank':>7} {'HV30':>5} {'DD%':>5} {'Price':>8} {'Sector'}")
print("─" * 72)
for i, s in enumerate(top20, 1):
    sym    = s["symbol"]
    sector = SECTOR_MAP.get(sym, "other")
    dd     = s.get("max_drawdown", 0)  # already a percentage
    print(
        f"{i:<3} {sym:<6} {s.get('score',0):>5.0f} "
        f"{s.get('expected_yield_pct',0):>7.1f}% "
        f"{s.get('iv_hv_ratio',0):>6.2f}x "
        f"{s.get('iv_rank',0):>6.0f}% "
        f"{s.get('hv30',0):>5.0f}% "
        f"{dd:>5.1f}% "
        f"${s.get('price',0):>7.2f}  "
        f"{sector}"
    )

# ── Also show what got FILTERED OUT and why ───────────────────────────────────
filtered_out = [s for s in scored if s not in qualified]
if filtered_out:
    print(f"\nFILTERED OUT ({len(filtered_out)} stocks — showing why):")
    print(f"{'Sym':<6} {'Score':>5} {'IV/HV':>6} {'IVrank':>7} {'DD%':>5}  Reasons")
    print("─" * 60)
    for s in sorted(filtered_out, key=lambda x: -x.get("score",0))[:15]:
        reasons = []
        if s.get("score",0)        < score_min:    reasons.append(f"score={s.get('score',0):.0f}<{score_min}")
        if s.get("iv_hv_ratio",0)  < iv_hv_min:   reasons.append(f"IV/HV={s.get('iv_hv_ratio',0):.2f}<{iv_hv_min}")
        if s.get("iv_rank",0)      < iv_rank_min:  reasons.append(f"IVrank={s.get('iv_rank',0):.0f}<{iv_rank_min}")
        dd = s.get("max_drawdown",0)
        if dd < 100 and dd > drawdown_max:         reasons.append(f"DD={dd:.0f}%>{drawdown_max}")
        print(f"  {s['symbol']:<6} {s.get('score',0):>5.0f} {s.get('iv_hv_ratio',0):>6.2f}x {s.get('iv_rank',0):>6.0f}% {dd:>5.1f}%  {', '.join(reasons)}")


# ════════════════════════════════════════════════════════════════════════════
# METHOD A — CODE
# ════════════════════════════════════════════════════════════════════════════
def code_select(candidates: list[dict], regime: str, n: int = 10) -> list[dict]:
    # max_drawdown is already a percentage — compare directly against 30
    def effective_score(s):
        score = s.get("score", 0)
        dd    = s.get("max_drawdown", 0)   # already %
        if regime == "bear":
            if dd > 30: score -= 15
            if dd < 15: score += 8
        elif regime == "bull":
            score += s.get("iv_hv_ratio", 0) * 3
        return score

    pool = sorted(candidates, key=effective_score, reverse=True)

    selected: list[dict]      = []
    sector_count: dict[str,int] = {}
    high_dd = 0

    for s in pool:
        if len(selected) >= n:
            break
        sym    = s["symbol"]
        sector = SECTOR_MAP.get(sym, "other")
        dd     = s.get("max_drawdown", 0)

        if sector_count.get(sector, 0) >= 2:
            continue
        if dd > 30 and high_dd >= 2:
            continue

        if dd > 30:
            high_dd += 1
        sector_count[sector] = sector_count.get(sector, 0) + 1

        iv_hv   = s.get("iv_hv_ratio", 0)
        iv_rank = s.get("iv_rank", 0)
        yld     = s.get("expected_yield_pct", 0)
        edge    = "strong edge" if iv_hv >= 1.5 else "moderate edge" if iv_hv >= 1.2 else "fair value"
        safety  = "safe if assigned" if dd < 20 else "moderate risk" if dd < 35 else "high DD risk"
        reason  = (
            f"IV/HV {iv_hv:.2f}x ({edge}), IV rank {iv_rank:.0f}%, "
            f"yield {yld:.1f}%/cycle, DD {dd:.0f}% ({safety})"
        )
        selected.append({**s, "reason": reason})

    return selected

code_picks = code_select(top20, regime)


# ════════════════════════════════════════════════════════════════════════════
# METHOD B — AI  (direct Gemini HTTP call — no anthropic package needed)
# ════════════════════════════════════════════════════════════════════════════
def call_gemini(prompt: str) -> str:
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    resp = requests.post(
        url,
        params={"key": GEMINI_KEY},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"maxOutputTokens": 1500, "temperature": 0.2,
                                   "thinkingConfig": {"thinkingBudget": 0}}},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

candidate_text = "\n".join([
    f"{c['symbol']}: score={c['score']:.0f} | "
    f"yield={c.get('expected_yield_pct',0):.1f}%/cycle ({c.get('annual_yield_pct',0):.0f}%/yr) | "
    f"IV_rank={c.get('iv_rank',0):.0f}% | "
    f"IV/HV={c.get('iv_hv_ratio',0):.2f}x | "
    f"drawdown={c.get('max_drawdown',0):.0f}% | "
    f"HV30={c.get('hv30',0):.0f}% | "
    f"price=${c.get('price',0):.2f} | "
    f"sector={SECTOR_MAP.get(c['symbol'],'other')}"
    for c in top20
])

ai_prompt = f"""You are finalizing a wheel options strategy universe.

Current market regime: {regime.upper()}
Strategy: Sell 45-DTE cash-secured puts. Collect premium via theta decay.

Pre-scored candidates (quantitative filters already applied):
{candidate_text}

Key metrics:
- yield/cycle: expected premium as % of collateral per 45-day cycle — PRIMARY metric
- IV/HV ratio: >1.5x = strong edge (options overpriced vs actual moves)
- IV rank: 0-100, higher = options more expensive vs own history
- drawdown: worst 6-month drop. Lower = safer if assigned
- HV30: actual stock volatility. Moderate HV = income without excessive assignment risk

Select 8-10 stocks. Rules:
- Maximize TOTAL expected yield across the portfolio
- Max 2 per sector
- No more than 2 stocks with drawdown > 30%
- Prefer IV/HV > 1.3x
- In BEAR regime: weight stability (low drawdown) heavily
- In BULL/NEUTRAL regime: weight yield/cycle and IV/HV ratio
- REJECT any stock with yield/cycle > 30% — this signals a binary event (FDA, earnings, trial readout) not a premium edge. These are traps: the stock gaps down 60-80% if the event fails.

Return ONLY a valid JSON array, no other text:
[{{"symbol":"KO","reason":"IV/HV 1.82x strong edge, yield 5.7%/cycle, drawdown 8% safe if assigned"}}]"""

print(f"\n\n{'=' * 72}")
print("PROMPT SENT TO AI:")
print("=" * 72)
print(ai_prompt)

ai_picks = []
if GEMINI_KEY:
    print(f"\nCalling Gemini Flash...")
    try:
        raw = call_gemini(ai_prompt)
        print(f"\nRAW AI RESPONSE:")
        print("─" * 72)
        print(raw)

        # Parse JSON from response
        import re
        json_match = re.search(r'\[.*?\]', raw, re.DOTALL)
        if json_match:
            parsed = json.loads(json_match.group())
            score_map = {c["symbol"]: c for c in top20}
            for pick in parsed:
                sym = str(pick.get("symbol","")).upper()
                if sym in score_map:
                    ai_picks.append({**score_map[sym], "reason": pick.get("reason","AI selected")})
        print(f"\nParsed {len(ai_picks)} AI picks.")
    except Exception as e:
        print(f"\nAI call failed: {e}")
        ai_picks = []
else:
    print("\n[Skipping AI — set GEMINI_API_KEY to enable]")


# ════════════════════════════════════════════════════════════════════════════
# COMPARISON
# ════════════════════════════════════════════════════════════════════════════
def portfolio_yield(picks):
    return sum(p.get("expected_yield_pct", 0) for p in picks)

def sector_breakdown(picks):
    out: dict[str, list[str]] = {}
    for p in picks:
        s = SECTOR_MAP.get(p["symbol"], "other")
        out.setdefault(s, []).append(p["symbol"])
    return out

code_syms = [p["symbol"] for p in code_picks]
ai_syms   = [p["symbol"] for p in ai_picks]

print("\n\n" + "═" * 72)
print("COMPARISON")
print("═" * 72)

col_w = 28
print(f"\n{'':30} {'CODE':>{col_w}}   {'AI':>{col_w}}")
print("─" * 72)
print(f"{'Stocks picked':<30} {', '.join(code_syms)}")
if ai_syms:
    print(f"{'(AI)':<30} {'':{col_w}}   {', '.join(ai_syms)}")
print(f"{'Count':<30} {len(code_picks):>{col_w}}   {len(ai_picks):>{col_w}}")
print(f"{'Total yield/cycle':<30} {portfolio_yield(code_picks):>{col_w-1}.1f}%  {portfolio_yield(ai_picks):>{col_w-1}.1f}%")

print(f"\nSECTOR BREAKDOWN:")
code_sectors = sector_breakdown(code_picks)
ai_sectors   = sector_breakdown(ai_picks)
all_sectors  = sorted(set(list(code_sectors.keys()) + list(ai_sectors.keys())))
for sec in all_sectors:
    c = ", ".join(code_sectors.get(sec, ["-"]))
    a = ", ".join(ai_sectors.get(sec, ["-"]))
    flag = " ⚠️  >2" if len(code_sectors.get(sec,[])) > 2 or len(ai_sectors.get(sec,[])) > 2 else ""
    print(f"  {sec:<14}: CODE={c:<22}  AI={a}{flag}")

if ai_syms:
    only_code = [s for s in code_syms if s not in ai_syms]
    only_ai   = [s for s in ai_syms   if s not in code_syms]
    both      = [s for s in code_syms if s in ai_syms]
    score_map = {s["symbol"]: s for s in top20}

    print(f"\n{'Agreed on':<30} {', '.join(both) or 'none'}")
    print(f"{'Only CODE picked':<30} {', '.join(only_code) or 'none'}")
    print(f"{'Only AI picked':<30} {', '.join(only_ai) or 'none'}")

    if only_code or only_ai:
        print("\nDIVERGENCE DETAIL:")
        for sym in only_code:
            s = score_map.get(sym, {})
            print(f"  CODE kept  {sym:<6}: score={s.get('score',0):.0f} IV/HV={s.get('iv_hv_ratio',0):.2f}x DD={s.get('max_drawdown',0):.0f}% sector={SECTOR_MAP.get(sym,'?')}")
        for sym in only_ai:
            s = score_map.get(sym, {})
            print(f"  AI  took   {sym:<6}: score={s.get('score',0):.0f} IV/HV={s.get('iv_hv_ratio',0):.2f}x DD={s.get('max_drawdown',0):.0f}% sector={SECTOR_MAP.get(sym,'?')}")

print("\n" + "═" * 72)
print("CODE selections + reasoning:")
for p in code_picks:
    print(f"  {p['symbol']:<6} {p['reason']}")

if ai_picks:
    print("\n" + "═" * 72)
    print("AI selections + reasoning:")
    for p in ai_picks:
        print(f"  {p['symbol']:<6} {p.get('reason','')}")

print("\nDone.")
