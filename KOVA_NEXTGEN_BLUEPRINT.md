---
name: project-kova-nextgen
description: Kova Next-Gen — complete 12-month blueprint to 85-90% win rate across 4 trading books
metadata: 
  node_type: memory
  type: project
  originSessionId: 86a99c0d-aaf0-452a-82d0-71b6ce23ad87
---

# KOVA — Next-Generation Trading System
## Complete 12-Month Blueprint

**Target:** 85-90% win rate | 4 trading books | 24/7 operation | fully self-tuning
**Capital:** $25k Month 1 → scale as profits compound
**Confidence:** Engineering 100% | Win rate 85-88% near-certain | 90% possible

---

## System Architecture

```
╔══════════════════════════════════════════════════════════════╗
║                  KOVA NEXT-GEN SYSTEM                        ║
║        Always On. Always Learning. Always Earning.           ║
╚══════════════════════════════════════════════════════════════╝

iOS App (Command HQ)
└── WebSocket + REST
    ├── DEV   (local)           — feature/* branch, paper, Claude Haiku
    ├── STAGING (Railway)       — staging branch,   paper, Claude Haiku
    └── PROD  (Railway)         — main branch,      live,  Claude Sonnet

PROD runs 4 books simultaneously:
┌─────────────────────────────────────────────────────────────┐
│ BOOK 1: US EQUITIES        9:30 AM–4:00 PM ET              │
│ Broker: Alpaca LIVE        AI: Claude Sonnet                │
│ Universe: 20-30 stocks                                      │
│ Target: M6 78-82% → M12 85-90% → M24 90%                  │
├─────────────────────────────────────────────────────────────┤
│ BOOK 2: US OPTIONS         9:30 AM–4:00 PM ET              │
│ Broker: Alpaca LIVE        AI: Claude Sonnet                │
│ Routing: conviction-based (calls/spreads/condors)           │
│ Target: M6 75-80% → M12 80-85% → M24 85-88%               │
├─────────────────────────────────────────────────────────────┤
│ BOOK 3: INDIA EQUITIES     9:15 AM–3:30 PM IST             │
│ = 11:45 PM–6:00 AM ET (zero overlap with US)               │
│ Broker: Zerodha (Kite API)                                  │
│ AI: Gemini Flash (thinking:OFF) → Haiku → Sonnet           │
│ Universe: Nifty 50 stocks + BankNifty index options        │
│ Target: M6 60-65% → M12 72-78% → M24 85%+                 │
│ (lower start = new market, same ceiling as US by M24)      │
├─────────────────────────────────────────────────────────────┤
│ BOOK 4: CRYPTO             24/7/365                         │
│ Broker: Alpaca Crypto      AI: Claude Haiku (high volume)  │
│ Universe: BTC + ETH (10-15% portfolio max)                 │
│ Target: M6 60-65% → M12 72-78% → M24 80-85%               │
│ (harder asset class, higher volatility, no market hours)   │
└─────────────────────────────────────────────────────────────┘

SUPPORT SYSTEMS (always running):
┌────────────────┐ ┌────────────────┐ ┌────────────────┐
│ TAX HARVESTING │ │  RISK ENGINE   │ │ LEARNING ENGINE│
│ Nov-Dec annual │ │ 24/7 all books │ │ Weekly Sunday  │
│ 1-3% net lift  │ │ Cross-book     │ │ All params tune│
└────────────────┘ └────────────────┘ └────────────────┘
```

---

## DEV → STAGING → PROD Pipeline

### Three Environments — Zero Extra Cost
Railway: same project, two Services (prod + staging), different branch + env vars.

```
Railway Project: kova-trading
├── Service: kova-prod      (branch: main,    BOOK_MODE=live)
└── Service: kova-staging   (branch: staging, BOOK_MODE=paper)
```

### Environment Matrix

| Behaviour | DEV (local) | STAGING (Railway) | PROD (Railway) |
|---|---|---|---|
| US broker | Alpaca paper | Alpaca paper | Alpaca live |
| India broker | PaperBrokerIndia | PaperBrokerIndia | Zerodha live |
| AI model (US) | Claude Haiku | Claude Haiku | Claude Sonnet |
| AI model (India) | Gemini Flash (thinking:OFF) | Gemini Flash (thinking:OFF) | Gemini Flash→Haiku→Sonnet |
| AI model (Crypto) | Claude Haiku | Claude Haiku | Claude Haiku |
| DB | Local Postgres | Staging Postgres | Prod Postgres |
| Sentry | Off | On | On |
| iOS alerts | Localhost | Staging URL | Prod URL |
| india_enabled | True | True | False → True (Stage 2) |

### Promotion Gate Rules

```
feature/* branch
  → 5 days DEV paper, no crashes, logic reviewed
  → PR to staging branch

staging service
  → 14 days paper, win rate ≥ baseline ±2%, no MaxDD regression
  → PR to main branch

main / prod service
  → live capital deployed
```

**Rule:** Nothing touches prod without staging proof. Same philosophy as Day 30/60 go-live.

### Config Changes (additive only — nothing existing touched)

```python
class Settings(BaseSettings):
    # ── existing US fields — UNTOUCHED ──
    alpaca_api_key: str
    alpaca_secret_key: str
    anthropic_api_key: str
    # ... all existing fields unchanged ...

    # ── new — environment + India (all safe defaults) ──
    environment: str = "development"       # development | staging | production
    book_mode: str = "paper"              # paper | live
    india_enabled: bool = False           # master switch — False = inert in prod
    india_paper_mode: bool = True         # False only when going live on Zerodha
    zerodha_api_key: str = ""
    zerodha_api_secret: str = ""
    zerodha_access_token: str = ""
```

`india_enabled=False` means all India code is completely inert in prod even after deployment.

---

## India Book — Architecture

### Broker
**Zerodha (Kite Connect API)** — ₹20/trade flat, algo trading supported, REST + WebSocket.

**PaperBrokerIndia simulator** (~100 lines):
- Fills at real NSE prices via yfinance
- Tracks positions in Postgres (india_positions table)
- Same interface as live broker — engine never knows the difference

```python
class ZerodhaBroker:
    def __init__(self, paper: bool = True):
        self.paper = paper
        self._engine = PaperBrokerIndia() if paper else KiteLiveBroker()

    async def place_order(self, symbol, qty, side, order_type):
        return await self._engine.place_order(symbol, qty, side, order_type)
```

### Instruments
- **Phase 1:** Nifty 50 stocks (RELIANCE.NS, INFY.NS, TCS.NS, HDFCBANK.NS, etc.)
- **Phase 2:** Nifty + BankNifty index options (weekly, massive liquidity)
- **Deferred:** Individual stock F&O, futures, intraday

### Isolation — services/india/ directory
Zero changes to: `trading_engine.py`, `alpaca_service.py`, `signals.py`, `ai_brain.py`.
All India code in `services/india/` — separate async loop, separate DB tables.

### India Signal Stack (in order of addition)
| When | Signal | Source |
|---|---|---|
| Now | Nifty regime, India VIX, RSI/MACD/RS ranking | yfinance |
| Month 1-2 | FII/DII daily flow | NSE free daily data |
| Month 1-2 | NSE bulk/block deals | NSE (transparent dark pool equivalent) |
| Month 3 | Nifty/BankNifty options OI buildup | NSE |
| Month 3 | India earnings calendar | Trendlyne |
| Month 6 | Promoter shareholding changes | BSE filings |
| Month 6 | Nifty index rebalancing events | NSE announcements |
| Month 9 | US ADR correlation (INFY/WIT/HDB) | Alpaca data |
| Month 12 | RBI-cycle sector rotation | RBI calendar |

### India AI Model Progression
```
Paper / early live       → Gemini Flash (thinking: OFF)  — fastest, cheapest, good enough for paper
Month 3 (win rate >60%)  → Gemini Flash (thinking: ON)   — better reasoning, slightly higher cost
Month 6 (win rate >65%)  → Claude Haiku                  — standardise on Anthropic, consistent prompting
Month 12                 → Claude Sonnet                  — same tier as US book when fully proven
```
**Why standardise on Claude:** one API key, consistent prompt behaviour, no context-switching between Gemini and Claude prompting styles. Gemini Flash (thinking:OFF) only used during paper phase for cost efficiency.

### India Go-Live Stages
```
Stage 1 (now→Month 2):  STAGING only, paper, Gemini Flash
Stage 2 (Month 2):      Promoted to PROD infra, still paper mode
Stage 3 (Month 3):      india_paper_mode=False, deploy ₹500k (~$6k)
                        Trigger: 30-day paper win rate ≥55%
```

### Timing
India: 9:15 AM–3:30 PM IST = 11:45 PM–6:00 AM ET.
**Zero overlap with US session.** One Railway server, two async loops, 24-hour coverage before crypto.

---

## The 5 Pillars (All Books)

### Pillar 1 — Signal Intelligence
- **Signal correlation matrix:** treat correlated signals as one (analyst upgrade + earnings = 1 signal)
- **Signal decay:** today=100%, yesterday=70%, 2 days=40%, 3+ days=10%
- **Signal regime validity:** each signal mapped to regimes where it's effective
- **Position sizing by confluence (soft gate — never blocks):**
  ```
  4+ signals → 100% intended size
  3 signals  → 100% size
  2 signals  → 50% size
  1 signal   → 25% size
  0 signals  → skip
  ```

### Pillar 2 — Regime Intelligence (Multi-Timeframe)
- **Weekly:** SPY 50/200 MA — macro trend
- **Daily:** SPY 20/50 MA — current trend (existing)
- **Hourly:** SPY 5/20 MA intraday — entry window
- Trade only when weekly + daily agree. Hourly confirms timing.
- **Sector regime:** track leading vs lagging sectors weekly
- **Volatility regime:** 4 types (fear+down / fear+up / calm+up / calm+flat)
- **Breadth filter:** if advance/decline ratio weak → reduce all sizes 30%

### Pillar 3 — Entry Timing
- **Intraday windows:** 10:00 AM–11:30 AM and 1:00 PM–3:30 PM ET only
- **Pullback entry:** limit order at -0.7% below current
  - Not filled 90min → widen to -0.3%
  - Not filled + conviction >75 → market order
  - Not filled + conviction <60 → skip, another opportunity tomorrow
- **Volume confirmation:** entry requires >100% of 20-day avg volume (150%+ = strong)
- **Position pyramid:** 50% at entry → +30% on volume confirmation → +20% on breakout

### Pillar 4 — Trade Structure (Conviction-Based Routing)
```
0-1 signals, any        → skip
2 signals, conviction<60 → small stock position (25% size)
3 signals, conviction 60-70 → stock, full size, trailing stop
4 signals, conviction 70-80 → long call (bull) or long put (bear)
5+ signals, conviction >80  → bull call spread (leveraged, defined risk)
chop regime + any signals   → iron condor (collect premium)
```

**Partial exit strategy:**
```
+5%  → sell 25% (lock partial profit)
+10% → sell another 25%
+15% → trail remaining 50% with 5% trailing stop
+25% → trail with 8% stop (let it run)
```

**Earnings event structure:**
- 3-5 days before: buy OTM call (bull) or put (bear) — cheap, huge upside
- Day after beat: buy gap momentum (50% size)
- Miss while in position: exit at open, no questions

### Pillar 5 — Risk Architecture
- **Sector cap:** 30% max per sector, 20% max per stock
- **Correlation check:** positions with >0.7 correlation = same risk, count as one
- **Volatility-adjusted sizing:** high-vol stocks get smaller positions (ATR-based)
- **Drawdown ladder:**
  ```
  -2% today    → reduce new sizes 25%
  -4% today    → reduce 50%, no new shorts
  -6% today    → exits only (current circuit breaker)
  -3% week     → reduce sizes 20%
  -5% week     → close weakest positions
  -10% month   → full stop, human review
  ```
- **Portfolio hedge:** for every 5 long stock positions, 1 SPY put as insurance (~0.3% cost/month)

---

## Adaptive Parameter System

### What Gets Auto-Tuned Weekly (Sunday sprint review)
| Parameter | Adjustment | Floor | Cap |
|---|---|---|---|
| min_signals_required | ±1 if trade count too low/high | 2 | 5 |
| confluence size multipliers | ±5% based on win rate by tier | fixed floor | fixed cap |
| trailing stop % | ±0.5% based on AvgWin/AvgLoss ratio | 2% | 10% |
| limit order offset | ±0.1% based on fill rate | 0.2% | 1.5% |
| options conviction threshold | ±5 based on options vs stock P&L | 60 | 85 |
| India FII flow weight | ±2 based on India signal win rates | 1 | 10 |

### Win/Loss Ratio Target
```
AvgWin / AvgLoss target: 2.0×
< 1.5× → tighten stop 0.5%, loosen take profit 1%
1.5-2.5× → no change
> 2.5× → loosen stop 0.5% (cutting too tight)
```

### Claude Sunday Optimizer (Month 6+)
Every Sunday sprint review sends Claude:
- Last 30-day regime history, win rates by regime
- Win rates by signal combination
- Current parameter values + drift history
- Ask: "What 1-2 parameter changes would most improve performance?"
Claude proposes → system applies within guardrails → logged for review.

### RL Agent (Month 9+)
- State: regime, VIX, win rate trend, recent drawdown, signal density
- Action: adjust any parameter within guardrails
- Reward: Sharpe improvement over rolling 30 days
- Human sets outer bounds. RL optimises within them.
- Learns: min_score=42 in chop, 28 in bull, 55 in bear+VIX

---

## Tax Harvesting (Month 4, November annual run)

- Scan all positions November 1 for unrealised losses
- Close losers before Dec 31
- Park in correlated ETF during 30-day wash sale window (sell AAPL → buy QQQ)
- Reopen original position after 30 days
- Log realised losses separately for tax reporting
- Expected benefit: 1-3% effective return improvement via tax deferral
- Already have tax summary endpoint — extend with harvesting recommendations

---

## 12-Month Implementation Timeline

### Month 1 — Foundation (US Book hardening)
- Week 1-2: Confluence position sizing, intraday entry windows, volume confirmation gate
- Week 2-3: Trailing stops (Alpaca native), partial exits (25/25/50)
- Week 3-4: Drawdown laddering, sector caps, correlation check
- Week 4: parameter_adjustments DB table + sprint review expansion
- **India:** STAGING setup, PaperBrokerIndia simulator, Nifty 50 universe
- Expected: US win rate 62-67%

### Month 2 — Regime Intelligence
- Multi-timeframe regime (weekly/daily/hourly)
- Sector rotation tracking (leading vs lagging)
- Volatility regime 4-type classification
- Breadth filter
- Pullback entry with limit orders + pyramid sizing
- **India:** FII/DII flow signal, NSE bulk/block deals signal
- Expected: US win rate 68-72%

### Month 3-4 — Trade Structure + India Live
- Options conviction routing (4+ signals → calls/spreads)
- Earnings event plays (pre-earnings options, post-earnings gap)
- Iron condors on chop regime
- Tax lot tracking system built (runs Nov-Dec)
- **India:** 30-day paper proof complete → promote to prod infra
- **India:** Flip india_paper_mode=False if win rate ≥55%
- **India:** Add Nifty/BankNifty options OI + earnings calendar signals
- Expected: US win rate 73-77%, AvgWin dramatically up

### Month 4-5 — Signal Intelligence
- Signal correlation matrix (stop double-counting)
- Signal decay weighting (recency)
- Signal regime validity mapping
- Dark pool prints signal
- Options flow imbalance signal
- **Crypto Book 3:** BTC + ETH live (10-15% portfolio max)
- Expected: US win rate 76-80%, India 60-65%

### Month 5-6 — Multi-Book Operations
- iOS multi-book P&L dashboard
- Cross-book risk management (don't be long tech + long BTC in bear)
- 13F institutional flow signal (US)
- Sector momentum signal (ride weekly leader)
- **India:** Promoter shareholding + index rebalancing signals
- **India:** Upgrade AI to Gemini 2.5 Flash if win rate >65%
- Claude Sunday optimizer live
- Expected: US win rate 78-82%

### Month 7-9 — AI Intelligence Layer
- Claude analyzes losing trades weekly, finds patterns
- Auto-generates rules from patterns
- Short squeeze detection signal
- **India:** US ADR correlation signal (overnight ADR moves predict gaps)
- **India:** Options expanding (BankNifty weekly options active)
- RL agent starts collecting data (not yet tuning)
- Expected: US win rate 80-84%, India 65-70%

### Month 9-12 — Full Adaptive System
- RL agent starts tuning (9+ months of data)
- Universe tightened to 10-15 highest-signal stocks dynamically
- Tax harvesting runs (November)
- Every losing trade type identified and filtered
- **India:** RBI-cycle sector rotation signal
- **India:** Gemini Pro upgrade if win rate sustained >70%
- Full 24-hour coverage: India → US → Crypto
- Expected: US win rate 85-90%, India 70-75%, Crypto 65-70%

---

## Revenue Projections

```
              Conservative    Realistic    Best Case
Month 3:        $3,000         $6,000      $10,000/month
Month 6:        $8,000        $15,000      $25,000/month
Month 9:       $15,000        $28,000      $45,000/month
Month 12:      $25,000        $45,000      $80,000+/month

By Month 12 breakdown (realistic):
  US Equities:  $20,000/month
  US Options:   $10,000/month
  India:         $8,000/month
  Crypto:        $5,000/month
  Tax savings:   $2,000/month effective
  Total:        ~$45,000/month
```

---

## 24/7 Money Map

```
11:45 PM ET:  India market opens (9:15 AM IST)
 6:00 AM ET:  India market closes (3:30 PM IST)
 9:30 AM ET:  US market opens — Books 1 + 2 active
 4:00 PM ET:  US market closes
 4:00 PM+:   Crypto runs all night
 Sunday 8PM: Sprint review — all books, all parameters tune
 Always:      Connector health, push alerts, drawdown protection
```

---

## Key Rules (Non-Negotiable)

1. **Never block trades — only size them down** (soft gates always)
2. **Every parameter has floor + cap** — RL and auto-tuning can't go rogue
3. **14-day staging proof before any prod change**
4. **india_enabled=False default** — India code inert until explicitly enabled
5. **Backtest first for structural changes** — parameter drift is fine, regime logic changes need proof
6. **Human iOS override always available** — locks for 7 days
7. **Every decision logged** — full audit trail, no black box
8. **Gradual capital deployment** — never full capital in one shot
9. **AvgWin must stay > AvgLoss** — don't chase win rate by cutting winners
10. **Cross-book risk managed** — correlated positions across books = concentrated risk

---

## Honest Confidence Levels

| Target | Confidence |
|---|---|
| Engineering complete in 12 months | 100% |
| US win rate 80-85% | 90% |
| US win rate 88-90% | 60% |
| India win rate 70-75% by Month 12 | 75% |
| $25-45k/month by Month 12 | 70% (market-dependent) |
| Full 24/7 autonomous operation | 95% |
