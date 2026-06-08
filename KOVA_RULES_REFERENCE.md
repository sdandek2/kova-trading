# KOVA — Rules Reference
## Read this first in every new session.

**Project path:** `/Users/siddheshdandekar/Documents/Siddhesh Playground/trading-app`
**Backend:** Python FastAPI on Railway
**iOS:** Swift (Xcode), Kova.xcodeproj
**DB:** PostgreSQL on Railway
**Broker:** Alpaca (paper → live)
**AI:** Claude Sonnet (primary), Gemini Flash (India/staging)

---

## Current State (as of 2026-06-08)

- ✅ Paper trading live on Railway
- ✅ Signal weights self-tuning weekly (sprint review)
- ✅ Connector health monitoring + iOS alerts
- ✅ Signal Intelligence + Connector Health on iOS (AI Agent tab)
- ✅ DEV → STAGING → PROD architecture designed
- ✅ India book designed (Zerodha, not yet built)
- ⏳ Month 1 improvements not yet started (see Blueprint)
- ⏳ India: STAGING setup pending
- 📅 Day 30 go-live decision: ~2026-07-07

---

## Non-Negotiable Rules

### Trading Logic
- **Never block trades — only size them down** (soft gates, always)
- **AvgWin must stay > AvgLoss** — never cut winners to inflate win rate
- **Backtest first** for any structural change (regime logic, MACD, min_score)
- **Parameter drift is fine** — auto-tuning within guardrails is always OK
- **Brain has fallback to claude_service** — bot never stops trading on errors

### Key Hardcoded Values (don't change without backtest)
| Parameter | Value | Reason |
|---|---|---|
| max_position_pct | 15% | Halved from 30% to reduce loss magnitude |
| min_confidence | medium | Stops forcing speculative trades |
| stop_loss | 4% aggressive | Tighter = smaller losses faster |
| daily_circuit_breaker | 4% | Halts buys early on bad days |
| Half-Kelly sizing | 50% | Full Kelly too aggressive |
| MACD > 0 required | leveraged ETFs only | Prevents entering 3× on negative momentum |
| Leveraged ETFs | blocked in non-bull | Prevents decay in chop/bear |

### Deployment Rules
- **Nothing to prod without 14-day staging proof**
- **india_enabled=False default** — India code inert until explicitly flipped
- **feature/* → staging → main** promotion gate always applies
- **Never amend commits** — always new commit if hook fails
- **Validate Xcode pbxproj with `plutil -lint`** before committing

### Capital Rules
- Day 30 (~2026-07-07): win rate >60% → go live $10k
- Day 45 (~2026-07-22): still >60% → add $15k (total $25k)
- Day 60 (~2026-08-06): full signal SQL review, scale decision
- Never deploy full capital in one shot

---

## Architecture — How the Bot Works

```
Every 10 minutes (market hours):
  1. Alpaca → account, positions, universe snapshot
  2. regime.py → bull / bear / chop / volatile (SPY + VIX + breadth)
  3. rs_ranking.py → rank all stocks vs SPY
  4. kelly.py → load trade history for sizing
  5. signals.py → score every stock 0-100 → top candidates
  6. ai_brain.py → Claude evaluates top candidates → 1-3 decisions
  7. trading_engine → execute orders
  8. trading_engine → manage positions (trailing stops, scale-outs)
  ↓ FALLBACK: if brain fails → claude_service.analyze_and_decide()
```

---

## Key File Locations

| What | Where |
|---|---|
| Trading cycle | `backend/services/trading_engine.py` → `run_trading_cycle()` |
| AI decisions | `backend/services/brain/ai_brain.py` → `decide()` |
| Signal scoring | `backend/services/brain/signals.py` → `score_universe()` |
| Regime detection | `backend/services/brain/regime.py` → `detect_regime()` |
| Position sizing | `backend/services/brain/kelly.py` → `kelly_size()` |
| Options engine | `backend/services/brain/options_engine.py` |
| Macro context | `backend/services/macro.py` |
| Connector health | `backend/services/connector_health.py` |
| Sprint review | `backend/services/brain/sprint_review.py` |
| DB functions | `backend/services/db.py` |
| iOS views | `ios/TradingApp/Sources/Views/` |
| iOS AI tab | `Views/AI/AIView.swift` |
| iOS insights | `Views/Insights/InsightsView.swift` |

---

## Signal Weights — Auto-Tuning Rules
- Adjusted **weekly on Sunday** via sprint review
- ±2 per week, floor=1, cap=default×1.50
- Win rate >70% last 30 trades → boost; <40% → reduce
- Sample count <15 trades → no adjustment (not enough data)
- History tracked in `signal_weight_history` table
- Connector critical failure → weight set to 1 automatically

---

## Connector Health — Thresholds
- >80% failure rate 24hrs → WARNING notification
- >80% failure rate 72hrs → CRITICAL + signal weight set to 1
- 0 calls in 48hrs → SILENT alert (check wiring)
- `no_key` status → excluded from failure % (expected, not an error)
- Alpaca connectors skip silent check on weekends

---

## Go-Live Rules
- Day 30 >60% win rate consistently + no Railway crashes + MaxDD <10% → $10k live
- Day 45 still >60% → add $15k (total $25k)
- Day 60 → full SQL review, options firing check, scale decision

---

## Next-Gen Blueprint Summary
Full detail in `KOVA_NEXTGEN_BLUEPRINT.md`. Key points:

**4 Books:**
- Book 1: US Equities (9:30 AM–4:00 PM ET) — target 85-90% win rate
- Book 2: US Options (same hours) — conviction-based routing
- Book 3: India/Zerodha (11:45 PM–6:00 AM ET) — zero overlap with US
- Book 4: Crypto/24/7 — BTC + ETH, 10-15% portfolio max

**Month 1 Priority (start now):**
1. Confluence position sizing (soft gate — never blocks)
2. Intraday entry windows (10 AM–3:30 PM only)
3. Trailing stops (Alpaca native)
4. Drawdown laddering (replace single circuit breaker)

**India Priority:**
1. Build PaperBrokerIndia simulator
2. Set up STAGING Railway service
3. Nifty 50 universe + yfinance data
4. 30-day paper proof → promote to prod

---

## Environment Variables

```bash
# Railway PROD
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_BASE_URL=https://api.alpaca.markets   # live
ANTHROPIC_API_KEY=...
FMP_API_KEY=...
FINNHUB_API_KEY=...
DATABASE_URL=...
ENVIRONMENT=production
BOOK_MODE=live
INDIA_ENABLED=false   # flip when ready

# Railway STAGING (second service, same repo)
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ENVIRONMENT=staging
BOOK_MODE=paper
INDIA_ENABLED=true

# Local DEV
ENVIRONMENT=development
BOOK_MODE=paper
INDIA_ENABLED=true
```

---

## Backtest Reference
- File: `backend/backtest_phase2.py`
- Latest run: 2026-06-07, 38 symbols, avg Sharpe 1.52
- Compounded 2020-2026: $100k → ~$156k (+56%)
- 2022 is the problem year (48.6% win rate) — fix: regime-conditional min_score (not yet implemented)
- MACD exit: keep enabled (ablation inconsistent across universes)
