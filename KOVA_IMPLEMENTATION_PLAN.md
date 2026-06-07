# Kova AI Trading App — Implementation Plan
**Project path:** `/Users/siddheshdandekar/Documents/Siddhesh Playground/trading-app/backend`
**iOS path:** `/Users/siddheshdandekar/Documents/Siddhesh Playground/trading-app/ios/TradingApp/Sources`
**Stack:** Python FastAPI + iOS Swift + Railway (PostgreSQL) + Alpaca paper trading

---

## Context: What has already been implemented

### Session 1 fixes (already in code):
1. `unusual_whales.py` — fixed broken `alpaca_get` import, now uses direct httpx call to Alpaca options API
2. `signals.py` — added `closing_prices`, `high_prices`, `low_prices` fields to `ScoredCandidate` dataclass
3. `ai_brain.py` — fixed `_sym_data = {}` bug that made ATR always 0; now reads from `ScoredCandidate` fields
4. `kelly.py` — wired ML learning loop into position sizing via `predict_win_prob()`; added `ml_mult` to both Kelly and ATR fallback branches
5. `trading_engine.py` — fixed near-MA20 re-entry from `* 1.10` to `* 1.04`
6. `news_stream.py` — fixed auth timeout with proper WebSocket handshake sequence
7. `entry_timing.py` — added pre-market (8:30–9:30 ET) and after-hours (4:00–6:00 PM ET) modes; changed 9:45→9:35 AM start
8. `alpaca_service.py` — added `get_market_status()` and `submit_extended_hours_order()`
9. `trading_engine.py` — extended hours order routing (premarket = news-triggered only, afterhours = earnings plays only)
10. `requirements.txt` — added `scikit-learn>=1.4.0`
11. `news_stream.py` — fixed false warning for `{"T":"success","msg":"connected"}` handshake message

### Session 2 fixes (already in code):
1. Stop loss: 3% normal stocks, 5% leveraged ETFs — `entry_timing.py`
2. Pyramid first tier: 5% gain trigger (was 10%) — `trading_engine.py`
3. Scale-out counter: resets to 0 on re-entry (was 1) — `trading_engine.py`
4. Trade slot: counted after order confirms, not before — `trading_engine.py`
5. Iron condor wing math: fixed `short_call - long_put` → `long_call - short_call` — `options_engine.py`
6. Stale exit range: -0.5% to +0.5% (was -1% to +3%) — `trading_engine.py`
7. MACD zero-crossover: scores +20 (was +5); `prev_histogram` added to `indicators.py` — `signals.py`
8. Short cover ladder: 25% at +10%, 25% at +20% (was 50% at +10%) — `entry_timing.py`
9. Circuit breaker: blocks longs only, shorts allowed — `trading_engine.py`
10. No RSI trim before +15% in aggressive mode — `entry_timing.py`
11. Profit target: +20% aggressive (was +15%), +15% balanced — `entry_timing.py`
12. MACD decay exit: skipped when relative volume ≥ 1.5× — `trading_engine.py`
13. Spread-aware limit buffer: 0.1% liquid, 0.35% thin (was fixed 0.2%) — `alpaca_service.py`
14. Fill-followup worker: updates watermarks at actual fill price — `trading_engine.py`
15. `log_blocked_trade()`: logs symbol, block_reason, signal_score, price — `db.py`

### Session 3 additions (already in code):
1. **Blocked trades table + price tracking worker** — `db.py`, `trading_engine.py`
   - New `blocked_trades` DB table: symbol, block_reason, signal_score, price_at_block, price_15m, price_1h, price_eod, price_next_day, hypothetical_pnl_pct
   - Background worker checks back at 15m/1h/EOD and fills price fields automatically
   - `get_blocked_trades_report()` — ranks which block reason missed the most profit
2. **Long vs short scorecards** — `db.py`, `routers/performance.py`
   - `get_long_short_scorecard()` — win rate, avg winner %, avg loser %, profit factor, avg hold time split by side
   - `GET /api/performance/scorecard` endpoint
   - `GET /api/performance/blocked-trades` endpoint
3. **Regime-level capital allocation** — `trading_engine.py`
   - Bull: 1.0× position size (full)
   - Chop: 0.6× (breakouts fail constantly in sideways markets)
   - Bear: 0.5× longs, shorts unaffected
   - Extreme VIX (>30): 0.4× (capital preservation)
   - Applied to `effective_max_pct` before vol-adjust runs; defaults to 1.0 if regime detection fails
4. **iOS Analytics screen** — `Views/AI/AnalyticsView.swift`
   - "Analytics & Scorecards" navigation button added to AI Agent tab (below Performance button)
   - Long vs Short Scorecard section: win rate, avg winner/loser, profit factor, hold time per side
   - Blocked Trades Opportunity Cost section: ranked by missed P&L %, shows times blocked, would-have-won rate, avg signal score
   - New models: `SideScorecard`, `BlockedTradeReport` added to `Models/Performance.swift`
   - New API calls: `getLongShortScorecard()`, `getBlockedTradesReport()` added to `Services/APIService.swift`

---

## Current Signal Scoring (active vs inactive)

| Signal | Max Points | Status |
|---|---|---|
| RS Rank | +25 | ✅ Active |
| MACD (with zero-cross) | +20 | ✅ Active |
| Volume | +15 | ✅ Active |
| News sentiment | +20 | ✅ Active |
| RSI | +15 | ✅ Active |
| MA20 | +10 | ✅ Active |
| Alpaca options flow | +25 | ✅ Active (fixed) |
| Dark pool (Unusual Whales) | +25 | ❌ No API key |
| Earnings revision (FMP/Quiver) | +20 | ❌ No API key |

**Active max score: 130 points. Missing: 45 points from inactive connectors.**

---

## Risk Settings (set from iOS app)
- Strategy: aggressive
- Kelly multiplier: 0.5× (change to 0.75× after go-live)
- Max position pct: 0.15 (change to 0.20 after go-live)
- Stop loss pct: 0.03 (overridden by ATR stops once implemented)
- Daily loss limit: 4%
- Min daily trades: 0
- Cycle interval: configurable

## Go-Live Checklist (after 1 week paper trading review)
- [ ] Change `ALPACA_PAPER=false` in Railway env
- [ ] Change `_HALF_KELLY = 0.5` → `0.75` in `kelly.py`
- [ ] Change `max_position_pct` from 0.15 → 0.20 in strategy settings
- [ ] Change `pro_fallback` model from `gemini-2.5-flash` → `gemini-2.5-pro` in `app_settings` DB table

---

## Phase 1 — Completed in Session 4 ✅

### Session 4 additions (already in code):
1. **ATR-Based Stops** — `entry_timing.py`, `trading_engine.py`
   - Long stops: `entry - (1.5× ATR)` for stocks, `entry - (2× ATR)` for leveraged ETFs
   - Short stops: `entry + (2× ATR)` — tighter because squeezes are violent
   - `_atr_stops` dict in trading_engine stores stop price per symbol at entry, cleaned up on close
   - Falls back to flat % stop when ATR data unavailable
2. **AI Approval-Gate + Override Logging** — `ai_brain.py`
   - Signal-baseline logged before Claude: `BUY if score ≥ 55`, logged as `signal_baseline` events
   - Claude override logged after: candidates with score ≥ 65 skipped by Claude → `claude_override` event
   - Phase 3 query: `SELECT COUNT(*) FROM bot_activity_log WHERE event_type='claude_override' AND signal_score>=65`
3. **Short Position Limits** — `trading_engine.py`, `entry_timing.py`
   - Max 5% portfolio per short name (quantity capped, not blocked)
   - Max 15% total short exposure (blocks if already at cap)
   - Short stop at 2× ATR via `should_cover_short(atr_stop_price=...)`
4. **Portfolio VaR + Gross Exposure Logging** — `trading_engine.py`
   - Each cycle: VaR = sum of `position_value × (ATR/price)`, logged as `portfolio_var`
   - Gross exposure = sum of `abs(position_value)`, logged as `gross_exposure`
   - Both in `bot_activity_log` each cycle
5. **Correlation Check on New Buys** — `trading_engine.py`, `indicators.py`
   - `compute_correlation()` added to `indicators.py` (Pearson 60-day daily returns)
   - If existing long has corr > 0.75 with candidate AND combined exposure > 20% → halve new qty
6. **VWAP-Aware Entry Filter** — `alpaca_service.py`, `trading_engine.py`
   - `get_vwap()` in alpaca_service: fetches 1-min bars since market open, computes intraday VWAP
   - If price > VWAP × 1.008 → reduce qty by 25%
7. **Setup Type Tagging** — `trading_engine.py`, `db.py`
   - `setup_type`: `momentum_breakout` / `mean_reversion` / `event_driven` / `extended_hours`
   - `entry_hour_et`: ET hour of entry (9-15) for time-of-day analysis
   - DB migration adds both columns to `position_log`; `log_position_open()` updated to accept them
8. **Signal-Score Baseline Logging** — (bundled with #2 above, `ai_brain.py`)
9. **Slippage Measurement** — `trading_engine.py`, `db.py`
   - New `trade_slippage_log` table: symbol, side, limit_price, fill_price, slippage_dollars, slippage_pct
   - `log_slippage()` in db.py
   - Fill-followup worker now stores `estimated_price` and `quantity` in `_pending_fill_orders`, calls `log_slippage` when fill confirmed
10. **Gross Exposure Tracking** — (bundled with #4 above)
11. **Free Connector Improvements** — multiple files
    - Earnings proximity scoring (yfinance): ±10 pts in `signals.py` based on days until earnings
    - News phrase matching fix: "rate" → "interest rate", "fed rate", "rate hike" in `alpaca_service.py`
    - Put/call volume ratio added to `unusual_whales.py`: vol skew ±10 stacked on top of OI signal

---

## Phase 1 — iOS UI Still To Build

All 6 iOS items below are now unblocked (backend data is being collected). Next session: build these.

---

## iOS UI — What Still Needs to Be Built

### Already on iOS:
- ✅ Analytics & Scorecards screen (`AnalyticsView.swift`) — Long/short scorecard + blocked trades opportunity cost
- ✅ Performance screen — win rate, sharpe, profit factor, SPY comparison
- ✅ Trade History, Bot Activity, EOD Report, Pre-Market view
- ✅ Risk settings, strategy picker, watchlist, budget, model picker

### iOS UI — Completed in Session 5 ✅

All 6 cards built in `AnalyticsView.swift`. New models in `Performance.swift`, new API calls in `APIService.swift`, regime fields added to `TradingStatus.swift`.

**A. Regime Dashboard card** ✅ — regime badge, VIX level, capital multiplier %, confidence % — reads from `GET /api/trading/status`
- Current regime: BULL / CHOP / BEAR badge with color
- VIX level: low / normal / high / extreme
- Capital multiplier currently active: e.g. "60% size (chop regime)"
- Regime confidence %
- Backend: `GET /api/trading/status` already returns regime — check if `market_regime` field is exposed, or add `brain_regime` to the status endpoint
- Swift: small card on Dashboard or top of AnalyticsView

**B. Setup Type P&L breakdown** (add to AnalyticsView)
- Bar chart or grid: momentum_breakout / mean_reversion / event_driven / extended_hours
- Shows: trade count, total P&L, win rate per setup type
- Backend: `GET /api/performance/by-setup` — needs new endpoint querying `position_log.setup_type`
- Only meaningful after setup type tagging (Phase 1 item #7) is implemented

**C. AI vs Signal Baseline comparison** (add to AnalyticsView)
- Two numbers side by side: "Signal-only win rate" vs "Claude-assisted win rate"
- Claude override rate: "Claude rejected X% of high-score (≥65) signals"
- ✅ Built in Session 5

**D. Slippage Summary** ✅ Built in Session 5
**E. Portfolio VaR card** ✅ Built in Session 5
**F. Time-of-Day win rate chart** ✅ Built in Session 5 — horizontal bar chart with color-coded win rate

---

## iOS Backend API Endpoints — Completed in Session 5 ✅

| Endpoint | Status |
|---|---|
| `GET /api/performance/var` | ✅ Done |
| `GET /api/performance/by-setup` | ✅ Done |
| `GET /api/performance/ai-baseline` | ✅ Done |
| `GET /api/performance/slippage` | ✅ Done |
| `GET /api/performance/by-hour` | ✅ Done |
| `GET /api/trading/status` → regime fields | ✅ Done (`brain_regime`, `vix_level`, `regime_confidence`, `regime_capital_mult`) |

---

## Phase 2 — Backtest 2020-2024 (after paper data exists, week 2)

**Uses:** yfinance OHLCV daily, signal scoring (no AI — too expensive), next-day open + 0.15% slippage
**File:** `backend/services/brain/backtest.py` already exists — extend to loop 2020-2024

**Ablation tests (run as part of backtest — toggle each exit rule off one at a time):**
- Remove stale exit
- Remove MACD decay exit
- Remove aggressive scale-out
- Remove cooldown after stop-out
- Remove circuit breaker

**Output format:**
```
Year    Trades  Win%   AvgWin  AvgLoss  Sharpe  MaxDD
2020    187     61%    +4.2%   -2.8%    1.8     -11%
2022    156     44%    +5.1%   -3.1%    0.7     -18%  ← bear year validation
```

---

## Phase 3 — Day 60 Review (⚠️ REMINDER: ~2026-08-06)

```sql
-- Which block reason missed the most money?
SELECT block_reason, times_blocked, avg_hypothetical_pnl, total_hypothetical_pnl
FROM blocked_trades GROUP BY block_reason ORDER BY total_hypothetical_pnl DESC;

-- Which setup type makes money?
SELECT setup_type, COUNT(*), SUM(realized_pl), AVG(realized_pl_pct)
FROM position_log GROUP BY setup_type;

-- Is Claude helping or hurting?
SELECT COUNT(*) FROM bot_activity_log
WHERE event_type = 'claude_override' AND signal_score >= 65;
-- If > 30% → Claude is killing alpha

-- Slippage cost
SELECT AVG(slippage_pct), SUM(slippage_dollars) FROM trade_slippage_log;

-- Time-of-day win rate
SELECT entry_hour_et, COUNT(*), AVG(CASE WHEN realized_pl > 0 THEN 1.0 ELSE 0 END)
FROM position_log GROUP BY entry_hour_et ORDER BY entry_hour_et;

-- Stop-loss width by volatility bucket (compare ATR stop vs flat % stop outcomes)
SELECT setup_type, exit_reason, AVG(realized_pl_pct) FROM position_log GROUP BY setup_type, exit_reason;

-- Gross exposure trend (are we over-leveraging?)
SELECT DATE(created_at), AVG(CAST(metadata->>'gross_exposure_pct' AS FLOAT))
FROM bot_activity_log WHERE event_type = 'gross_exposure'
GROUP BY DATE(created_at) ORDER BY 1;
```

**Edge statement review:** Rewrite the one-paragraph edge statement using 60 days of actual data. Compare to Day 0 draft. If the data contradicts the draft (e.g. shorts don't add value, news-triggered beats RS-breakout), update the strategy accordingly.

---

## Phase 4 — After Go-Live + Profitable

- **Unusual Whales** (~$50/month) — dark pool flow, real options unusual activity
- **SPY puts as portfolio hedge** — defined risk, no squeeze, better than shorting ETFs
- **Strategy split into 3 books** (momentum 60% / mean-reversion 20% / event-driven 20%) — ⚠️ REMINDER: only when Sharpe ≥ 1.5 for 90 days

---

## Phase 5 — Tier 5 (time, not code)

| Metric | Target |
|---|---|
| Sharpe ratio | ≥ 1.5 annualized |
| Max drawdown | ≤ 12% |
| Win rate | ≥ 55% |
| Avg win / avg loss | ≥ 1.8× |
| Correlation to SPY | < 0.4 |
| Live track record | ≥ 6 months |

---

## Key Files Reference

| File | Purpose |
|---|---|
| `services/trading_engine.py` | Main trading loop, order routing, position management, regime mult |
| `services/brain/ai_brain.py` | Claude AI decision layer |
| `services/brain/signals.py` | Signal scoring 0-130 points |
| `services/brain/kelly.py` | Kelly Criterion position sizing (0.5× half-Kelly + ML mult) |
| `services/brain/learning.py` | ML learning loop (GradientBoostingClassifier, activates after 50 trades) |
| `services/brain/regime.py` | Market regime detection (bull/bear/chop + VIX level) |
| `services/brain/strategy.py` | Strategy config (max_position_pct etc.) |
| `services/brain/options_engine.py` | Options routing and iron condor logic (wing math fixed) |
| `services/brain/connectors/unusual_whales.py` | Options flow via Alpaca API (UW key inactive) |
| `services/entry_timing.py` | Entry/exit timing, stop loss, trail, RSI exits, short cover ladder |
| `services/alpaca_service.py` | Alpaca API wrapper, spread-aware limit orders, extended hours |
| `services/news_stream.py` | WebSocket news stream, urgent cycle triggering |
| `services/indicators.py` | MACD (returns prev_histogram), RSI, MA calculations |
| `services/db.py` | PostgreSQL: log_bot_activity, log_blocked_trade, get_long_short_scorecard, get_blocked_trades_report |
| `services/brain/backtest.py` | Backtesting engine (extend for 2020-2024) |
| `routers/performance.py` | GET /api/performance, /scorecard, /blocked-trades |
| `ios/.../Views/AI/AnalyticsView.swift` | Long/short scorecard + blocked trades opportunity cost screen |
| `ios/.../Models/Performance.swift` | PerformanceStats, SideScorecard, BlockedTradeReport |
| `ios/.../Services/APIService.swift` | All API calls including getLongShortScorecard, getBlockedTradesReport |

---

## DB Tables

| Table | Purpose |
|---|---|
| `app_settings` | Permanent settings (model config, risk settings) — no TTL |
| `ai_cache` | Temporary cache with TTL |
| `position_log` | Full round-trip P&L per trade (add: setup_type, entry_hour_et) |
| `bot_activity_log` | Per-cycle audit trail, blocked trades, circuit breaker events |
| `circuit_breaker_log` | When daily loss limit fires |
| `blocked_trades` | Blocked trades with 15m/1h/EOD/next-day price followup + hypothetical P&L |

---

## Architecture Notes

- **Trading loop:** async FastAPI, cycles every N seconds (configurable), skips if market closed
- **Market status:** `get_market_status()` → open/premarket/afterhours/closed using ZoneInfo("America/New_York")
- **Extended hours:** premarket (8:30-9:30 ET) = news-triggered only; afterhours (4:00-6:00 PM ET) = earnings plays only
- **Signal pipeline:** regime detection → RS ranking → signal scoring (0-130) → Claude approve/reject → Kelly sizing → regime mult → vol-adjust → order routing
- **Regime capital mult:** bull=1.0×, chop=0.6×, bear=0.5× longs, extreme VIX=0.4×
- **Kelly:** half-Kelly (0.5×, change to 0.75× after go-live), ML multiplier after 50 trades
- **AI models:** Claude Sonnet 4.6 (pro_primary), Gemini 2.5 Flash (pro_fallback — change to gemini-2.5-pro in app_settings), Claude Haiku 4.5 (standard_primary)
- **Pending fill tracking:** `_pending_fill_orders` dict — fill-followup worker updates watermarks at actual fill price
- **Pending block tracking:** `_pending_block_price_checks` dict — price-followup worker fills 15m/1h/EOD prices

---

## Decisions Already Made

- Paper trade 1 week → review → go live
- No Unusual Whales until go-live and profitable
- No SMS/push alerts needed
- No strategy split until Sharpe ≥ 1.5 for 90 days
- Skip over-complexity refactor — measure first
- Min daily trades = 0
- Aggressive mode throughout paper trading
- No new API keys needed until Phase 4

---

## ⚠️ Timed Reminders

- **Day 60 (~2026-08-06):** Run Phase 3 queries — regime performance, setup type P&L, AI override rate, slippage, time-of-day win rate
- **When Sharpe ≥ 1.5 for 90 days live:** Implement strategy split into 3 books

---

## Where to Start Next Session

**Phase 1 backend + iOS UI: COMPLETE ✅**

Next priority: **Phase 2 — Backtest 2020-2024** (once ~1 week of paper trading data exists)
- Extend `backend/services/brain/backtest.py` to loop 2020-2024 using yfinance
- Run ablation tests (toggle exit rules off one at a time)
- Output: Year / Trades / Win% / AvgWin / AvgLoss / Sharpe / MaxDD table

Then: Let paper trading run until **Day 60 (~2026-08-06)** and run Phase 3 queries.

## How to Start a New Session

Paste this at the start of the new conversation:
> "I'm working on the Kova AI trading app. Read this file first — it has full context of everything done and what's planned next: `/Users/siddheshdandekar/Documents/Siddhesh Playground/trading-app/KOVA_IMPLEMENTATION_PLAN.md`"
