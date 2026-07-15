"""
sprint_review.py — Daily EOD sprint review + weekly signal weight adjustment.

run_daily_sprint_review()  — 4:30 PM ET, pure SQL + Alpaca screener, no AI
run_weekly_weight_adjustment() — Sunday, pure arithmetic, no AI

Both functions are injected into the existing EOD Claude prompt via
eod_analysis_service.py so Claude naturally incorporates them into its
narrative. No separate AI call, no extra cost.
"""

import logging
from datetime import datetime, timezone, date as _date

logger = logging.getLogger(__name__)


def run_daily_sprint_review() -> dict:
    """
    Runs at 4:30 PM ET. Pure SQL + Alpaca screener — no AI.

    1. Fetch top 50 movers (Alpaca screener)
    2. Classify each against daily_universe_log + position_log
    3. Compute signal win rates from signal_performance_log
    4. Flag underperforming signals (< 40% win rate with >= 15 samples)
    5. Store to sprint_review_daily table
    6. Return summary dict (injected into existing EOD Claude prompt)
    """
    from services.db import (
        _get_conn, save_sprint_review, get_signal_win_rates, get_latest_sprint_review,
    )
    from services import alpaca_service

    today = datetime.now(timezone.utc).date()
    logger.info(f"Running daily sprint review for {today}...")

    try:
        # ── Step 1: Fetch today's top movers from Alpaca screener ─────────────
        gainers_raw = []
        losers_raw = []
        try:
            from alpaca.data.historical.screener import ScreenerClient
            from alpaca.data.requests import MarketMoversRequest
            from config import settings as _settings
            sc = ScreenerClient(_settings.alpaca_api_key, _settings.alpaca_secret_key)
            movers = sc.get_market_movers(MarketMoversRequest(top=25))
            gainers_raw = movers.gainers or []
            losers_raw = movers.losers or []
        except Exception as e:
            logger.warning(f"Sprint review: could not fetch movers: {e}")

        # ── Step 2: Classify each mover ───────────────────────────────────────
        conn = _get_conn()

        def _classify(symbol: str, pct_change: float) -> dict:
            result = {
                "symbol": symbol,
                "pct_change": round(pct_change, 2),
                "kova_status": "MISSED_ENTIRELY",
                "kova_pnl": None,
                "sources": [],
                "in_universe": False,
            }
            if not conn:
                return result
            try:
                with conn.cursor() as cur:
                    # Was it in today's universe?
                    cur.execute("""
                        SELECT sources FROM daily_universe_log
                        WHERE log_date = %s AND symbol = %s
                    """, (today, symbol))
                    row = cur.fetchone()
                    if row:
                        result["in_universe"] = True
                        result["sources"] = row[0] or []

                    # Was it traded today?
                    cur.execute("""
                        SELECT realized_pl_pct, exit_time FROM position_log
                        WHERE symbol = %s AND exit_time::date = %s AND exit_price IS NOT NULL
                        ORDER BY exit_time DESC LIMIT 1
                    """, (symbol, today))
                    trade = cur.fetchone()
                    if trade:
                        pl_pct, _ = trade
                        if pl_pct is not None and float(pl_pct) > 0:
                            result["kova_status"] = "CAPTURED"
                        else:
                            result["kova_status"] = "IN_UNIVERSE_WRONG"
                        result["kova_pnl"] = float(pl_pct) if pl_pct is not None else None
                    elif result["in_universe"]:
                        result["kova_status"] = "IN_UNIVERSE_SKIPPED"
            except Exception as e:
                logger.debug(f"Sprint review classify {symbol}: {e}")
            return result

        top_gainers = [_classify(g.symbol, float(g.percent_change or 0)) for g in gainers_raw[:20]]
        top_losers = [_classify(l.symbol, float(l.percent_change or 0)) for l in losers_raw[:10]]

        # ── Step 3: Aggregate capture stats ──────────────────────────────────
        captured = sum(1 for g in top_gainers if g["kova_status"] == "CAPTURED")
        missed_entirely = sum(1 for g in top_gainers if g["kova_status"] == "MISSED_ENTIRELY")
        capture_rate = round(captured / len(top_gainers) * 100, 1) if top_gainers else 0.0

        # Hypothetical P&L: equal-weight position in all top-10 gainers at their actual gains
        top10_gains = [g["pct_change"] for g in top_gainers[:10] if g["pct_change"] > 0]
        hyp_long_pnl = round(sum(top10_gains) / len(top10_gains), 2) if top10_gains else 0.0

        top5_losses = [abs(l["pct_change"]) for l in top_losers[:5] if l["pct_change"] < 0]
        hyp_short_pnl = round(sum(top5_losses) / len(top5_losses), 2) if top5_losses else 0.0

        # Actual Kova P&L today
        actual_pnl = 0.0
        try:
            account = alpaca_service.get_account()
            actual_pnl = round(float(account.day_pl_percent or 0), 2)
        except Exception:
            pass

        # Missed-by-source breakdown: for MISSED_ENTIRELY, which source would have caught it?
        missed_by_source: dict[str, int] = {}
        for g in top_gainers:
            if g["kova_status"] == "MISSED_ENTIRELY":
                missed_by_source["no_source"] = missed_by_source.get("no_source", 0) + 1

        # ── Step 4: Signal win rates from signal_performance_log ──────────────
        win_rates_list = get_signal_win_rates(days=30)
        signal_win_rates = {r["signal_name"]: float(r["win_rate"] or 0) for r in win_rates_list}

        # Flag underperforming signals: < 40% win rate with >= 15 samples
        flagged = [r["signal_name"] for r in win_rates_list
                   if (r["win_rate"] or 0) < 0.40 and (r["sample_count"] or 0) >= 15]

        # ── Step 5: Store to DB ───────────────────────────────────────────────
        review = {
            "review_date": today,
            "top_gainers": top_gainers,
            "top_losers": top_losers,
            "opportunity_capture_rate": capture_rate,
            "hypothetical_long_pnl": hyp_long_pnl,
            "hypothetical_short_pnl": hyp_short_pnl,
            "actual_kova_pnl": actual_pnl,
            "missed_entirely_count": missed_entirely,
            "missed_by_source": missed_by_source,
            "signal_win_rates": signal_win_rates,
            "flagged_signals": flagged,
        }
        save_sprint_review(review)
        logger.info(
            f"Sprint review saved: captured={captured}/{len(top_gainers)} ({capture_rate}%), "
            f"missed={missed_entirely}, flagged_signals={flagged}"
        )
        return review

    except Exception as e:
        logger.error(f"run_daily_sprint_review failed: {e}", exc_info=True)
        return {}


def run_weekly_weight_adjustment() -> None:
    """
    Runs every Sunday. Pure arithmetic — no AI.
    Reads 30-day signal win rates → adjusts weights in signal_weights table.

    Rules:
    - Sample count < 15: skip (not enough data)
    - Win rate < 40%: reduce weight by up to 15% (min 85% of original)
    - Win rate > (overall win rate + 10pp): increase weight by up to 15% (max 115%)
    - Otherwise: leave unchanged

    The boost threshold is relative, not a fixed 70%, because this strategy's
    overall win rate runs ~30-40% — a flat 70% bar was unreachable in practice
    (e.g. analyst_revision sat at 47.6% win rate over 42 trades, comfortably
    the best-performing signal in the system, and never once qualified for a
    boost). A signal meaningfully outperforming the bot's own baseline should
    get more weight even if it isn't outperforming a coin flip.
    """
    from services.db import (
        get_signal_win_rates, get_signal_weights_full, update_signal_weight,
        get_trade_performance_summary,
    )

    logger.info("Running weekly signal weight adjustment...")
    try:
        win_rates = get_signal_win_rates(days=30)
        all_weights = get_signal_weights_full()  # {signal: {current, default}}

        overall = get_trade_performance_summary()
        overall_win_rate = float(overall.get("win_rate_pct") or 0) / 100.0
        boost_threshold = overall_win_rate + 0.10
        logger.info(
            f"Overall win rate {overall_win_rate:.0%} — signal boost threshold set to {boost_threshold:.0%}"
        )

        for row in win_rates:
            signal = row["signal_name"]
            sample_count = row.get("sample_count") or 0
            win_rate = float(row.get("win_rate") or 0)
            weight_row = all_weights.get(signal)

            if weight_row is None:
                continue  # not a managed signal
            if sample_count < 15:
                continue  # not enough data

            current = weight_row["current"]
            default_weight = weight_row["default"]

            if win_rate < 0.40:
                # Decay toward 1 — floor is 85% of current (compounding decay), min 1
                floor = max(1, int(current * 0.85))
                new_weight = max(floor, current - 2)
                reason = f"win_rate {win_rate:.0%} over {sample_count} trades — reduced"
                update_signal_weight(signal, new_weight, reason,
                                     old_weight=current, win_rate=win_rate, sample_count=sample_count)
                logger.info(f"Weight adjusted: {signal} {current} → {new_weight} ({reason})")

            elif win_rate > boost_threshold:
                # Recovery/boost capped at 150% of DEFAULT — not current.
                # Prevents permanent death: a signal at weight=1 can climb back to default
                # at +2/week even after months of suppression.
                cap = int(default_weight * 1.50)
                new_weight = min(cap, current + 2)
                reason = f"win_rate {win_rate:.0%} over {sample_count} trades — increased"
                update_signal_weight(signal, new_weight, reason,
                                     old_weight=current, win_rate=win_rate, sample_count=sample_count)
                logger.info(f"Weight adjusted: {signal} {current} → {new_weight} ({reason})")

        logger.info("Weekly weight adjustment complete.")
    except Exception as e:
        logger.error(f"run_weekly_weight_adjustment failed: {e}", exc_info=True)
