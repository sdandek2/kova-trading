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

**G. Near-Miss Tracker card** ⏳ Session 7 — "Did we miss good trades?" (scores 35–54 only)

**H. Sprint Review card** ⏳ Session 7 — "What did the whole market do vs what Kova captured?"
- Shows stocks that scored 35–54 (just below trade threshold) and what they did next
- Counts how often we were right to skip vs. missed a profitable trade
- Breaks down which signal was the deciding factor (e.g. "options_flow blocked 12 trades, avg +4.8%")
- "Threshold verdict" line: tells you whether to raise/lower the 45-pt threshold based on data
- Full spec in Session 7 → Step 5
- **Files to create/modify:** `db.py`, `signals.py`, `trading_engine.py`, `routers/performance.py`, `Performance.swift`, `APIService.swift`, `AnalyticsView.swift`
- **DB migration:** `near_miss_log` table (see Session 7 → Step 5)

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
| `GET /api/performance/near-misses` | ⏳ Session 7 — near-miss tracker (scores 35–54) |
| `GET /api/performance/sprint-review/latest` | ⏳ Session 7 — most recent daily + weekly sprint review |
| `GET /api/performance/sprint-review/daily` | ⏳ Session 7 — single day movers vs Kova capture rate |
| `GET /api/performance/sprint-review/weekly` | ⏳ Session 7 — weekly signal performance + what's working |

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

## Session 6 — Signal Upgrades ✅ COMPLETED

### What was researched and decided (Session 6 planning):

**Researched and REJECTED:**
| Signal | Reason skipped |
|---|---|
| FMP earnings guard | User wants aggressive — blocks stocks that might go up |
| FMP earnings surprise (standalone) | ~40% direction accuracy. Macro overrides it (employment report wiped out DOCU +9% beat) |
| EODHD sentiment | Redundant with existing signals, unvalidated |
| Quiver Quantitative | Paid only, $115–345/month |
| OptionData.io unusual flow | 30 calls/month = useless. Code in their sample was mocked/fake |
| Alpaca dark pool (Exchange D) | Requires paid SIP feed. Free paper accounts get IEX only which excludes TRF/dark pool |
| Kadoa congressional trading | Data stale up to 45 days. Amounts mostly $1K–$15K (noise). Post-STOCK Act alpha small |
| USASpending gov contracts | Action dates missing from free tier — can't time entries |
| Senate lobbying | Quarterly data, too slow to be actionable |
| Massive.com | Duplicates Alpaca entirely |
| Nasdaq Data Link | WIKI dataset discontinued 2018. DEMO_KEY returns 403 |

**Why earnings surprise was reconsidered and added back:**
- With ATR stop protection, downside is capped at ~4%
- ZM beat +9.2% → drifted +12.5% over 8 days (real post-earnings drift exists)
- Math: 2 wins at +10% avg, 3 losses at -4% ATR stop = +8% net across 5 trades
- Single macro event (employment report) caused yesterday's drop — not a structural failure

---

### Signal 1: Earnings Surprise via FMP ✅ IMPLEMENT

**Source:** `https://financialmodelingprep.com/stable/earnings-calendar`
**API key:** `KloIwZY8s1YC0qceIgM4D1Fm1Vot1Np5` (add to Railway env as `FMP_API_KEY`)
**Free tier:** 250 calls/day. This uses ~2 calls/week (bulk fetch, cached)
**File to create:** `backend/services/brain/connectors/fmp_earnings.py`

**How it works — two-level implementation:**

Level 1 — Universe injection (proactive):
```python
# In alpaca_service.get_tradeable_universe()
# Fetch once/day: stocks that beat EPS by >10% in last 21 days
# Inject those symbols into universe for 21 days post-earnings
# This catches post-earnings drift on days 2–21 (after initial spike settles)
```

Level 2 — Score boost (when scoring injected symbols):
```python
# In signals.py
# +12 pts conviction if beat >10% in last 21 days
# -12 pts if missed >10% in last 21 days
```

**Bulk fetch (1 call for all symbols):**
```python
# Upcoming (1 call, cache 24h): from=today&to=today+21days
# Past beats (1 call, cache 24h): from=today-21days&to=today (max 21 days back on free tier)
```

**Cache:** 24h — data only changes daily at most

**Important:** Earnings surprise is NOT a standalone trade. Stock still needs base score ≥55 after boost.
ATR stops protect the downside when macro overrides the signal.

---

### Signal 2: SEC Form 4 Insider Buys via EDGAR ✅ IMPLEMENT

**Source:** `https://data.sec.gov/submissions/CIK{cik}.json`
**API key:** None needed. Free, official US government data.
**Headers required:** `User-Agent: Kova Trading kova@trading.com`
**File to create:** `backend/services/brain/connectors/sec_insider.py`

**How it works — two-level implementation:**

Level 1 — Universe injection (proactive):
```python
# Each cycle: scan recent Form 4 filings for net cash buys > $500K in last 14 days
# Inject those symbols into universe — finds stocks before market notices the filing
# Only count BUYS (acquired, code='A'). Ignore sells entirely.
# Sells are almost always RSU vesting, planned 10b5-1 programs, diversification — not informational
```

Level 2 — Score boost (when scoring injected symbols):
```python
# In signals.py:
# Net insider bought > $500K last 30 days → +15 pts
# Net insider bought > $100K last 30 days → +8 pts
# Sells: 0 pts (ignored completely)
```

**Data reliability:** Legally mandated filing. Insiders must file within 2 business days.
Data is 2–4 days old when we see it. Price cross-checked: Levinson $311.02 vs market $308-311 ✅

**Key distinction:** Only cash purchases signal conviction.
- CEO buying $2M open market = strong signal (they already have stock options, buying more means they're bullish)
- CEO selling = could be divorce, house purchase, planned program, taxes — meaningless

**CIK lookup:** `https://www.sec.gov/cgi-bin/browse-edgar?company={name}&CIK=&action=getcompany`
Or use Alpaca symbol → company name → search EDGAR for CIK.

**Universe coverage:** Pre-build a mapping of top 500 tradeable symbols → CIK numbers.
Cache CIK map permanently (changes rarely). Check Form 4s daily.

---

### Signal 3: FRED Macro Regime Modifier ✅ IMPLEMENT

**Source:** `https://fred.stlouisfed.org/graph/fredgraph.csv?id={SERIES}`
**API key:** None needed. Federal Reserve official data. Completely free.
**File to modify:** `backend/services/brain/regime.py`

**How it works:**
```python
# Pull 3 series once per day (cache 24h):
# UNRATE  — Unemployment Rate (monthly)
# PAYEMS  — Non-Farm Payrolls total (compute monthly change)
# FEDFUNDS — Fed Funds Rate (monthly)

# Compute 3-month trend for each:
unemp_score = -1 if rising >0.3% over 3mo else +1 if falling >0.3% else 0
jobs_score  = +1 if avg monthly additions >150K else -1 if <75K else 0
fed_score   = +1 if rate falling (cutting) else -1 if rising (hiking) else 0
macro_total = unemp_score + jobs_score + fed_score  # range: -3 to +3
```

**Integration with existing regime:**
```python
# Current regime: bull/bear/chop from VIX + price momentum
# Macro modifier adjusts CONFIDENCE, not the regime itself:
# macro_total >= +2 → boost regime confidence by 15%
# macro_total <= -2 → reduce regime confidence by 15%, reduce capital mult by 0.1
# Single bad data point (like yesterday's jobs report) → doesn't move 3-month trend → regime unchanged
```

**Why this is better than reacting to single readings:**
- Yesterday's employment drop was one data point. Unemployment is still 4.3% stable.
- 3-month trend hasn't changed. Regime stays bull.
- Only sustained deterioration (3+ months rising unemployment) triggers regime shift.

**Current macro signal (as of 2026-06-07):**
- Unemployment: STABLE (4.3%, no trend) → 0
- Jobs: MODERATE (~172K/month avg) → 0
- Fed: CUTTING (3.64% → 3.63%) → +1
- Total: +1 → MACRO NEUTRAL, no regime change

---

### Signal 4: Barchart Unusual Options Flow ✅ IMPLEMENTED (Session 6)

**Source:** `https://www.barchart.com/proxies/core-api/v1/options/get?unusual=1&...`
**API key:** None needed. Session cookie scraping — no account required.
**File created:** `backend/services/brain/connectors/barchart_options.py`
**Replaces:** `unusual_whales.py` (which used raw Alpaca OI/volume, no unusual filter)

**Why better than unusual_whales.py:**
- Barchart pre-filters for unusual activity using vol/OI ratio
- QQQ Call 1,589× ratio = volume is 1,589× open interest = brand new aggressive positioning
- Old code just compared total call OI vs put OI — not the same thing

**Authentication pattern (tested, working):**
```python
# Step 1: GET main page → captures XSRF-TOKEN and laravel_session cookies
# Step 2: URL-decode XSRF token: urllib.parse.unquote(cookie)
# Step 3: API call with decoded token as X-XSRF-TOKEN header
```

**Signal thresholds:**
```python
vol/OI > 50x → "very unusual" → call: +18 pts / put: -18 pts
vol/OI > 10x → "unusual"      → call: +10 pts / put: -10 pts
```

**Filters applied:**
- `volume >= 1,000` (ignore micro-volume noise)
- Excludes SPY/QQQ/IWM/VIX/GLD/TLT/EEM (macro hedging, not stock signal)
- Per symbol: keeps strongest signal across all strikes/expiries

**Fallback:** If Barchart fails (rate limit, session expired), falls back to `unusual_whales.py`

**Today's sample data (2026-06-06):**
- QQQ Call $725 exp 06/08: vol/OI = 1,589× — extremely unusual
- STM Call $100 exp 10/16: vol/OI = 669× — individual stock unusual bullish flow
- NVDA Call $210 exp 06/08: vol/OI = 117× — unusual call positioning

---

### Signal 5: Finnhub Analyst Revisions (TEST FIRST, then implement)

**Source:** `https://finnhub.io/api/v1/stock/price-target?symbol={ticker}&token={key}`
**API key:** Sign up free at finnhub.io — get free developer token
**Cost:** Free tier — unlimited calls but rate limited
**Status:** Sandbox token in circulating script (`sandbox_c8m9bca201qio9kv9ngg`) is invalid/expired

**What it gives:**
```json
{
  "targetHigh": 260.0,
  "targetLow": 150.0,
  "targetMean": 210.0,
  "targetMedian": 207.5,
  "lastWeek": 200.0  ← compare to targetMedian
}
```

**Signal logic:**
```python
# targetMedian > lastWeek → analysts raised target → +15 pts conviction
# targetMedian < lastWeek → analysts lowered target → -15 pts conviction
# This IS the revision direction signal FMP paywalls
```

**Before implementing:** Sign up for free Finnhub account, test endpoint returns valid data for AAPL/NVDA/MSFT.
Then add `FINNHUB_API_KEY` to Railway env and implement in `connectors/fmp.py` (replace yfinance fallback).

---

### Session 6 implementation — COMPLETED ✅

All implemented in-session:

| Step | Status | Detail |
|---|---|---|
| Add `FMP_API_KEY` to Railway | ⏳ Manual | Add `FMP_API_KEY=KloIwZY8s1YC0qceIgM4D1Fm1Vot1Np5` in Railway dashboard |
| Create `fmp_earnings.py` | ✅ Done | FMP stable API, 21-day window, +12/-12 boost, universe injection |
| Create `sec_insider.py` | ✅ Done | EDGAR Form 4, P-code only, +15/>$500K +8/>$100K, universe injection |
| Create `barchart_options.py` | ✅ Done | vol/OI ratio filter, +18/+10 unusual flow, replaces `unusual_whales.py` |
| Modify `signals.py` | ✅ Done | Added earnings_surprise, insider_buy, barchart options_flow signals |
| Modify `alpaca_service.py` | ✅ Done | Universe injection from FMP + SEC insider buys |
| Modify `regime.py` | ✅ Done | FRED macro modifier (UNRATE/PAYEMS/FEDFUNDS, 3-month trends) |
| Finnhub analyst revisions | ⏳ Next | Sign up free at finnhub.io, get token, test AAPL endpoint |

**DB migrations needed:** None — all signals modify scoring and universe, no new tables required.

**One manual step required before deploy:** Add `FMP_API_KEY=KloIwZY8s1YC0qceIgM4D1Fm1Vot1Np5` to Railway environment variables.

---

## Session 7 — What to Do Next ⬅️ START HERE

### Step 0 — Before anything else (Railway dashboard, 2 minutes)
Add these env vars or new signals silently return 0:
- `FMP_API_KEY` = [check your FMP account dashboard]
- `FINNHUB_API_KEY` = [check your Finnhub account dashboard]
- **Remove the API key that was in this plan file** — it was committed to git. Rotate it in FMP dashboard and use the new one.

---

### Step 1 — Fix SEC insider proactive scan (HIGH — currently broken)

**Bug:** `get_universe_additions()` in `sec_insider.py` returns `_inject_until` which is only populated inside `get_insider_signal()`. That means SEC insider injects 0 symbols unless a stock is already being scored — defeating the entire purpose.

**Fix needed in `sec_insider.py`:** Add a daily batch scan that proactively fetches recent Form 4 filings for top 500 symbols, independent of the per-symbol signal path.

```python
# New function to add:
def run_daily_insider_scan(symbols: list[str]) -> None:
    """
    Called once at market open. Scans top symbols for insider buys
    and populates _inject_until so get_universe_additions() returns real data.
    Rate limit: ~0.2s per symbol → 500 symbols = ~100 seconds. Run async or in thread.
    """
    for sym in symbols:
        get_insider_signal(sym)  # populates _inject_until as side effect
        time.sleep(0.2)         # EDGAR rate limit

# Call this from trading_engine.py at startup / daily reset:
# from services.brain.connectors.sec_insider import run_daily_insider_scan
# threading.Thread(target=run_daily_insider_scan, args=(top_500_symbols,), daemon=True).start()
```

Top 500 symbols source: use the existing `get_tradeable_universe()` output from previous day, extended with S&P 500 list from `https://en.wikipedia.org/wiki/List_of_S%26P_500_companies` (free, no API).

---

### Step 2 — Wire FRED confidence to actual position sizing (MEDIUM)

**Bug:** FRED adjusts `RegimeResult.confidence` but `_regime_capital_mult` in `trading_engine.py` is computed only from `regime` + `vix_level` — confidence is stored but not used for sizing.

**Fix needed in `trading_engine.py`** (around line 530, after `_last_regime_confidence = _brain_regime.confidence`):

```python
# After computing _regime_capital_mult from regime/vix:
# Apply FRED confidence modifier to actual capital multiplier
if _brain_regime.confidence >= 0.80:   # FRED boosted confidence
    _regime_capital_mult = min(1.0, _regime_capital_mult + 0.10)
elif _brain_regime.confidence <= 0.50:  # FRED reduced confidence
    _regime_capital_mult = max(0.20, _regime_capital_mult - 0.10)
```

This makes FRED actually affect trade sizing, not just logging.

---

### Step 3 — Fix FMP negative EPS estimate exclusion (MEDIUM)

**Bug:** `fmp_earnings.py` line 109 skips `eps_estimate < 0`. Companies beating a loss estimate (e.g. -$0.50 est → -$0.10 actual = 80% beat) are excluded. These are often high-beta post-earnings drift candidates.

**Fix in `fmp_earnings.py`:**
```python
# Replace:
if eps_estimate == 0 or eps_estimate < 0:
    continue

# With:
if eps_estimate == 0:
    continue
# For negative estimates, beat_pct still works because we use abs(eps_estimate)
# A company going from -$0.50 loss to -$0.10 loss = 80% improvement = valid signal
```

---

### Step 4 — Fix Barchart failure silent zero (MEDIUM)

**Bug:** On fetch failure, `_cache_fetched_at = time.time()` marks empty cache as fresh — universe gets zero Barchart injections for 15 min silently. Scoring falls back to `unusual_whales.py` but universe expansion has no fallback.

**Fix in `barchart_options.py`:**
```python
# In _refresh_cache(), on failure:
except Exception as e:
    logger.warning("Barchart unusual options: %s", e)
    # Do NOT update _cache_fetched_at on failure — let next cycle retry
    # _cache_fetched_at = time.time()  ← remove this line
    return
```

Also add fallback in `alpaca_service.py` Barchart injection block:
```python
# If _bc_syms() returns empty AND _cache_fetched_at is stale → use previous cache
```

---

### Step 5 — Missed Trades Tracker (NEW FEATURE)

**What it does:** Tracks stocks that were close to trading but didn't (score 35-54), then checks their actual price movement 1h/EOD/next-day. Shows which signals were the marginal ones. Helps calibrate thresholds over time.

**Already have:** `blocked_trades` table tracks trades blocked by circuit breaker/regime/other hard rules.
**What's missing:** Near-miss tracking for stocks that *scored* but not enough (35–54).

**Backend implementation (3 files):**

1. **`db.py`** — add `log_near_miss()` and `update_near_miss_prices()` and `get_near_misses_report()`
2. **`signals.py`** — after scoring, if `35 <= score < 45`: call `log_near_miss(symbol, score, breakdown, suggested_action, price)`
3. **`trading_engine.py`** — background worker (same pattern as blocked_trades worker) checks price at +1h, EOD, next day and calls `update_near_miss_prices()`
4. **`routers/performance.py`** — add `GET /api/performance/near-misses` endpoint

**DB migration needed:**
```sql
CREATE TABLE near_miss_log (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(10),
    score INTEGER,
    breakdown JSONB,               -- full signal breakdown: {rs: 15, macd: 10, options_flow: -18, ...}
    suggested_action VARCHAR(10),  -- "buy" | "short" | "skip"
    price_at_skip NUMERIC,
    price_1h NUMERIC,
    price_eod NUMERIC,
    price_next_day NUMERIC,
    hypothetical_pnl_pct NUMERIC,  -- what % gain/loss if we had traded
    timestamp TIMESTAMPTZ DEFAULT NOW()
);
```

**API response shape (`GET /api/performance/near-misses`):**
```json
{
  "summary": {
    "total_near_misses": 47,
    "would_have_been_profitable": 28,
    "accuracy_if_traded": "59.6%",
    "avg_hypothetical_pnl": "+3.2%",
    "threshold_verdict": "Current threshold (45) is close to optimal — lowering to 40 would add +2.1% avg PnL"
  },
  "top_missed_signals": [
    {"signal": "options_flow", "times_was_deciding_factor": 12, "avg_pnl_when_deciding": "+4.8%"},
    {"signal": "regime", "times_was_deciding_factor": 8, "avg_pnl_when_deciding": "-1.2%"}
  ],
  "recent": [
    {"symbol": "MU", "score": 42, "price_at_skip": 864, "price_eod": 750, "hypothetical_pnl": "-13.2%", "was_right_to_skip": true}
  ]
}
```

**iOS UI — Near-Miss Tracker card (add to `AnalyticsView.swift`):**

New card `G` in `AnalyticsView.swift` (after the existing 6 cards A–F):

```
┌─────────────────────────────────────────┐
│ 🎯 Near Misses — Did We Miss Good Trades?│
│                                          │
│  47 near-misses last 30 days             │
│  28 would have been profitable (59.6%)   │
│  Avg hypothetical gain: +3.2%            │
│                                          │
│  Deciding signal:                        │
│  options_flow  12x  avg +4.8% ████████  │
│  regime        8x   avg -1.2% ███       │
│  insider_buy   4x   avg +6.1% ██████████│
│                                          │
│  Threshold verdict:                      │
│  ✅ Current (45) looks optimal            │
│  [View all near misses →]                │
└─────────────────────────────────────────┘
```

**Swift model additions needed:**
- `NearMissSummary` struct in `Performance.swift`
- `getNearMisses()` call in `APIService.swift`
- Card view `NearMissCard` in `AnalyticsView.swift`

**Why this matters:** After 60 days of paper trading you'll have ~500+ near-misses. The `top_missed_signals` array tells you exactly which signal is being too conservative. If `options_flow` keeps blocking profitable longs, maybe -18 is too harsh. If `regime` keeps saving you from bad trades, the regime penalty is correctly sized. This turns the system into a self-improving loop.

---

### Step 6 — Sprint Review Tracker (NEW FEATURE — Daily + Weekly)

**Concept:** End-of-day and end-of-week automated report that answers:
- "What were the biggest market moves today — did Kova capture them?"
- "What did Kova trade well this week — which signals are working?"
- "If Kova had bought every top-10 gainer at open, how much more would the portfolio be worth?"

This is **different from Near-Miss Tracker (Step 5)**:

| Near-Miss Tracker | Sprint Review |
|---|---|
| Stocks scoring 35–54 (almost traded) | ALL significant market movers — regardless of whether Kova ever looked at them |
| "Were we right to skip?" | "What did the whole market do vs what we captured?" |
| Signal calibration tool | Strategy validation + opportunity cost measurement |
| Real-time, logged during trading | Runs once at 4:30 PM ET daily, once on Friday EOD |

---

**Three sections of the Sprint Review:**

**Section 1 — Market Opportunity Map (daily)**
- Pull top 20 gainers + top 20 losers at EOD via Alpaca screener
- For each: classify into one of four buckets:
  - `CAPTURED` — Kova traded it and made money ✅
  - `IN_UNIVERSE_SKIPPED` — was in our universe, scored below 45, skipped
  - `IN_UNIVERSE_WRONG` — was in our universe, we shorted when it went up (or vice versa)
  - `MISSED_ENTIRELY` — never entered our universe at all
- Calculate: "opportunity capture rate" = CAPTURED / total top-20 gainers

**Section 2 — Hypothetical Portfolio (daily)**
- Simulate: if Kova had bought top 10 gainers at 9:35 AM open and sold at 3:55 PM close
- Simulate: if Kova had shorted top 10 losers at 9:35 AM and covered at 3:55 PM
- Compare actual Kova P&L vs this hypothetical — gives "ceiling" for what was available
- NOT a benchmark we try to beat — it's a sanity check ("market was up 3%, Kova made 0.8% — was anything available?")

**Section 3 — What's Working (weekly, Friday EOD)**
- Which signals triggered the most profitable completed trades this week?
- Signal win rate this week: `{options_flow: 7/10 profitable, earnings_surprise: 3/3, insider_buy: 2/2}`
- "Continue" list: signals with >60% win rate on trades they influenced
- "Review" list: signals with <40% win rate (maybe tuning needed)
- Regime accuracy: "Bull regime called correctly X/Y days (regime said bull, market was up)"

---

**Backend implementation:**

**New file: `backend/services/brain/sprint_review.py`**
```python
# Runs at 4:30 PM ET daily via APScheduler (already used for other EOD tasks)
# Also runs full weekly summary on Fridays

def run_daily_sprint_review():
    """Fetch EOD movers, classify against our universe + trades, store to DB."""
    pass

def run_weekly_sprint_review():
    """Aggregate daily reviews into weekly signal performance summary."""
    pass
```

**DB tables needed:**
```sql
-- Daily snapshot of market movers vs Kova's activity
CREATE TABLE sprint_review_daily (
    id SERIAL PRIMARY KEY,
    review_date DATE,
    top_gainers JSONB,   -- [{symbol, open, close, pct_gain, kova_bucket, kova_pnl}]
    top_losers  JSONB,   -- [{symbol, open, close, pct_loss, kova_bucket, kova_pnl}]
    opportunity_capture_rate NUMERIC,  -- % of top gainers Kova caught
    hypothetical_long_pnl NUMERIC,     -- if bought all top-10 gainers
    hypothetical_short_pnl NUMERIC,    -- if shorted all top-10 losers
    actual_kova_pnl NUMERIC,
    missed_entirely_count INTEGER,     -- movers Kova never even looked at
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Weekly signal performance summary
CREATE TABLE sprint_review_weekly (
    id SERIAL PRIMARY KEY,
    week_ending DATE,
    signal_performance JSONB,  -- {options_flow: {wins:7, losses:3, avg_pnl:+2.1%}, ...}
    regime_accuracy JSONB,     -- {bull: {called:5, correct:4}, bear: {...}, chop: {...}}
    continue_signals TEXT[],   -- signals working well
    review_signals TEXT[],     -- signals underperforming
    summary_text TEXT,         -- auto-generated narrative
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

**New endpoints:**
- `GET /api/performance/sprint-review/daily?date=2026-06-07` — single day report
- `GET /api/performance/sprint-review/weekly?week=2026-06-07` — week ending this date
- `GET /api/performance/sprint-review/latest` — most recent daily + weekly

---

**iOS UI — Sprint Review screen (new tab or section in AnalyticsView)**

New card `H` in `AnalyticsView.swift`:

```
┌──────────────────────────────────────────┐
│ 📊 Sprint Review — Today                 │
│                                          │
│  Market top movers: 20 gainers, 20 losers│
│  Kova captured:  4 of top 20 gainers ✅  │
│  Missed entirely: 11 (never in universe) │
│  In universe, skipped: 5                 │
│                                          │
│  Hypothetical ceiling:                   │
│  Top-10 longs:  +6.8% today             │
│  Top-10 shorts: +4.2% today             │
│  Kova actual:   +1.3% today             │
│                                          │
│  [View weekly →]                         │
└──────────────────────────────────────────┘

Weekly view (tap "View weekly →"):
┌──────────────────────────────────────────┐
│ 📊 Sprint Review — Week of Jun 2–7       │
│                                          │
│  Signals working well ✅                 │
│  options_flow    7/10 trades profitable  │
│  insider_buy     2/2  trades profitable  │
│  earnings_surp.  3/4  trades profitable  │
│                                          │
│  Signals to review ⚠️                   │
│  barchart_short  1/4  trades profitable  │
│                                          │
│  Regime accuracy: Bull 4/5 days correct  │
│  Opportunity capture: 22% of top movers  │
└──────────────────────────────────────────┘
```

**Swift files to create/modify:**
- `SprintReview.swift` — new models: `DailySprintReview`, `WeeklySprintReview`, `SignalPerformance`
- `APIService.swift` — add `getSprintReviewLatest()`, `getSprintReviewWeekly()`
- `AnalyticsView.swift` — add Card H (`SprintReviewCard`)

**Data source for top movers:** Alpaca Screener API (`get_market_movers`) — already used in `get_tradeable_universe()`. At EOD, re-run with `top=20` to get the day's actual biggest movers. No new API needed.

---

### Step 7 — Google Finance
**Don't implement.** No official API. Scraping violates ToS and breaks constantly. yfinance (already in use) provides equivalent data from Yahoo Finance reliably. Nothing Google Finance offers that we don't already have.

---

### Step 7 — Medium term
**Phase 2 — Backtest 2020-2024** (once ~1 week of paper trading data exists)
- Extend `backend/services/brain/backtest.py` to loop 2020-2024 using yfinance
- Run ablation tests (toggle exit rules off one at a time)
- Output: Year / Trades / Win% / AvgWin / AvgLoss / Sharpe / MaxDD table

Then: Let paper trading run until **Day 60 (~2026-08-06)** and run Phase 3 queries.

## How to Start a New Session

Paste this at the start of the new conversation:
> "I'm working on the Kova AI trading app. Read this file first — it has full context of everything done and what's planned next: `/Users/siddheshdandekar/Documents/Siddhesh Playground/trading-app/KOVA_IMPLEMENTATION_PLAN.md`"
