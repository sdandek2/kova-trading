# Kova Trading Bot — Master Build Plan

## How to Start a New Session
Tell Claude: **"Read KOVA_BUILD_PLAN.md and continue from [task below]"**
This file has everything. No re-explaining needed.

---

## Current Status (as of last session)
- **Paper trading:** active on Alpaca
- **Target:** move to live within ~1 week
- **Alpaca level:** Level 3 options (all strategies enabled)
- **All 7 phases built** — see status below
- **Immediate next task:** Replace paid API connectors with free alternatives (see Pending Tasks)

---

## Pending Tasks (do these next, in order)

### TASK 1 — Replace paid connectors with free alternatives ⭐ DO THIS FIRST
Paid APIs (UW, FMP, Quiver) were built but user doesn't want to pay yet.
Replace all 3 with free implementations:

**B — Unusual Whales → Alpaca options volume anomaly detection (FREE)**
- File to rewrite: `backend/services/brain/connectors/unusual_whales.py`
- Logic: fetch options chain from Alpaca API for the symbol
  - Compare today's total call volume vs 20-day average call volume
  - Compare today's total put volume vs 20-day average put volume
  - If call vol / avg_call_vol >= 3.0 → bullish signal (+25 pts)
  - If call vol / avg_call_vol >= 1.5 → bullish signal (+10 pts)
  - If put vol / avg_put_vol >= 3.0 → bearish signal (+25 pts)
  - Same interface: returns `{"signal": ..., "conviction_boost": ..., "details": ...}`
- Alpaca options chain endpoint: `GET /v2/options/contracts?underlying_symbols={symbol}`
- Use `alpaca_service` pattern (already exists) for the HTTP call

**H — FMP → yfinance analyst estimates (FREE)**
- File to rewrite: `backend/services/brain/connectors/fmp.py`
- Logic: `pip install yfinance` then:
  ```python
  import yfinance as yf
  ticker = yf.Ticker(symbol)
  info = ticker.info
  # current_price vs analyst target = revision signal
  target = info.get("targetMeanPrice")
  current = info.get("currentPrice")
  # Also check: info.get("recommendationKey") — "buy"/"strong_buy" = bullish
  ```
- Same interface: returns `{"signal": ..., "conviction_boost": ..., "details": ...}`
- Cache results 4 hours per symbol (same as current fmp.py)

**I — Quiver → FINRA weekly dark pool reports (FREE)**
- File to rewrite: `backend/services/brain/connectors/quiver.py`
- FINRA publishes weekly OTC/dark pool data at: https://otctransparency.finra.org/
- Alternatively: use yfinance institutional holders as a proxy
  ```python
  ticker = yf.Ticker(symbol)
  inst = ticker.institutional_holders  # DataFrame
  # If top institutions increased holdings recently → accumulating
  ```
- Keep same interface. Lower conviction scores since data is weekly not real-time.
- Note: this is the least critical of the three — can stub out with neutral if complex

**After rewriting all 3:** remove the `UW_API_KEY`, `FMP_API_KEY`, `QUIVER_API_KEY` entries
from the `.env` example since they're no longer needed.

---

### TASK 2 — Wire options_engine.py into trading_engine.py
`options_engine.py` exists but is NOT yet called by the trading engine.
Currently all trades go through stock orders only.

**What to do:**
- In `trading_engine.py`, after `ai_brain.decide()` returns decisions,
  check each decision: if `holding_period == "swing"` → route to `options_engine`
- `ai_brain.py` needs to add `holding_period: "intraday" | "swing"` to each TradeDecision
- Add `holding_period` field to `TradeDecision` model in `backend/models/trade.py`
- Options orders use different Alpaca endpoint: `POST /v2/options/orders`
- Start with just long calls/puts (simplest). Spreads and iron condors come after.

**Routing logic:**
```
confidence=high + regime=bull + hold 2-5 days  → buy call (swing)
confidence=high + regime=bear                   → buy put (swing)  
confidence=medium or intraday                   → buy stock (unchanged)
regime=chop + high conviction                   → iron condor (Phase 3 full)
```

---

### TASK 3 — Wire mean_reversion.py into signals.py
`mean_reversion.py` exists but `signals.py` doesn't call it yet.

**What to do:**
- In `signals.py` `score_universe()`, after scoring all candidates:
  - Call `mean_reversion.scan(universe_snapshot, regime_result)` 
  - For any symbol flagged as oversold by mean_reversion, boost its score +20
  - Set signal_type = "mean_reversion" for these symbols
- Mean reversion only runs when `regime_result.regime in ("bull", "chop")`

---

### TASK 4 — Wire learning.py into the trade close loop
`learning.py` exists but is never called.

**What to do:**
- In `trading_engine.py`, in the position-close detection block (~line 473),
  after `log_position_close()` is called, also call:
  ```python
  from services.brain.learning import record_trade_outcome
  record_trade_outcome(symbol, entry_price, exit_price, signal_type, regime, ...)
  ```
- After 50+ trades, `learning.py` trains a classifier and adjusts Kelly sizing
- `kelly.py` should call `learning.get_conviction_multiplier(symbol, signal_type)` 
  to get an ML-adjusted multiplier before finalizing share count

---

### TASK 5 — Run backtest before going live
`backtest.py` exists. Run it to validate the strategy.

**What to do:**
- Run: `python3 backend/services/brain/backtest.py`
- Target metrics before going live:
  - Win rate ≥ 50%
  - Avg win / avg loss ≥ 1.5 (make more on wins than you lose on losses)
  - Max drawdown ≤ 15%
  - Sharpe ratio ≥ 1.0
- If metrics fail: adjust stop loss pct or min_score threshold in signals.py

---

### TASK 6 — Go live checklist
Before switching from paper to live:
1. ✅ Backtest validates (Task 5)
2. Change `.env`: `ALPACA_PAPER=false` (or equivalent env var)
3. Reduce `max_position_pct` to 0.10 for first week of live trading (extra caution)
4. Set `daily_loss_limit_pct` to 2.0 for first week (tighter circuit breaker)
5. Monitor first 3 live days manually — check logs every hour
6. After 1 week profitable → restore max_position_pct to 0.15

---

## What to NEVER touch (working infrastructure)
- `backend/services/alpaca_service.py` — Alpaca API, orders, positions, market data
- `backend/services/db.py` — database, cache, trade logging
- `backend/websocket/` — live WebSocket updates to iOS app
- `backend/routers/` — all API endpoints
- `backend/models/` — data models (except adding `holding_period` to TradeDecision for Task 2)
- `ios/` — entire iOS app
- `Dockerfile`, `railway.toml` — deployment config

---

## Architecture: New Trading Brain
```
backend/services/brain/
├── __init__.py             ✅ lazy imports (pydantic-safe)
├── regime.py               ✅ LIVE — detects bull/bear/chop each cycle
├── rs_ranking.py           ✅ LIVE — ranks stocks vs SPY, filters top 60th pct
├── kelly.py                ✅ LIVE — Kelly Criterion sizing (ATR fallback < 10 trades)
├── signals.py              ✅ LIVE — scores universe 0-100 before Claude sees it
├── ai_brain.py             ✅ LIVE — regime-aware Claude wrapper, top candidates only
├── options_engine.py       ✅ BUILT — not yet wired (Task 2)
├── mean_reversion.py       ✅ BUILT — not yet wired (Task 3)
├── backtest.py             ✅ BUILT — not yet run (Task 5)
├── learning.py             ✅ BUILT — not yet wired (Task 4)
└── connectors/
    ├── unusual_whales.py   ⚠️  NEEDS REWRITE → free Alpaca-based version (Task 1)
    ├── fmp.py              ⚠️  NEEDS REWRITE → free yfinance version (Task 1)
    └── quiver.py           ⚠️  NEEDS REWRITE → free FINRA/yfinance version (Task 1)
```

**How the cycle works right now (every 10 min during market hours):**
```
trading_engine.run_trading_cycle()
  1. Alpaca: get account, positions, universe snapshot
  2. brain/regime.py: detect bull/bear/chop from SPY + VIX + breadth
  3. brain/rs_ranking.py: rank all stocks vs SPY, build rs_map
  4. brain/kelly.py: load trade history for sizing
  5. brain/signals.py: score every stock 0-100 → top 12 candidates
  6. brain/ai_brain.py: Claude evaluates top 12 with regime context → 1-3 decisions
  7. trading_engine: execute orders (stock only for now)
  8. trading_engine: manage open positions (trailing stops, scale-outs, pyramiding)
  ↓ FALLBACK: if brain fails at any step → claude_service.analyze_and_decide() runs instead
```

---

## Key Design Decisions (don't reverse these)

| Decision | Reason |
|----------|--------|
| `max_position_pct = 0.15` | Was 0.30 — halved to reduce loss magnitude |
| `min_confidence = "medium"` | Was "low" — stopped forcing speculative trades |
| `stop_loss = -4%` (aggressive) | Was -6% — tighter cuts smaller losses faster |
| `pyramid_tier1 at 10%` | Was 5% — avoids pyramiding near false breakout tops |
| `daily_circuit_breaker = 4%` | Was 6% — halts buys earlier on bad days |
| Afternoon pressure = soft nudge | Was "MUST trade" — removed forced bad trades |
| 2h stop-out cooldown | Prevents re-buying falling knives, allows afternoon recovery |
| MACD > 0 required for leveraged ETFs | Prevents entering 3x instruments with negative momentum |
| Leveraged ETFs blocked in non-bull regime | Prevents decay in choppy/bear markets |
| Half-Kelly sizing | Full Kelly is too aggressive — 50% reduces variance significantly |
| Brain has fallback to claude_service | Safety net — bot never stops trading if brain errors |

---

## Files Changed From Original (summary)
- `backend/services/strategy.py` — aggressive strategy tightened
- `backend/services/entry_timing.py` — stop-out cooldown, MACD guard for ETFs, tighter stops
- `backend/services/trading_engine.py` — brain wired in, regime blocks, pyramid threshold
- `backend/services/claude_service.py` — RS tags injected, regime note, Kelly sizing, brain params
- `backend/services/brain/` — all new (14 files)
- `KOVA_BUILD_PLAN.md` — this file

---

## Environment Variables
```bash
# backend/.env — existing
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ANTHROPIC_API_KEY=...
ALPACA_PAPER=true          # change to false when going live (Task 6)

# After Task 1 (free alternatives — no paid keys needed)
# UW_API_KEY, FMP_API_KEY, QUIVER_API_KEY no longer needed
```

---

## Quick Reference: Key File Locations
| What | Where |
|------|-------|
| Trading cycle main loop | `backend/services/trading_engine.py` → `run_trading_cycle()` |
| AI trade decisions | `backend/services/brain/ai_brain.py` → `decide()` |
| Signal scoring | `backend/services/brain/signals.py` → `score_universe()` |
| Regime detection | `backend/services/brain/regime.py` → `detect_regime()` |
| Position sizing | `backend/services/brain/kelly.py` → `kelly_size()` |
| Options orders | `backend/services/brain/options_engine.py` (not yet wired) |
| Risk settings | `trading_engine.py` → `_RISK_DEFAULTS` (top of file) |
| Strategy config | `backend/services/strategy.py` → `STRATEGIES` dict |
| Stop-out cooldown | `backend/services/entry_timing.py` → `record_stopout()` |
