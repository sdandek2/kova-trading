---
name: kova-sprint
description: Sprint review and weekly system analysis hat for the Kova trading project. Use this skill when the user shares weekly performance data, asks how signals are doing, wants to review the sprint results, asks about signal weights, asks what's working and what's not, or it's Sunday/Monday and time to review last week. Also triggers on "review last week", "how are signals performing", "should we adjust weights", "what does the data say", "sprint review results", or any request to analyse trading performance data.
---

# Kova — Sprint Review Mode

Every Sunday the bot runs an automated sprint review. Your job: interpret the data and produce next week's parameter recommendations.

## Step 1 — Collect Data

If user hasn't provided it, request these queries:

```sql
-- A. Weekly performance by signal
SELECT signal_name, COUNT(*) trades,
    ROUND(AVG(CASE WHEN trade_profitable THEN 1.0 ELSE 0 END)*100,1) as win_rate,
    ROUND(AVG(trade_pnl_pct),2) as avg_pnl
FROM signal_performance_log
WHERE trade_date >= CURRENT_DATE - 7
GROUP BY signal_name HAVING COUNT(*) >= 2
ORDER BY win_rate DESC;

-- B. 30-day for weight adjustments
SELECT signal_name, COUNT(*) trades,
    ROUND(AVG(CASE WHEN trade_profitable THEN 1.0 ELSE 0 END)*100,1) as win_rate_30d,
    current_weight, default_weight
FROM signal_performance_log sp
JOIN signal_weights sw USING (signal_name)
WHERE trade_date >= CURRENT_DATE - 30
GROUP BY signal_name, current_weight, default_weight
HAVING COUNT(*) >= 15
ORDER BY win_rate_30d DESC;

-- C. Daily P&L this week
SELECT DATE(trade_date) as day, COUNT(*) trades,
    ROUND(SUM(trade_pnl_pct),2) as daily_pnl,
    ROUND(AVG(CASE WHEN trade_profitable THEN 1.0 ELSE 0 END)*100,1) as win_pct
FROM signal_performance_log
WHERE trade_date >= CURRENT_DATE - 7
GROUP BY DATE(trade_date) ORDER BY day;

-- D. AvgWin vs AvgLoss (most important ratio)
SELECT
    ROUND(AVG(CASE WHEN trade_profitable THEN trade_pnl_pct END),2) as avg_win,
    ROUND(AVG(CASE WHEN NOT trade_profitable THEN ABS(trade_pnl_pct) END),2) as avg_loss,
    ROUND(AVG(CASE WHEN trade_profitable THEN trade_pnl_pct END) /
          NULLIF(AVG(CASE WHEN NOT trade_profitable THEN ABS(trade_pnl_pct) END),0),2) as ratio
FROM signal_performance_log
WHERE trade_date >= CURRENT_DATE - 30;
```

## Step 2 — Weight Adjustment Rules

- Win rate **>70%** AND sample **≥15** → boost +2 (max = default × 1.50)
- Win rate **<40%** AND sample **≥15** → reduce -2 (min = 1)
- Sample **<15** → NO adjustment (insufficient data)
- Connector critical failure → weight forced to 1 (auto-handled by connector_health.py)
- Weights anchored to **default_weight**, not current weight

## Step 3 — What Else to Check

**AvgWin / AvgLoss < 1.5** → review stop-loss aggressiveness, widen take-profit
**Win rate < 55% for 5+ days** → investigate, do not increase size
**MaxDD > 7%** → reduce all sizes 25% immediately, alert CEO
**Options fallback rate > 80%** → conviction threshold needs lowering
**Options 0 trades this week** → check options engine wiring in trading_engine.py

## Step 4 — Sprint Review Output

```
SPRINT REVIEW — WEEK OF [DATE]

📊 SCORECARD
Win rate:   X%    (target: >60%) ✅/🟡/🔴
AvgWin:    +X%   AvgLoss: -X%   Ratio: X.Xx  ✅/🟡/🔴
Total P&L: +/-X%
MaxDD:      X%                               ✅/🟡/🔴
Trades:     N this week

🔧 WEIGHT CHANGES
[signal] X → Y  (win rate 72%, 18 trades)
[signal] X → Y  (win rate 35%, 20 trades)
No change: [list — insufficient data or stable]

⚠️ FLAGS
[Any connector issues, MaxDD concerns, options problems]

📅 NEXT WEEK FOCUS
[1-2 specific items based on this data]

✅ MILESTONE: Day 30 (~Jul 7) — On track / At risk / Off track
```

## Win Rate Targets

| Phase | Target | Action if Below |
|---|---|---|
| Paper now | 55%+ | Investigate signal quality |
| Day 30 go-live | >60% | Don't go live |
| Month 6 | >70% | Options + confluence tuning |
| Month 12 | 75-80% | Full signal library optimised |
| Month 24 | 85-90% | RL agent fine-tuning |

## Auto-Tuning vs Manual

| Parameter | Auto? | Manual trigger |
|---|---|---|
| Signal weights | ✅ weekly | Sample too small, connector issue |
| Stop loss | ❌ | AvgLoss > AvgWin consistently |
| Confluence threshold | ❌ | Trade count < 10/week |
| Options conviction | ❌ | Fallback rate > 80% |

## Red Flags — Escalate Immediately

- Win rate < 45% for 3 consecutive days → P0
- MaxDD > 7% → reduce sizes, alert
- Any connector > 80% failure 24h → P0
- Options in wrong direction vs regime → bug
- Railway crash → P0
