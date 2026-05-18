"""
eod_analysis_service.py — Claude-powered end-of-day analysis for Kova.

Runs automatically when market closes. Queries today's full activity from DB,
sends to Claude for analysis, and saves a structured report to cache.
The report is served by the /api/eod router and displayed in the iOS app.
"""

import json
import logging
from datetime import datetime, timezone, date

logger = logging.getLogger(__name__)

EOD_CACHE_KEY = "eod_report_latest"


def get_todays_activity() -> dict:
    """
    Query today's trades, positions closed, rejections, and account state from DB.
    Returns a dict with all context needed for the EOD analysis prompt.
    """
    from services.db import _get_conn
    from services.alpaca_service import get_account, get_positions

    today = datetime.now(timezone.utc).date()
    result = {
        "date": today.isoformat(),
        "trades_executed": [],
        "positions_closed": [],
        "rejections": [],
        "account": {},
        "open_positions": [],
    }

    conn = _get_conn()

    # ── Today's executed trades (buy/sell/short) ──
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT action, symbol, quantity, reasoning, confidence, market_regime, timestamp
                    FROM trade_log
                    WHERE timestamp::date = %s AND action IN ('buy', 'sell', 'short')
                    ORDER BY timestamp ASC
                """, (today,))
                for row in cur.fetchall():
                    action, symbol, qty, reasoning, conf, regime, ts = row
                    result["trades_executed"].append({
                        "action": action,
                        "symbol": symbol,
                        "quantity": qty,
                        "reasoning": (reasoning or "")[:200],
                        "confidence": conf,
                        "time": ts.strftime("%H:%M") if ts else "",
                    })
        except Exception as e:
            logger.warning(f"EOD: could not query trades_executed: {e}")

        # ── Positions closed today with realized P&L ──
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT symbol, side, entry_price, exit_price, quantity,
                           realized_pl, realized_pl_pct, hold_duration_mins,
                           exit_reason, strategy
                    FROM position_log
                    WHERE exit_time::date = %s AND exit_price IS NOT NULL
                    ORDER BY exit_time ASC
                """, (today,))
                for row in cur.fetchall():
                    sym, side, ep, xp, qty, pl, pl_pct, dur, reason, strat = row
                    result["positions_closed"].append({
                        "symbol": sym,
                        "side": side or "long",
                        "entry_price": ep,
                        "exit_price": xp,
                        "quantity": qty,
                        "realized_pl": pl,
                        "realized_pl_pct": pl_pct,
                        "hold_duration_mins": dur,
                        "exit_reason": reason,
                        "strategy": strat,
                    })
        except Exception as e:
            logger.warning(f"EOD: could not query positions_closed: {e}")

        # ── Bot activity: rejections & key events ──
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT event_type, symbol, message, timestamp
                    FROM bot_activity_log
                    WHERE timestamp::date = %s AND event_type IN ('entry_rejected', 'earnings_block', 'circuit_breaker', 'trade_cap')
                    ORDER BY timestamp ASC
                """, (today,))
                for row in cur.fetchall():
                    event_type, symbol, message, ts = row
                    result["rejections"].append({
                        "type": event_type,
                        "symbol": symbol,
                        "reason": (message or "")[:120],
                        "time": ts.strftime("%H:%M") if ts else "",
                    })
        except Exception as e:
            logger.warning(f"EOD: could not query rejections: {e}")

    # ── Live account state ──
    try:
        account = get_account()
        result["account"] = {
            "portfolio_value": account.portfolio_value,
            "cash": account.cash,
            "day_pl": account.day_pl,
            "day_pl_percent": account.day_pl_percent,
        }
    except Exception as e:
        logger.warning(f"EOD: could not fetch account: {e}")

    # ── Currently open positions ──
    try:
        positions = get_positions()
        result["open_positions"] = [
            {
                "symbol": p.symbol,
                "side": p.side,
                "qty": int(float(p.qty)),
                "current_price": p.current_price,
                "unrealized_pl": p.unrealized_pl,
                "unrealized_pl_pct": p.unrealized_pl_percent,
            }
            for p in positions
        ]
    except Exception as e:
        logger.warning(f"EOD: could not fetch open positions: {e}")

    return result


def run_eod_analysis() -> dict:
    """
    Main entry point. Queries today's activity, sends to Claude for analysis,
    saves the structured report to cache, and returns it.
    """
    from services.ai_client import ask_ai_pro
    from services.db import cache_set, cache_get
    from services.strategy import get_strategy

    logger.info("Running EOD analysis...")

    activity = get_todays_activity()
    today_str = activity["date"]
    account = activity.get("account", {})
    day_pl = account.get("day_pl", 0)
    day_pl_pct = account.get("day_pl_percent", 0)
    portfolio_value = account.get("portfolio_value", 0)

    # Format trades executed
    trades_text = ""
    if activity["trades_executed"]:
        lines = [
            f"  {t['time']} {t['action'].upper()} {t['symbol']} x{t['quantity']} "
            f"[{t['confidence'] or '?'}] — {t['reasoning'][:100]}"
            for t in activity["trades_executed"]
        ]
        trades_text = "\n".join(lines)
    else:
        trades_text = "  No trades executed today."

    # Format closed positions P&L
    closed_text = ""
    if activity["positions_closed"]:
        lines = []
        for p in activity["positions_closed"]:
            pl_str = f"+${p['realized_pl']:.2f}" if (p['realized_pl'] or 0) >= 0 else f"-${abs(p['realized_pl'] or 0):.2f}"
            pct_str = f"{p['realized_pl_pct']:+.1f}%" if p['realized_pl_pct'] else "N/A"
            dur = f"{p['hold_duration_mins']}min" if p['hold_duration_mins'] else "?"
            lines.append(
                f"  {p['symbol']} [{p['side']}]: {pl_str} ({pct_str}) | "
                f"entry=${p['entry_price']:.2f} exit=${p['exit_price']:.2f} | "
                f"held {dur} | reason={p['exit_reason']}"
            )
        closed_text = "\n".join(lines)
    else:
        closed_text = "  No positions closed today."

    # Format open positions
    open_text = ""
    if activity["open_positions"]:
        lines = [
            f"  {p['symbol']} [{p['side']}]: {p['qty']} shares @ ${p['current_price']:.2f} | "
            f"unrealized P&L: ${p['unrealized_pl']:+.2f} ({p['unrealized_pl_pct']:+.1f}%)"
            for p in activity["open_positions"]
        ]
        open_text = "\n".join(lines)
    else:
        open_text = "  None"

    # Format rejections
    rejection_text = ""
    if activity["rejections"]:
        lines = [
            f"  {r['time']} {r['type'].upper()} {r['symbol'] or ''}: {r['reason']}"
            for r in activity["rejections"]
        ]
        rejection_text = "\n".join(lines)
    else:
        rejection_text = "  None"

    strat = get_strategy()

    prompt = f"""You are reviewing the performance of Kova, an AI-powered paper trading bot, for today {today_str}.
Analyze the day objectively and provide actionable insights for tomorrow.

## Today's Performance
Portfolio value: ${portfolio_value:,.2f}
Day P&L: ${day_pl:+,.2f} ({day_pl_pct:+.2f}%)
Strategy: {strat['name']}

## Trades Executed Today
{trades_text}

## Positions Closed Today (Realized P&L)
{closed_text}

## Currently Open Positions (Unrealized)
{open_text}

## Entries Rejected by Risk Filters
{rejection_text}

Important: In tomorrow_watchlist, do NOT add symbols currently held as SHORT positions with action "buy". If a symbol in open positions is marked [short], only include it with action "short" or "watch".

Respond with ONLY this JSON — no markdown, no explanation. Be concise — keep all text fields brief (headline: 1 sentence, key_insight: 2-3 sentences max, bullet points: 10 words each). Total response must fit within 1200 tokens:
{{
  "headline": "One sentence summary of today (e.g. 'Strong bull day +2.1% — NVDA and SOXL led gains')",
  "performance_grade": "A/B/C/D/F",
  "what_worked": ["Up to 3 bullet points about what went well today"],
  "what_didnt": ["Up to 3 bullet points about what underperformed or was missed"],
  "key_insight": "One paragraph: the most important pattern or lesson from today's trading",
  "tomorrow_watchlist": [
    {{"symbol": "AAPL", "thesis": "One sentence on why to watch tomorrow", "action": "buy/short/watch"}}
  ],
  "risk_note": "Any risk flag for tomorrow (e.g. upcoming earnings, macro events, overexposure)",
  "bot_rating": "1-10 score for how well the bot executed today's strategy"
}}"""

    try:
        raw = ask_ai_pro(prompt, max_tokens=2500)
        # Strip markdown if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        try:
            analysis = json.loads(raw.strip())
        except json.JSONDecodeError:
            import re
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            analysis = json.loads(match.group()) if match else {}
    except Exception as e:
        logger.error(f"EOD analysis Claude call failed: {e}")
        analysis = {
            "headline": f"EOD analysis unavailable ({str(e)[:60]})",
            "performance_grade": "N/A",
            "what_worked": [],
            "what_didnt": [],
            "key_insight": "Analysis could not be generated.",
            "tomorrow_watchlist": [],
            "risk_note": "",
            "bot_rating": "N/A",
        }

    report = {
        "date": today_str,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": {
            "portfolio_value": portfolio_value,
            "day_pl": day_pl,
            "day_pl_pct": day_pl_pct,
            "trades_executed": len(activity["trades_executed"]),
            "positions_closed": len(activity["positions_closed"]),
            "entries_rejected": len(activity["rejections"]),
        },
        "analysis": analysis,
        "raw_activity": {
            "trades": activity["trades_executed"],
            "closed": activity["positions_closed"],
            "open": activity["open_positions"],
        },
    }

    cache_set(EOD_CACHE_KEY, report, ttl_seconds=7 * 24 * 3600)  # keep 7 days
    logger.info(f"EOD analysis complete for {today_str}: grade={analysis.get('performance_grade','?')}, bot_rating={analysis.get('bot_rating','?')}")
    return report


def get_latest_eod_report() -> dict | None:
    """Return the most recent saved EOD report, or None if not yet generated."""
    from services.db import cache_get
    return cache_get(EOD_CACHE_KEY)
