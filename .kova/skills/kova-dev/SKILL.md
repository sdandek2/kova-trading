---
name: kova-dev
description: Developer / implementation hat for the Kova trading project. Use this skill when the user wants to write code, implement a feature, fix a bug, deploy to Railway, make an iOS change, add a database migration, or do any hands-on engineering work. Also triggers on "implement X", "write the code for", "fix the bug in", "push to Railway", "add a column", "build the X feature", or any request to produce actual working code. Always wear this hat when writing or reviewing code files in the trading-app project.
---

> ⚡ **GOLDEN LAW — applies in every role, every decision:**
> **Never block a trade. Never. Size it down if uncertain, but always let it through.**
> A missed good trade is a guaranteed loss. A small bad trade is recoverable.
> When in doubt: take the trade at 25% size. Never at 0%.



# Kova — Developer Mode

You write production code that trades real money. Every line matters.

## Project Root

```
/Users/siddheshdandekar/Documents/Siddhesh Playground/trading-app
├── backend/
│   ├── main.py
│   └── services/
│       ├── trading_engine.py     ← trading cycle
│       ├── macro.py              ← FRED macro context
│       ├── connector_health.py   ← health monitoring
│       ├── db.py                 ← all DB functions
│       └── brain/
│           ├── ai_brain.py       ← Claude decisions
│           ├── signals.py        ← signal scoring
│           ├── regime.py         ← bull/bear/chop/volatile
│           ├── kelly.py          ← position sizing
│           ├── options_engine.py ← options routing
│           └── sprint_review.py  ← weekly self-tuning
└── ios/TradingApp/Sources/Views/
    ├── AI/AIView.swift
    └── Insights/InsightsView.swift
```

## Coding Standards

```python
# External API calls — always use @track_api:
from services.connector_health import track_api

@track_api("connector_name")
async def call_external_api():
    ...  # exceptions re-raised and logged automatically

# Soft gate — never block, only size:
multipliers = {4: 1.0, 3: 1.0, 2: 0.5, 1: 0.25}
size_multiplier = multipliers.get(confluence_count)
if size_multiplier is None:
    return  # only skip at 0 signals

# DB — always use db.py, never raw SQL in services
await db.log_signal_performance(signal_name, pnl, profitable)
```

```swift
// iOS — match existing NavigationLink card pattern:
NavigationLink {
    DetailView().task { await vm.loadData() }
} label: {
    HStack {
        Image(systemName: "icon")
        Text("Label").fontWeight(.semibold)
        Spacer()
        Image(systemName: "chevron.right").font(.caption).foregroundColor(.secondary)
    }
    .padding()
    .background(Color(.secondarySystemBackground))
    .cornerRadius(14)
}
.foregroundColor(.primary)
```

## Deployment Workflow

```bash
# 1. Local paper trading test
cd backend && ENVIRONMENT=development BOOK_MODE=paper python -m uvicorn main:app --reload

# 2. Commit to feature branch (specific files only — never git add -A)
git checkout -b feature/my-feature
git add backend/services/specific_file.py
git commit -m "feat: description"

# 3. Push to staging (NOT main)
git push origin feature/my-feature
# PR → staging branch → 14 days clean → PR staging → main
```

## Key DB Tables

```sql
signal_performance_log (signal_name, trade_date, trade_profitable, trade_pnl_pct)
position_log           (symbol, opened_at, closed_at, pnl_pct, trade_type, notes)
signal_weight_history  (signal_name, old_weight, new_weight, changed_at, reason)
connector_health_log   (connector_name, status, logged_at, key_missing)
sprint_review_daily    (review_date, top_gainers, top_losers, capture_rate, notes)
-- Month 1 (to build):
parameter_adjustments  (param_name, current_val, floor, cap, last_adjusted, reason)
```

## Month 1 Implementation Notes

### Confluence Position Sizing
- **File:** `backend/services/brain/kelly.py`
- After Kelly fraction computed, multiply by confluence_multiplier
- Multipliers: 4+ signals=1.0, 3=1.0, 2=0.5, 1=0.25, 0=skip

### Trailing Stops
- **File:** `backend/services/trading_engine.py`
- Replace fixed take-profit with Alpaca trailing stop (trail_percent)
- trail_percent = 2% default, tighten to 1% after 5% gain

### Intraday Entry Windows
- **File:** `backend/services/trading_engine.py` → `run_trading_cycle()`
- Windows: 10:00-11:30 AM ET and 1:00-3:30 PM ET only
- Position management (stops, exits) runs any time regardless

### Drawdown Laddering
- **File:** `backend/services/trading_engine.py`
- Replace single circuit breaker with:
  ```python
  if daily_pnl_pct <= -6: exits_only = True
  elif daily_pnl_pct <= -4: size_multiplier *= 0.5
  elif daily_pnl_pct <= -2: size_multiplier *= 0.75
  if weekly_pnl_pct <= -3: size_multiplier *= 0.8
  if monthly_pnl_pct <= -10: trading_paused = True
  ```

## Pre-Commit Checklist

- [ ] Tested locally with paper API
- [ ] DB logs correct data after running
- [ ] No new errors in logs
- [ ] iOS: `plutil -lint ios/TradingApp/Kova.xcodeproj/project.pbxproj`
- [ ] Specific files staged only (no `git add -A`)
- [ ] Commit message has feat/fix/refactor prefix

## Output Format

1. **Exact files to modify** (full paths)
2. **Exact code** (complete functions — this runs on real money)
3. **Test steps** (how to verify it works)
4. **Rollback** (how to undo if it breaks)
