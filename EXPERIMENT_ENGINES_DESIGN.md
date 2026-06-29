# Experiment Engines — Design Document
**Version:** 1.1 | **Date:** 2026-06-21  
**Status:** Ready to build  
**Author:** Design session with Claude

---

## Overview

Three new paper-trading engines running as isolated experiments alongside the existing Lakshmi, Wheel, and PureAI systems. Goal: validate alternative entry strategies before committing real money.

| Engine | Strategy | Account | Mode |
|--------|----------|---------|------|
| Squeeze | Short squeeze detection | Paper (new) | Paper |
| Spillover | Sector earnings spillover | Paper (new) | Paper |
| Revision | Accelerating EPS beats | Paper (new) | Paper |

**All three are completely isolated from Lakshmi, Wheel, and PureAI.**

---

## Core Isolation Rules (Non-Negotiable)

1. Each engine has its own Alpaca paper account (own API key + secret)
2. Each engine creates its own Alpaca trading + data client — never imports from `alpaca_service.py`
3. No imports from `trading_engine.py`, `wheel_engine.py`, or `pureai_engine.py`
4. All 3 engines write only to `experiment_positions` table — never touch any existing table
5. FMP earnings cache (`fmp_earnings.py`) is read-only shared — engines call `get_earnings_signal()` and `get_universe_additions()` only, never `_refresh_cache()` directly
6. **Experiment engines do not make bulk yfinance calls** — NASDAQ API is primary for short interest (NASDAQ stocks). yfinance used only as fallback for NYSE stocks that pass the volume filter (typically 1-5 calls/day max, well below the bulk 429 threshold)
7. Each engine runs as an independent daemon thread — same pattern as `pureai_engine.py`

---

## Railway Environment Variables

Add these 6 new vars to Railway (same pattern as existing wheel/pureai vars):

```
ALPACA_SQUEEZE_KEY      = <new paper account API key>
ALPACA_SQUEEZE_SECRET   = <new paper account secret>

ALPACA_SPILLOVER_KEY    = <new paper account API key>
ALPACA_SPILLOVER_SECRET = <new paper account secret>

ALPACA_REVISION_KEY     = <new paper account API key>
ALPACA_REVISION_SECRET  = <new paper account secret>
```

All point to `https://paper-api.alpaca.markets`.

If any key pair is unset/empty, that engine's scheduler silently skips startup (same pattern as pureai_engine.py line 573).

---

## config.py Changes

Add to `Settings` class (after existing pureai vars):

```python
# Experiment engines — paper accounts
alpaca_squeeze_key: str = ""
alpaca_squeeze_secret: str = ""
alpaca_squeeze_base_url: str = "https://paper-api.alpaca.markets"

alpaca_spillover_key: str = ""
alpaca_spillover_secret: str = ""
alpaca_spillover_base_url: str = "https://paper-api.alpaca.markets"

alpaca_revision_key: str = ""
alpaca_revision_secret: str = ""
alpaca_revision_base_url: str = "https://paper-api.alpaca.markets"
```

---

## Database

### One New Table (shared across all 3 engines)

```sql
CREATE TABLE IF NOT EXISTS experiment_positions (
    id SERIAL PRIMARY KEY,
    engine VARCHAR(20) NOT NULL,          -- 'squeeze' | 'spillover' | 'revision'
    symbol VARCHAR(10) NOT NULL,
    entry_price FLOAT,
    entry_date TIMESTAMPTZ DEFAULT NOW(),
    shares INT,
    stop_price FLOAT,
    target_price FLOAT,
    status VARCHAR(20) DEFAULT 'open',    -- 'open' | 'closed' | 'stopped'
    exit_price FLOAT,
    exit_date TIMESTAMPTZ,
    realized_pl FLOAT,

    -- squeeze-specific (null for other engines)
    days_to_cover FLOAT,               -- from NASDAQ short interest API
    volume_ratio FLOAT,

    -- spillover-specific (null for other engines)
    trigger_symbol VARCHAR(10),
    trigger_beat_pct FLOAT,
    sector VARCHAR(50),

    -- revision-specific (null for other engines)
    beat_pct_prev FLOAT,
    beat_pct_curr FLOAT,
    acceleration FLOAT,

    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_experiment_engine ON experiment_positions(engine);
CREATE INDEX IF NOT EXISTS idx_experiment_status ON experiment_positions(status);
```

**Never touches:** `lakshmi_trades`, `wheel_positions`, `pureai_decisions`, `trade_decisions`, or any other existing table.

No additional DB tables needed. Short interest is fetched on-demand from the NASDAQ API at scan time (see Short Interest Data section).

---

## File Structure

```
backend/
  services/
    squeeze_engine.py       ← new
    spillover_engine.py     ← new
    revision_engine.py      ← new
  routers/
    experiments.py          ← new (single router for all 3 engines)
  data/
    sector_peers.json       ← new (static sector peer mapping)
  main.py                   ← add 3 scheduler starts (same pattern as pureai)

ios/TradingApp/Sources/
  Views/
    LabsView.swift          ← new (combined tab, engine picker)
  ViewModels/
    LabsViewModel.swift     ← new
  ContentView.swift         ← add LabsView as tab 2 (shift existing tabs)
```

---

## Engine 1 — Squeeze (`squeeze_engine.py`)

### What it does
Detects stocks where short sellers are mathematically forced to buy back shares, driving the price up.

### Logic

```
Step 1: Get all stocks with volume spike (Alpaca batch snapshot — own data client)
  - volume today > 2× 20-day average
  - price up > 3% today
  - price > $3 (not penny stock)
  → Typically 10-30 candidates

Step 2: Get short interest for each candidate
  Primary: NASDAQ API (no key, covers NASDAQ-listed stocks)
    GET api.nasdaq.com/api/quote/{symbol}/short-interest?type=SHORT_INTEREST&limit=1
    Returns daysToCover instantly
  Fallback for NYSE stocks: yfinance ticker.info['shortPercentOfFloat']
    Only called if NASDAQ API returns "not supported"
    Typically 1-5 NYSE stocks per day — well below 429 threshold
    0.5s sleep between yfinance calls
  If both fail → score on volume/price only, cap score at 60
  ~10-30 calls total across both sources, takes ~10-15 seconds

Step 3: Score each candidate 0–100
  daysToCover > 7           → +35 pts  (very hard to unwind)
  daysToCover > 5           → +25 pts  (strong squeeze fuel)
  daysToCover > 3           → +10 pts  (moderate)
  volume > 3× average       → +20 pts
  volume > 2× average       → +10 pts
  price up > 5% today       → +15 pts
  price up > 3% today       → +8 pts
  no short interest data    → score on volume/price only, cap score at 60
  score ≥ 70 → buy
```

### Position Management
- Max 2 open positions simultaneously
- Position size: 25% of account equity per trade
- Entry: limit order at ask price

**Exit conditions (first trigger wins):**
1. **Stop loss:** Hard stop -8% from entry (not trailing — squeezes are volatile, trailing stops out too early)
2. **Profit lock:** Once up +10%, switch to trailing stop of 6% from peak to protect gains
3. **Target hit:** +25% → exit immediately, don't get greedy
4. **Squeeze died:** Volume drops back below 1.5× average → squeeze is over, exit regardless of P&L
5. **Time limit:** 5 calendar days max hold — if nothing happened in 5 days, thesis is wrong, exit

### Schedule
- Runs daily at **9:50 AM ET** (Mon–Fri)
- Single scan, places orders, exits managed every 30 min via same scheduler loop

### Data Sources
| Data | Source | Extra API calls? |
|------|--------|-----------------|
| Price + volume | Alpaca batch snapshot (own data client) | Yes — own account, own limit |
| Short interest (daysToCover) | NASDAQ API per-symbol (10-30 calls/day, 0.3s apart) | Yes — no key, no rate limit observed |
| Account equity | Alpaca trading client (own account) | Yes — own account |

---

## Engine 2 — Spillover (`spillover_engine.py`)

### What it does
When a company beats earnings significantly, its sector peers almost always follow within days. Buy the peers before they move.

### Logic

```
Step 1: Check FMP earnings cache (already fetched by Lakshmi — zero extra calls)
  from services.brain.connectors.fmp_earnings import get_universe_additions, _earnings_cache
  Find earnings beats from last 48 hours where beat_pct > 10%

Step 2: For each trigger stock, find sector peers
  Use hardcoded sector_peers.json mapping
  Filter peers:
    not already moved > 5% since trigger date  → skip (too late)
    hasn't reported earnings yet               → skip (binary risk)
    avg daily volume > 300k                    → must be liquid
    price > $5                                 → no penny stocks
    no open position already                   → no duplicate

Step 3: Score peers
  Same sub-industry as trigger  → +30 pts
  Similar market cap (within 3×) → +20 pts
  RSI < 60 (not overbought)      → +15 pts
  Volume uptick today            → +15 pts
  score ≥ 60 → buy

Step 4: Place limit order at ask
```

### Sector Peer Mapping
Create `backend/data/sector_peers.json` — a static mapping of companies to their closest peers. Example:

```json
{
  "NVDA": ["AMD", "MRVL", "AVGO", "QCOM", "INTC"],
  "META": ["SNAP", "PINS", "GOOGL", "TTD"],
  "JPM":  ["BAC", "WFC", "GS", "MS", "C"],
  "AMZN": ["SHOP", "EBAY", "WMT", "TGT"]
}
```
~100-150 major companies mapped. Built once, updated rarely.

### Position Management
- Max 3 open positions simultaneously
- Position size: 20% of account equity
- Entry: limit at ask

**Exit conditions (first trigger wins):**
1. **Stop loss:** Hard stop -6% from entry
2. **Target hit:** +10% → exit
3. **Pre-earnings:** Always close 1 day before the peer's own earnings date — never hold through a binary event
4. **Trigger reversal:** If the original earnings-beat stock closes below its pre-earnings price, the sector thesis is dead → exit all spillover positions from that trigger immediately
5. **Time limit:** 14 days max hold

**Long only.** Short side (peers of earnings misses) to be added after long side is validated.

### Schedule
- Runs daily at **9:50 AM ET** (Mon–Fri)
- Checks for triggers from last 48 hours
- Position exits checked every 30 min

### Data Sources
| Data | Source | Extra API calls? |
|------|--------|-----------------|
| Earnings beats | FMP cache (shared, already fetched by Lakshmi) | **Zero** |
| Peer price + volume | Alpaca batch snapshot (own data client) | Yes — own account |
| Sector mapping | Local JSON file | Zero |

---

## Engine 3 — Revision (`revision_engine.py`)

### What it does
Find stocks where EPS beats are getting bigger quarter over quarter — accelerating positive surprise means analysts keep underestimating the company. Market eventually catches up.

### Logic

```
Step 1: From FMP earnings cache, find stocks with strong recent EPS beats
  Get all symbols where direction == 'beat' AND beat_pct > 15%
  (Zero extra FMP calls — same 21-day cache Lakshmi already fetches)
  → Typically 5-20 candidates per day

  NOTE: The shared FMP cache covers 21 days back only. Multi-quarter
  acceleration tracking is NOT used — it would require separate historical
  FMP calls per symbol which uses daily quota. Instead we focus on
  STRONG single-quarter beats (>15%) as the signal. This is sufficient
  for post-earnings drift capture.

Step 2: Filter candidates
  stock not already up > 20% since earnings date  → skip (too late)
  stock price > $5                                 → no penny stocks
  avg daily volume > 200k                          → must be liquid
  no open position already                         → no duplicate

Step 3: Score 0–100
  beat_pct > 30%              → +40 pts  (massive beat)
  beat_pct > 20%              → +30 pts  (strong beat)
  beat_pct > 15%              → +20 pts  (solid beat)
  stock up < 5% since report  → +25 pts  (market hasn't reacted yet)
  stock up 5-20% since report → +10 pts  (still early in drift)
  price above MA20             → +15 pts  (uptrend intact)
  earnings < 7 days ago        → +10 pts  (fresher = more drift ahead)
  score ≥ 65 → buy

Step 4: Place limit order
```

### Note on FMP Free Tier Coverage
FMP earnings calendar on free tier covers S&P 500 + large caps well. Smaller stocks may have incomplete data. Engine handles missing data gracefully — skips symbols with no earnings data. No crash, no error.

### Position Management
- Max 4 open positions (slower moving, safer to hold more)
- Position size: 20% of account equity
- Entry: limit at ask

**Exit conditions (first trigger wins):**
1. **Stop loss:** Hard stop -7% from entry
2. **Target hit:** +12% → exit
3. **Thesis broken:** If company reports next quarter and misses estimates → exit same day, don't hold hoping for recovery
4. **Acceleration stalled:** If stock has been flat for 15 days and EPS revision momentum has stopped → exit (market has priced it in or doesn't believe it)
5. **Time limit:** 30 days max hold

**Long only.**

### Schedule
- Runs daily at **9:50 AM ET** (Mon–Fri)
- Exits checked every 30 min

### Data Sources
| Data | Source | Extra API calls? |
|------|--------|-----------------|
| Earnings beats + history | FMP cache (shared, zero extra calls) | **Zero** |
| Price + volume | Alpaca batch snapshot (own data client) | Yes — own account |

---

## Short Interest Data — NASDAQ API (validated)

### What was tested and why FINRA CDN was dropped

| Source | Tested | Result |
|--------|--------|--------|
| `cdn.finra.org/equity/regsho/monthly/` | ✅ Tested | **403 Access Denied** — CDN is blocked |
| `nasdaqtrader.com/dynamic/symdir/shortinterest/` | ✅ Tested | **302 → 404** — URL no longer valid |
| `api.nasdaq.com/api/quote/{symbol}/short-interest` | ✅ Tested | **Works for NASDAQ-listed stocks only** — NYSE stocks return "not supported" |
| yfinance (Railway IP) | ✅ Tested | 429-blocked for bulk calls — fine for 2-5 NYSE stocks/day |

**Known limitation:** NASDAQ API does not cover NYSE-listed stocks (GME, AMC are NYSE). Most small-cap squeeze candidates (MARA, SOFI, RIVN, UPST, LCID, COIN) are NASDAQ-listed so coverage is good.

### NASDAQ API (the real solution)

```
GET https://api.nasdaq.com/api/quote/{symbol}/short-interest?type=SHORT_INTEREST&limit=1
Headers: User-Agent: Mozilla/5.0
```

Returns per-symbol:
```json
{
  "settlementDate": "05/29/2026",
  "interest": "144,871,068",        ← total shares short
  "avgDailyShareVolume": "32,560,000",
  "daysToCover": 4.45               ← shares_short / avg_daily_volume
}
```

**Validated on active stocks:** RIVN (4.4), LCID (4.3), SOFI (2.6), MARA (2.5), COIN (3.0), HOOD (1.3), UPST (5.8) — all returned data with 0.3s spacing.

### Why daysToCover beats shortPercentOfFloat

Instead of needing `floatShares` (to compute % of float), `daysToCover` is actually a **better** squeeze signal:
- `short_pct_float` tells you: what fraction of shares are short
- `daysToCover` tells you: how long it takes shorts to escape — the real squeeze pressure

High `daysToCover` = shorts need many days to buy back = price gets pushed up harder and longer.
Standard threshold: **daysToCover > 5** = strong squeeze candidate.

### How the squeeze engine uses it

No DB table needed. Call on demand for filtered candidates only:
1. Alpaca batch snapshot filters universe down to 10-30 volume/price candidates
2. NASDAQ API called for those 10-30 symbols only (0.3s apart = ~10 seconds total)
3. `daysToCover > 4` adds to score
4. No bulk loading, no scheduled job, no extra DB table

### Manual validation command (you can run this yourself)

```bash
curl "https://api.nasdaq.com/api/quote/RIVN/short-interest?type=SHORT_INTEREST&limit=1" \
  -H "User-Agent: Mozilla/5.0" \
  -H "Accept: application/json" | python3 -m json.tool
```

Expected output:
```json
{
  "data": {
    "shortInterestTable": {
      "rows": [
        {
          "settlementDate": "05/29/2026",
          "interest": "144,871,068",
          "avgDailyShareVolume": "32,560,452",
          "daysToCover": 4.449
        }
      ]
    }
  }
}
```

Try with any active stock ticker. If it returns data, integration will work on Railway too — it's a standard HTTPS call, not scraping.

---

## Scheduler Registration (main.py)

Add after existing PureAI scheduler start (lines 41–42):

```python
# Experiment engines — paper accounts, isolated from Lakshmi/Wheel/PureAI
try:
    from services.squeeze_engine import start_squeeze_scheduler
    start_squeeze_scheduler()
    logger.info("Squeeze scheduler started.")
except Exception as e:
    logger.warning(f"Squeeze scheduler not started: {e}")

try:
    from services.spillover_engine import start_spillover_scheduler
    start_spillover_scheduler()
    logger.info("Spillover scheduler started.")
except Exception as e:
    logger.warning(f"Spillover scheduler not started: {e}")

try:
    from services.revision_engine import start_revision_scheduler
    start_revision_scheduler()
    logger.info("Revision scheduler started.")
except Exception as e:
    logger.warning(f"Revision scheduler not started: {e}")
```

Each `start_*_scheduler()` checks if its API keys are set. If not, it returns immediately without starting — same pattern as `pureai_engine.py`.

---

## API Router (`routers/experiments.py`)

Single router serving all 3 engines. Prefix: `/experiments`

```
GET  /experiments/status              → all 3 engines: running?, account equity, open positions count
GET  /experiments/{engine}/positions  → open + closed positions for engine (squeeze/spillover/revision)
GET  /experiments/{engine}/summary    → P&L, win rate, trades count, best/worst trade
POST /experiments/{engine}/run        → manual trigger (force one scan cycle)
POST /experiments/{engine}/close/{id} → manually close a position
```

Register in `main.py`:
```python
from routers import experiments
app.include_router(experiments.router)
```

---

## iOS — Labs Tab

### Tab Bar Change (ContentView.swift)
Add `LabsView` as tag 2, shift existing tabs:

```
Tab 0: Dashboard
Tab 1: Orders
Tab 2: Labs       ← new
Tab 3: PureAI
Tab 4: Wheel
Tab 5: More
```

### LabsView Layout

```
┌─────────────────────────────────┐
│  Labs                     [⚙]  │
│                                 │
│  [Squeeze] [Spillover] [Revision] │  ← segmented picker
│                                 │
│  ┌─────────────────────────────┐ │
│  │ Account Equity: $25,432     │ │
│  │ Open Positions: 2           │ │
│  │ Total P&L: +$342 (+1.4%)   │ │
│  │ Win Rate: 6/9 (67%)        │ │
│  └─────────────────────────────┘ │
│                                 │
│  Open Positions                 │
│  ┌─────────────────────────────┐ │
│  │ GME  entry $18.20  +12.4%  │ │  ← green/red
│  │ AMC  entry $4.10   -3.2%   │ │
│  └─────────────────────────────┘ │
│                                 │
│  Recent Closed                  │
│  ┌─────────────────────────────┐ │
│  │ BBBY  +$234  ✓ target hit  │ │
│  │ SPCE  -$89   ✗ stopped out │ │
│  └─────────────────────────────┘ │
└─────────────────────────────────┘
```

### LabsViewModel.swift

Hits these endpoints:
- `GET /experiments/status` on load
- `GET /experiments/{engine}/positions` when engine picker changes
- `GET /experiments/{engine}/summary` for P&L stats
- Auto-refresh every 60 seconds when tab is active

---

## API Limits Summary

| Resource | Lakshmi uses | New engines add | Risk |
|----------|-------------|----------------|------|
| FMP 250 calls/day | ~2/day | **0** (reuse cache) | None |
| yfinance | Used in signals.py, macro.py | **0** — experiment engines never call yfinance | None |
| NASDAQ short interest API | Not used | 10-30 calls/day (squeeze only, 0.3s apart) | Low — validated working, no key needed |
| Alpaca data | Own client | Own clients (separate keys) | None |
| Alpaca trading | Own account | Own accounts (separate keys) | None |

---

## Build Order

1. **DB** — Add `experiment_positions` table to `db.py` `_ensure_table()`
2. **config.py** — Add 6 new env vars
3. **`revision_engine.py`** — Easiest: pure FMP cache logic, zero new data sources
4. **`spillover_engine.py`** — Needs `data/sector_peers.json`, then straightforward
5. **`squeeze_engine.py`** — NASDAQ short interest API + Alpaca batched data, no yfinance, no DB table for short interest
6. **`routers/experiments.py`** — API endpoints for all 3
7. **`main.py`** — Register all schedulers + router
8. **iOS `LabsView.swift` + `LabsViewModel.swift`** — Labs tab
9. **Railway** — Add 6 env vars, deploy, verify all schedulers start in logs

---

## What Each Engine File Looks Like (Pattern)

Each engine follows the exact same pattern as `pureai_engine.py`:

```python
"""
[Engine name] — [one line description]
Runs as daemon thread. Never touches Lakshmi/Wheel/PureAI.
Own Alpaca paper account. Writes only to experiment_positions table.
"""
import threading
import time
import logging
from config import settings

logger = logging.getLogger(__name__)

# ── Alpaca clients (own account, never shared) ─────────────────────────
def _get_trading_client():
    ...

def _get_data_client():
    ...

# ── Core scan logic ────────────────────────────────────────────────────
def run_scan() -> dict:
    ...

# ── Position management ────────────────────────────────────────────────
def check_exits():
    ...

# ── Scheduler ──────────────────────────────────────────────────────────
def _loop():
    while True:
        try:
            _run_if_market_hours()
        except Exception as e:
            logger.error(f"[Engine] scheduler error: {e}")
        time.sleep(1800)  # 30 min

def start_[engine]_scheduler():
    if not settings.alpaca_[engine]_key:
        logger.info("[Engine]: keys not set — scheduler not started")
        return
    t = threading.Thread(target=_loop, daemon=True, name="[engine]-scheduler")
    t.start()
    logger.info("[Engine] scheduler started")
```

---

## Trade Direction

**Phase 1 (now): Long only across all 3 engines.**

Reasons:
- Simpler to validate — one variable at a time
- Shorting requires checking borrow availability per symbol before every entry
- Borrow cost on squeeze candidates can be 20-50%+ annualized (eats returns)
- Paper vs live accounts handle short selling differently — harder to benchmark

**Phase 2 (after validation): Add short side to Spillover only.**
- When a company misses earnings badly (> -10%), sector peers fall too
- Same logic in reverse — buy puts or short the peer stocks
- Only add this if the long spillover engine shows ≥ 55% win rate after 60 days

**Inverse ETFs: Not used.**
They track indices, not individual squeeze/spillover/revision signals. Not a natural fit for any of these strategies.

---

## Success Criteria (after 60 days paper trading)

| Metric | Minimum to graduate to real money |
|--------|----------------------------------|
| Win rate | ≥ 55% |
| Net P&L | Positive |
| Sharpe ratio | ≥ 0.8 |
| Max drawdown | ≤ 15% |
| Trades | ≥ 20 completed trades |

If any engine fails these after 60 days → shut it down, don't allocate real money.

---

## Questions Answered

**Q: Will this impact Lakshmi, Wheel, or PureAI?**  
No. Separate DB table, separate Alpaca accounts, separate Alpaca clients, no shared imports from existing engines. The only shared resource is the FMP earnings cache (read-only) and yfinance (mitigated by 5-min stagger + call limit).

**Q: Is FMP earnings working?**  
Yes — live in Lakshmi, 2 calls/day, 24h cache. New engines reuse the cache for zero extra calls.

**Q: Is FMP analyst estimates working?**  
Disabled in Lakshmi (free tier coverage too limited). Revision engine instead uses accelerating EPS beat pattern from the same earnings cache — no analyst estimate calls needed.

**Q: Why not S&P 500 only?**  
No restriction. FMP earnings calendar covers all stocks. Alpaca data covers all stocks. Engines scan the full market.

**Q: yfinance risk?**  
Experiment engines make near-zero yfinance calls. Short interest comes from the NASDAQ per-symbol API (`api.nasdaq.com`) for NASDAQ-listed stocks — no API key, no rate limit issues. NYSE stocks use yfinance as fallback, but Alpaca volume filter reduces NYSE candidates to 1-5/day maximum — nowhere near the bulk 429 threshold that blocks Railway.

**Q: Railway env vars?**  
Yes — 6 new vars (3 key + 3 secret). Same pattern as `ALPACA_WHEEL_KEY` / `ALPACA_PUREAI_KEY`.
