import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from services.db import _get_conn, _extract_setup_tag

logger = logging.getLogger(__name__)


def _pack_prior(values: list[float]) -> dict:
    wins = sum(1 for v in values if v > 0)
    avg = sum(values) / len(values)
    avg_win = sum(v for v in values if v > 0) / wins if wins else 0.0
    losses = len(values) - wins
    avg_loss = sum(abs(v) for v in values if v < 0) / losses if losses else 0.0
    expectancy = (wins / len(values) * avg_win) - ((1 - wins / len(values)) * avg_loss)
    return {
        "trades": len(values),
        "avg_pl_pct": round(avg, 2),
        "expectancy_pct": round(expectancy, 2),
        "win_rate_pct": round(wins / len(values) * 100, 2),
    }


def run_trade_replay(
    days: int = 180,
    min_prior_trades: int = 3,
    negative_cutoff: float = -0.35,
    positive_boost_cutoff: float = 0.35,
) -> dict:
    """
    Sequentially replay recent closed trades using only information that would
    have existed before each trade entry. This tests the current predictive gate
    against actual history without needing full intraday bar storage.
    """
    empty = {
        "period_days": days,
        "min_prior_trades": min_prior_trades,
        "negative_cutoff": negative_cutoff,
        "positive_boost_cutoff": positive_boost_cutoff,
        "baseline": {"trades": 0, "avg_pl_pct": 0.0, "total_realized_pl": 0.0, "win_rate_pct": 0.0},
        "replay": {"taken_trades": 0, "skipped_trades": 0, "avg_pl_pct": 0.0, "total_realized_pl": 0.0, "win_rate_pct": 0.0},
        "delta": {"trade_count": 0, "avg_pl_pct": 0.0, "total_realized_pl": 0.0},
        "skipped_worst": [],
        "missed_winners": [],
    }
    try:
        conn = _get_conn()
        if not conn:
            return empty
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    symbol, side, entry_time, exit_time, realized_pl, realized_pl_pct,
                    strategy, claude_reasoning, market_regime
                FROM position_log
                WHERE exit_time IS NOT NULL
                  AND entry_time IS NOT NULL
                  AND exit_time >= %s
                  AND realized_pl IS NOT NULL
                  AND realized_pl_pct IS NOT NULL
                ORDER BY entry_time ASC, exit_time ASC
            """, (cutoff,))
            rows = cur.fetchall()
        if not rows:
            return empty

        priors_symbol_side: dict[str, list[float]] = {}
        priors_symbol_regime_side: dict[str, list[float]] = {}
        priors_setup: dict[str, list[float]] = {}
        priors_news: dict[str, list[float]] = {}
        priors_conviction: dict[str, list[float]] = {}

        baseline_pcts: list[float] = []
        replay_pcts: list[float] = []
        baseline_total = 0.0
        replay_total = 0.0
        skipped_worst: list[dict] = []
        missed_winners: list[dict] = []
        taken_count = 0
        skipped_count = 0

        for symbol, side, entry_time, exit_time, realized_pl, realized_pl_pct, strategy, reasoning, market_regime in rows:
            sym = (symbol or "").upper()
            trade_side = (side or "long").lower()
            pct = float(realized_pl_pct or 0.0)
            pl = float(realized_pl or 0.0)
            regime = market_regime or "unknown"
            setup = _extract_setup_tag(reasoning, strategy=strategy, side=trade_side)
            news_key = "news_event" if "NEWS_EVENT=" in (reasoning or "") else "non_news"
            conviction_key = "rocket" if "[ROCKET]" in (reasoning or "") else "standard"

            baseline_pcts.append(pct)
            baseline_total += pl

            symbol_regime_key = f"{sym}|{regime}|{trade_side}"
            symbol_side_key = f"{sym}|{trade_side}"

            prior = None
            if len(priors_symbol_regime_side.get(symbol_regime_key, [])) >= min_prior_trades:
                prior = _pack_prior(priors_symbol_regime_side[symbol_regime_key])
            elif len(priors_symbol_side.get(symbol_side_key, [])) >= min_prior_trades:
                prior = _pack_prior(priors_symbol_side[symbol_side_key])
            elif len(priors_setup.get(setup, [])) >= min_prior_trades:
                prior = _pack_prior(priors_setup[setup])

            take_trade = True
            if prior and prior["expectancy_pct"] <= negative_cutoff and conviction_key != "rocket":
                take_trade = False

            if take_trade:
                taken_count += 1
                replay_pcts.append(pct)
                replay_total += pl
            else:
                skipped_count += 1
                item = {
                    "symbol": sym,
                    "side": trade_side,
                    "entry_time": entry_time.isoformat() if entry_time else None,
                    "realized_pl_pct": pct,
                    "realized_pl": round(pl, 2),
                    "prior_expectancy_pct": prior["expectancy_pct"] if prior else None,
                    "prior_trades": prior["trades"] if prior else 0,
                    "setup": setup,
                    "market_regime": regime,
                }
                if pct < 0:
                    skipped_worst.append(item)
                else:
                    missed_winners.append(item)

            priors_symbol_side.setdefault(symbol_side_key, []).append(pct)
            priors_symbol_regime_side.setdefault(symbol_regime_key, []).append(pct)
            priors_setup.setdefault(setup, []).append(pct)
            priors_news.setdefault(news_key, []).append(pct)
            priors_conviction.setdefault(conviction_key, []).append(pct)

        baseline_wins = sum(1 for v in baseline_pcts if v > 0)
        replay_wins = sum(1 for v in replay_pcts if v > 0)
        baseline_avg = round(sum(baseline_pcts) / len(baseline_pcts), 2) if baseline_pcts else 0.0
        replay_avg = round(sum(replay_pcts) / len(replay_pcts), 2) if replay_pcts else 0.0

        return {
            "period_days": days,
            "min_prior_trades": min_prior_trades,
            "negative_cutoff": negative_cutoff,
            "positive_boost_cutoff": positive_boost_cutoff,
            "baseline": {
                "trades": len(baseline_pcts),
                "avg_pl_pct": baseline_avg,
                "total_realized_pl": round(baseline_total, 2),
                "win_rate_pct": round(baseline_wins / len(baseline_pcts) * 100, 2) if baseline_pcts else 0.0,
            },
            "replay": {
                "taken_trades": taken_count,
                "skipped_trades": skipped_count,
                "avg_pl_pct": replay_avg,
                "total_realized_pl": round(replay_total, 2),
                "win_rate_pct": round(replay_wins / len(replay_pcts) * 100, 2) if replay_pcts else 0.0,
            },
            "delta": {
                "trade_count": taken_count - len(baseline_pcts),
                "avg_pl_pct": round(replay_avg - baseline_avg, 2),
                "total_realized_pl": round(replay_total - baseline_total, 2),
            },
            "skipped_worst": sorted(skipped_worst, key=lambda x: x["realized_pl_pct"])[:15],
            "missed_winners": sorted(missed_winners, key=lambda x: x["realized_pl_pct"], reverse=True)[:15],
        }
    except Exception as e:
        logger.warning(f"run_trade_replay failed ({e})")
        return empty
