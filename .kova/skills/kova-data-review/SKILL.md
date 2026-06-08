---
name: kova-data-review
description: Data analysis hat for the Kova trading project. Use this skill when the user pastes Railway logs, SQL query results, error messages, trade data, or any raw system output that needs to be interpreted. Also triggers on "here are the logs", "here's the data", "what does this mean", "analyse this output", "check these results", or any time the user shares raw data from Railway, PostgreSQL, or Alpaca. This is the primary triage skill — wear it before any bug investigation or performance diagnosis.
---

> ⚡ **GOLDEN LAW — applies in every role, every decision:**
> **Never block a trade. Never. Size it down if uncertain, but always let it through.**
> A missed good trade is a guaranteed loss. A small bad trade is recoverable.
> When in doubt: take the trade at 25% size. Never at 0%.



# Kova — Data Review Mode

When the user brings raw data — logs, SQL results, Railway output — rapidly triage, classify, and recommend.

## Triage Severity

| Severity | Meaning | Action |
|---|---|---|
| 🔴 P0 | Bot stopped trading OR money lost due to bug | Fix today |
| 🟡 P1 | Degraded performance, silent failure | Fix this sprint |
| 🟢 P2 | Enhancement opportunity | Add to roadmap |
| ℹ️ INFO | Expected behaviour | Note only |

## Railway Log Analysis

**🔴 P0 signals:**
```
Traceback (most recent call last)
service crashed / Railway restart
CRITICAL: connector failure
KeyError / AttributeError in trading_engine.py
```

**🟡 P1 signals:**
```
Options engine: fallback to stock  (check rate — >80% is a problem)
WARNING: connector
SILENT: no calls in 48h
brain failed, using claude_service  (how often?)
```

**ℹ️ Expected:**
```
No candidates above min_score  (normal in chop/bear)
Market closed                  (weekends/holidays)
Position limit reached         (expected)
```

## SQL Results — What to Look For

**Win rate thresholds:**
```
>70%   → weight boost candidate (check sample ≥15)
60-70% → healthy
55-60% → marginal, investigate before go-live
<55%   → investigate immediately, do NOT go live
```

**AvgWin / AvgLoss:**
```
> 2.0  → excellent
1.5-2  → good
1.0-1.5→ marginal
< 1.0  → 🔴 critical — losing more per loss than gaining per win
```

**Connector health:**
```
fail_pct > 80% (24h)  → WARNING should have fired
fail_pct > 80% (72h)  → CRITICAL, signal weight should be 1
total_calls = 0       → SILENT alert — check @track_api wiring
alpaca_* on weekend   → expected zero calls (skip)
```

## Standard Queries (if not yet run)

```sql
-- 1. Day 1 baseline (no history yet)
SELECT connector_name, status, COUNT(*), MAX(logged_at) as last_call
FROM connector_health_log
WHERE logged_at >= NOW() - INTERVAL '3 hours'
GROUP BY connector_name, status;

-- 2. Bot is alive?
SELECT COUNT(*) as cycles_last_hour
FROM connector_health_log
WHERE logged_at >= NOW() - INTERVAL '1 hour';

-- 3. AvgWin vs AvgLoss
SELECT
    ROUND(AVG(CASE WHEN trade_profitable THEN trade_pnl_pct END),2) as avg_win,
    ROUND(AVG(CASE WHEN NOT trade_profitable THEN ABS(trade_pnl_pct) END),2) as avg_loss
FROM signal_performance_log WHERE trade_date >= CURRENT_DATE - 30;

-- 4. Options firing?
SELECT COUNT(*) options_trades FROM position_log
WHERE opened_at >= NOW() - INTERVAL '7 days'
  AND (trade_type ILIKE '%option%' OR notes ILIKE '%option%');
```

## Day 1 Specific Checks

Since Day 1 has no historical data, look for these instead:

```
✅ "Trading cycle started" in logs → bot is alive
✅ "regime detected" in logs → market data flowing
✅ "signals scored" in logs → signal engine running
✅ connector_health_log has rows → @track_api working
⚠️ "No candidates above min_score" → normal, market may be slow
⚠️ "brain failed, using claude_service" → fallback used, check why
🔴 Traceback anywhere → bug, paste and I'll fix
🔴 No log activity for >15 min during market hours → bot down
```

## Common Bug Patterns

**Options engine never fires**
→ Check: does `ai_brain.py` ever return `holding_period == "swing"`?
→ Check: options conviction threshold — may be too high for current signal scores

**Connector silent alert (unexpected)**
→ Non-alpaca connectors silent on weekday → check API key + @track_api wiring
→ Alpaca silent on weekend → expected, already handled

**Win rate below 50%**
→ How many trades? (<10 = noise, not signal)
→ Which signals dragging? → target those for weight reduction
→ What regime? Bear = lower expected rate
→ Entry timing issue? → needs intraday windows (Month 1)

**AvgLoss > AvgWin**
→ 🔴 Most dangerous pattern
→ Are stops being hit while winners cut early?
→ Fix: widen take-profit OR tighten stop, not both simultaneously

## Output Format

```
DATA REVIEW — [DATE]

🏥 SYSTEM HEALTH
Railway:     ✅ Running / 🔴 Crashed / 🟡 Degraded
Connectors:  ✅ All green / 🟡 [name] / 🔴 [name]
Activity:    N trades / N cycles this period

📊 TRADING PERFORMANCE
Win rate: X%  [target >60%]  ✅/🟡/🔴
Ratio:    X.Xx (AvgWin +X% / AvgLoss -X%)  ✅/🟡/🔴
MaxDD:    X%  ✅/🟡/🔴

🔍 FINDINGS
🔴 [P0 — immediate fix needed, file location]
🟡 [P1 — this sprint]
🟢 [P2 — roadmap]
ℹ️ [Expected behaviour]

🛠️ ACTIONS
1. [Most urgent with file]
2. [Second priority]

✅ MILESTONE: Day 30 (~Jul 7) — On track / At risk / Off track
```
