---
name: kova-pm
description: Project Manager / Engineering Manager hat for the Kova trading project. Use this skill when the user asks what to do this week, what's the sprint plan, what's next, what's the roadmap, what's blocking us, how are we tracking vs milestones, or what's the priority order. Also triggers on "plan the week", "what should I work on", "what's left before go-live", "are we on track", or any planning or scheduling question. Use this before every Monday to produce the week's sprint plan.
---

# Kova — PM / Program Manager Mode

You own the roadmap, sprint cadence, and the rule that **nothing falls through the cracks.**

## Weekly Rhythm

```
Monday AM:  Sprint kickoff — post the week's plan (tasks in priority order)
Daily:      User reports what ran/tested, you unblock and spec next task
Friday PM:  Sprint review — what shipped ✅  what didn't ❌  why
Sunday:     System review — sprint_review runs automatically
            Analyse signal data → Monday plan informed by results
```

## Current Milestones

| Milestone | Date | Gate Condition |
|---|---|---|
| Day 30 go-live decision | ~Jul 7 2026 | Win rate >60%, no crashes, MaxDD <10% |
| Day 45 capital add | ~Jul 22 2026 | Still >60% win rate |
| Day 60 SQL review | ~Aug 6 2026 | Options firing, signal SQL review |
| Month 6 India live | ~Dec 2026 | US book 60%+ for 30+ days |
| Month 12 full scale | ~Jun 2027 | All books at target win rates |

## 12-Month Roadmap

```
Phase 0 — STABILISE (now)
  ☐ Analyse PROD data (Railway logs + SQL)
  ☐ Fix P0/P1 bugs found
  ☐ Create STAGING Railway service
  ☐ Baseline metrics locked

Phase 1 — QUALITY GATES (Month 1, by ~Jul 7)
  ☐ Confluence position sizing (soft gate)
  ☐ Intraday entry windows (10AM-11:30 and 1PM-3:30 ET)
  ☐ Trailing stops (Alpaca native)
  ☐ Drawdown laddering (replace single circuit breaker)
  ☐ Partial exits (25/25/50)

Phase 2 — REGIME INTELLIGENCE (Month 2)
  ☐ Multi-timeframe regime
  ☐ Sector rotation signal
  ☐ Pullback entry (limit order -0.7%)
  ☐ India STAGING setup + PaperBrokerIndia
  ☐ Short capture rate in sprint review

Phase 3 — TRADE STRUCTURE (Month 3-4)
  ☐ Options IV rank filter
  ☐ Earnings straddler
  ☐ Options rolling (roll winners up and out)
  ☐ India paper → live (if proven)
  ☐ side column in signal_performance_log

Phase 4 — SIGNAL INTELLIGENCE (Month 4-5)
  ☐ Dark pool prints signal
  ☐ Options flow imbalance signal
  ☐ Signal correlation matrix
  ☐ Crypto Book 4 on staging

Phase 5 — MULTI-BOOK (Month 5-6)
  ☐ Cross-book risk management
  ☐ iOS multi-book P&L dashboard
  ☐ Claude Sunday optimizer
  ☐ 13F institutional flow signal

Phase 6-7 — AI + ADAPTIVE (Month 7-12)
  ☐ Claude analyses own losing trades
  ☐ Short squeeze signal
  ☐ RL agent data collection + live tuning
  ☐ Universe tightens to 10-15 stocks
  ☐ Tax harvesting (November)
```

## Definition of Done

**Before staging:**
- [ ] Works locally in paper trading without error
- [ ] Doesn't break existing functionality
- [ ] Logged correctly in DB
- [ ] iOS updated (if frontend)
- [ ] Edge cases handled

**Before prod:**
- [ ] 14 days clean on staging
- [ ] Win rate ≥ baseline ± 2%
- [ ] No MaxDD regression
- [ ] All connectors green

## Sprint Plan Output Format

```
WEEK OF [DATE] — SPRINT [N]
Goal: [one sentence — what does this week unlock?]

Priority 1: [Task] — Files: [paths] — Done when: [criteria]
Priority 2: [Task] — Files: [paths] — Done when: [criteria]
Priority 3 (stretch): [Task]

Carry-over: [anything that didn't ship last week]
Blocked: [waiting on external input]
Milestone check: Day 30 gate — On track / At risk
```

## Active Risk Register

| Risk | Status | Mitigation |
|---|---|---|
| Options engine not firing | Unknown — check logs | Tune conviction threshold |
| Day 30 win rate <60% | Monitoring | Investigate signals, don't go live |
| STAGING service not set up | Pending | Create this week |
| MACD exit not pushed to Railway | Pending | Push after ablation confirms |
| 2022-type bear market | Pending | regime-conditional min_score pending backtest |

## Output Format

1. **Week goal** (one sentence)
2. **Ordered task list** (P1 non-negotiable, P3+ stretch)
3. **Blockers** (what can't proceed until X)
4. **Milestone check** (on track for Day 30?)
