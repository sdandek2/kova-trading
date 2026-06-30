---
name: project-sec-intel
description: SEC Intel engine status, pending work, and 13D/13G backtest
metadata:
  type: project
---

# SEC Intel Engine — Current State (2026-06-29)

## Live Status
- Deployed to Railway, running on paper Alpaca account ($50k)
- 10/10 positions filled (AMD, SNDK, CRWV + 7 others from SA Q1 2026 signals)
- 23,538 signals synced from DuckDB to Railway PostgreSQL
- Scheduler runs daily 9:35 AM ET weekdays — fully automatic
- Telegram bot: @LakshmiSecIntel_bot, chat_id=5023706656
- Warrant/OTC filter deployed (skips tickers with W suffix, -, ., >5 chars)

## Q2 2026 Signal Sync — MANUAL
- Q2 2026 13F filings come out mid-August 2026 (45-day lag after June 30)
- User must manually run sync after August filings:
  ```
  cd sec-intelligence
  export DATABASE_URL=postgresql://...
  python3 sync_to_railway.py
  ```
- Bot does NOT auto-sync — sync is a local DuckDB → Railway push
- After sync, bot picks up new signals automatically at next 9:35 AM run

**How to apply:** Remind user in August to re-run sync_to_railway.py for Q2 signals.

## Pending: SEC 13D/13G Backtest
- File: `sec-intelligence/pipeline/backtest_13dg.py`
- Purpose: validate 13D/13G filings predict price movement before auto-trading
- Fixed extractor: EDGAR index → subject company CIK → submissions API → ticker
- Elliott CIK corrected to 0001791786 (was wrong before)
- SA excluded — too small to file 13D/13G (never crosses 5% ownership)
- Run command:
  ```
  cd sec-intelligence && source venv/bin/activate
  python3 pipeline/backtest_13dg.py --years 3
  ```
- Next step after backtest: if 90d win rate > 60% → build real-time 13D/13G monitor

## Pending Code (not yet pushed)
- Account balance fix deployed (wrong function name `_alpaca()` → `_get_trading_client()`)
- Warrant filter deployed
