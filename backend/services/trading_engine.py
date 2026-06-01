import asyncio
import functools
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from models.trade import TradingStatus, AIAnalysis
from services import alpaca_service, claude_service
from services.db import (
    cache_get, cache_set, log_trade_decision,
    log_position_open, log_position_close,
    log_circuit_breaker, log_bot_activity,
    get_trade_performance_summary,
)
from services.indicators import compute_atr, compute_rsi, volatility_adjusted_quantity
from services.entry_timing import should_confirm_entry, get_scale_in_quantity, should_scale_out, should_cover_short, get_cooldown_symbols
from services.earnings import get_upcoming_earnings
from services.macro import get_macro_context, get_sector_rotation
from services.macro_calendar import get_macro_event_today, is_fomc_entry_blocked, check_fda_event as check_fda_binary_event
from services.geopolitical import get_geopolitical_context, get_trend_forecast
from websocket.manager import manager

logger = logging.getLogger(__name__)

TRADING_INTERVAL_SECONDS = 600  # 10 minutes

_RISK_CACHE_KEY = "user_pref:risk_settings"
_RISK_DEFAULTS = {
    "daily_loss_limit_pct": 3.0,   # halt fresh entries sooner when the day is going wrong
    "stop_loss_pct": 0.04,          # 4% trailing stop fallback (Claude overrides per trade)
    "take_profit_pct": 0.12,        # 12% TP fallback (Claude overrides per trade)
    "min_daily_trades": 0,          # never force churn just to hit an activity target
    "afternoon_pressure_hour": 12,  # pressure kicks in at noon ET
    "max_trades_per_cycle": 3,      # max new buys/shorts per cycle; sells/covers/trailing stops never blocked
    "max_penny_position_pct": 3.0,  # max position size % for stocks under $5 (stored as %, e.g. 3.0 = 3%)
    "cycle_interval_seconds": 600,  # how often the bot runs (seconds); 600=10min, 300=5min
    "profit_reserve_pct": 0.0,      # % of each realized profit moved to reserve (0 = disabled)
}

_RESERVE_CACHE_KEY = "user_pref:reserved_cash"


def get_reserved_cash() -> float:
    """Return the current reserved cash balance (never used for trading)."""
    val = cache_get(_RESERVE_CACHE_KEY)
    return float(val) if val is not None else 0.0


def add_to_reserve(amount: float) -> float:
    """Add amount to reserved cash. Returns new total. Safe to call from any thread."""
    current = get_reserved_cash()
    new_total = round(current + amount, 2)
    cache_set(_RESERVE_CACHE_KEY, new_total, 365 * 24 * 3600)
    logger.info(f"Profit reserve: +${amount:.2f} → total ${new_total:.2f}")
    return new_total


def _load_risk_settings() -> dict:
    """Load risk settings from persistent cache, falling back to defaults."""
    from services.db import cache_get

    def _as_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    cached = cache_get(_RISK_CACHE_KEY)
    if isinstance(cached, dict):
        # Merge with defaults so new keys are always present
        settings = {**_RISK_DEFAULTS, **cached}
        # Defaults are aggressive, but explicit UI/API settings should remain
        # user-controlled when you intentionally dial the bot up or down.
        settings["min_daily_trades"] = max(_as_int(settings.get("min_daily_trades"), 0), 0)
        settings["max_trades_per_cycle"] = max(_as_int(settings.get("max_trades_per_cycle"), 3), 1)
        settings["afternoon_pressure_hour"] = min(max(_as_int(settings.get("afternoon_pressure_hour"), 12), 9), 15)
        settings["cycle_interval_seconds"] = max(_as_int(settings.get("cycle_interval_seconds"), 600), 60)
        return settings
    return _RISK_DEFAULTS.copy()


def _save_risk_settings(settings: dict) -> None:
    from services.db import cache_set
    cache_set(_RISK_CACHE_KEY, settings, 365 * 24 * 3600)  # 1 year TTL


def _compute_entry_score(
    action: str,
    confidence: str,
    relative_volume: float,
    rsi: Optional[float],
    macd_hist_pct: Optional[float],
    market_tier: str,
) -> float:
    score = {"low": 45.0, "medium": 65.0, "high": 82.0}.get((confidence or "").lower(), 55.0)
    score += min(max((relative_volume or 1.0) - 1.0, 0.0), 3.0) * 5.0
    if rsi is not None:
        if action == "buy":
            if 48 <= rsi <= 72:
                score += 6.0
            elif rsi > 85:
                score -= 8.0
        elif action == "short":
            if rsi >= 68:
                score += 8.0
            elif rsi < 60:
                score -= 10.0
    if macd_hist_pct is not None:
        if action == "buy":
            score += min(max(macd_hist_pct, -0.2), 0.5) * 30.0
        elif action == "short":
            score += min(max(-macd_hist_pct, -0.2), 0.5) * 30.0
    if market_tier == "bull" and action == "buy":
        score += 4.0
    if market_tier == "bear" and action == "short":
        score += 4.0
    if market_tier == "bull" and action == "short":
        score -= 6.0
    return round(max(0.0, min(score, 100.0)), 2)


def _regime_risk_multiplier(macro: dict, geo: Optional[dict] = None) -> float:
    """
    Convert macro / geopolitical backdrop into a sizing multiplier.
    Prompts already mention regime awareness, but the engine should also enforce it.
    """
    regime = (macro or {}).get("market_regime", "neutral")
    vix_level = (macro or {}).get("vix_level", "normal")
    geo_risk = (geo or {}).get("risk_level", "low")

    multiplier = 1.0
    if regime == "bear":
        multiplier = min(multiplier, 0.35)
    elif regime == "volatile":
        multiplier = min(multiplier, 0.50)
    elif vix_level in ("elevated", "extreme_fear"):
        multiplier = min(multiplier, 0.60)

    if geo_risk in ("high", "extreme"):
        multiplier = min(multiplier, 0.50)
    elif geo_risk == "elevated":
        multiplier = min(multiplier, 0.70)

    return multiplier


_risk_settings = _load_risk_settings()
# Restore trailing-stop watermarks from last session (survives server restarts)
_position_high_watermarks = {}  # populated after _load_watermarks is defined below

_is_running = False
_last_analysis_at: Optional[datetime] = None
_next_run_at: Optional[datetime] = None
_latest_analysis: Optional[AIAnalysis] = None
_task: Optional[asyncio.Task] = None
_wake_event: Optional[asyncio.Event] = None
_urgent_news_context: list[dict] = []


def _derive_market_tier(macro: dict) -> str:
    """Return the same bull/neutral/bear tier used in the AI prompts."""
    regime = (macro or {}).get("market_regime", "")
    spy_trend = (macro or {}).get("spy_trend", "")
    vix_level = (macro or {}).get("vix_level", "normal")
    is_bull = (
        regime in ("bull", "bullish") and
        "uptrend" in (spy_trend or "") and
        vix_level in ("normal", "low", "low_fear")
    )
    is_bear = regime in ("bear", "bearish") or "downtrend" in (spy_trend or "")
    if is_bull:
        return "bull"
    if is_bear:
        return "bear"
    return "neutral"


def _rank_step1_universe(
    snapshot: dict,
    positions: list,
    sentiment: dict,
    prebreakout_candidates: list,
    eod_watchlist_symbols: list,
    urgent_news_context: Optional[list],
    earnings_plays: list,
    market_tier: str = "neutral",
    max_symbols: int = 150,
) -> list[str]:
    """
    Reduce prompt noise without becoming a narrow whitelist.
    Mandatory catalyst/position symbols are always included, then the highest
    momentum, volume, news, and reversal names fill the remaining slots.
    """
    if len(snapshot) <= max_symbols:
        return list(snapshot.keys())

    available = set(snapshot.keys())
    must_include: set[str] = set()
    must_include.update(p.symbol for p in positions if getattr(p, "symbol", None))
    must_include.update(s for s in (eod_watchlist_symbols or []) if isinstance(s, str))
    must_include.update(p.get("symbol") for p in (prebreakout_candidates or []) if isinstance(p, dict) and p.get("symbol"))
    must_include.update(p.get("symbol") for p in (earnings_plays or []) if isinstance(p, dict) and p.get("symbol"))
    must_include.update(sym for sym, count in (sentiment or {}).items() if count > 0)
    for item in urgent_news_context or []:
        must_include.update(sym for sym in item.get("symbols", []) if isinstance(sym, str))

    must_include = {sym.upper() for sym in must_include if sym and sym.upper() in available}
    prebreakout_syms = {p.get("symbol") for p in (prebreakout_candidates or []) if isinstance(p, dict)}
    earnings_syms = {p.get("symbol") for p in (earnings_plays or []) if isinstance(p, dict)}

    def score_symbol(sym: str) -> float:
        data = snapshot.get(sym, {})
        five_day = data.get("five_day_change_pct")
        rel_vol = data.get("relative_volume", 1.0) or 1.0
        price = data.get("current_price") or 0
        score = 0.0
        if isinstance(five_day, (int, float)):
            score += min(abs(five_day), 35) * 2.0
            if five_day > 0:
                score += min(five_day, 20) * 0.5
        score += min(max(rel_vol - 1.0, 0), 5) * 12.0
        score += min((sentiment or {}).get(sym, 0), 5) * 15.0
        if sym in must_include:
            score += 1000.0
        if sym in prebreakout_syms:
            score += 80.0
        if sym in earnings_syms:
            score += 40.0
        if 0 < price < 5:
            score += 6.0
        return score

    selected: list[str] = []

    def add_many(symbols: list[str], limit: int) -> None:
        for sym in symbols:
            if len(selected) >= max_symbols or limit <= 0:
                return
            if sym in snapshot and sym not in selected:
                selected.append(sym)
                limit -= 1

    ranked = sorted(snapshot.keys(), key=score_symbol, reverse=True)
    add_many([sym for sym in ranked if sym in must_include], len(must_include))

    def five_day(sym: str) -> float:
        value = snapshot.get(sym, {}).get("five_day_change_pct")
        return float(value) if isinstance(value, (int, float)) else 0.0

    def rel_vol(sym: str) -> float:
        value = snapshot.get(sym, {}).get("relative_volume", 1.0)
        return float(value) if isinstance(value, (int, float)) else 1.0

    gainers = sorted(snapshot.keys(), key=five_day, reverse=True)
    losers = sorted(snapshot.keys(), key=five_day)
    volume = sorted(snapshot.keys(), key=rel_vol, reverse=True)
    news = sorted((sentiment or {}).keys(), key=lambda sym: (sentiment or {}).get(sym, 0), reverse=True)
    prebreakouts = [sym for sym in prebreakout_syms if sym in snapshot]
    earnings = [sym for sym in earnings_syms if sym in snapshot]

    add_many(news, 30)
    add_many(prebreakouts, 20)
    add_many(earnings, 15)
    add_many(volume, 35)
    if market_tier == "bear":
        add_many(losers, 45)
        add_many(gainers, 25)
    elif market_tier == "bull":
        add_many(gainers, 45)
        add_many(losers, 25)
    else:
        add_many(gainers, 35)
        add_many(losers, 35)
    add_many(ranked, max_symbols)
    return selected[:max_symbols]


def _load_watermarks() -> dict:
    """Restore long-position high watermarks from persistent cache after a server restart."""
    cached = cache_get("position_watermarks")
    return cached if isinstance(cached, dict) else {}


def _load_short_watermarks() -> dict:
    """Restore short-position low watermarks from persistent cache after a server restart."""
    cached = cache_get("short_position_watermarks")
    return cached if isinstance(cached, dict) else {}


_position_high_watermarks: dict = _load_watermarks()    # symbol → peak price seen while holding long position
_short_low_watermarks: dict = _load_short_watermarks()  # symbol → lowest price seen while holding short position
_previous_positions: dict = {}         # symbol → {qty, avg_entry_price, entry_time, side} for close detection
_current_cycle_id: Optional[str] = None  # UUID refreshed each cycle for activity log grouping
_ai_sold_symbols: set = set()          # symbols sold by AI this cycle — guards against double reserve on Alpaca lag
# Staircase scale-out tracking: symbol → number of scale-outs already taken.
# Thresholds: 1st = 20%, 2nd = 35%, 3rd = 50%, 4th = 65% ...  (each +15pp)
# Prevents the cascade bug where remaining position sits at same P&L% and
# fires the scale-out rule every cycle until position is nearly zero.
_scale_out_counts: dict = {}           # symbol → int (how many scale-outs taken so far)
_cover_short_counts: dict = {}         # symbol → int (how many partial covers taken so far)
_pyramid_counts: dict = {}             # symbol → int (how many pyramid adds taken; max 2)
# Re-entry tracking: when we scale out partially, record the qty BEFORE the trim so we
# can re-buy a portion if the stock pulls back to MA20 and momentum resumes.
_pre_scaleout_qty: dict = {}           # symbol → int (qty held before first scale-out)
_earnings_day_positions: set = set()   # symbols entered as earnings plays — forced EOD exit
_recent_exit_cache: dict = {}          # symbol -> {timestamp, reason, pnl_pct}


def _save_watermarks() -> None:
    cache_set("position_watermarks", _position_high_watermarks, 86400)        # 24h TTL
    cache_set("short_position_watermarks", _short_low_watermarks, 86400)      # 24h TTL


def _load_daily_trade_count() -> dict:
    """
    Restore today's executed trade count from trade_log after a server restart.
    Prevents afternoon pressure from misfiring because the in-memory counter reset to 0.
    Falls back to empty dict silently if DB is unavailable.
    """
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return {}
        today = datetime.now(timezone.utc).date()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM trade_log WHERE action IN ('buy', 'sell', 'short') AND timestamp::date = %s",
                (today,),
            )
            row = cur.fetchone()
            count = int(row[0]) if row else 0
        logger.info(f"Restored daily trade count from DB: {count} trades today")
        return {today: count} if count > 0 else {}
    except Exception as e:
        logger.warning(f"Could not restore daily trade count ({e}), starting from 0")
        return {}


# Restore state that must survive server restarts
_position_high_watermarks = _load_watermarks()
_daily_trade_count: dict = _load_daily_trade_count()


def _classify_exit_reason(raw_reason: str, is_short: bool = False, fraction: float = 1.0) -> str:
    text = (raw_reason or "").lower()
    if "trailing stop" in text:
        return "trailing_stop"
    if "momentum decaying" in text or "momentum decay" in text:
        return "momentum_decay"
    if "stale" in text or "redeploying capital" in text:
        return "stale_exit"
    if "earnings play" in text and "forced exit" in text:
        return "earnings_eod_exit"
    if "gap down" in text:
        return "gap_down_exit"
    if "stop loss" in text or "loss" in text:
        return "stop_loss"
    if "rsi" in text:
        return "rsi_cover" if is_short else "rsi_exit"
    if "profit" in text or "take profit" in text:
        return "partial_take_profit" if fraction < 1.0 else "take_profit"
    if is_short:
        return "short_cover_partial" if fraction < 1.0 else "short_cover"
    return "ai_sell"


def _record_recent_exit(symbol: str, reason: str, pnl_pct: Optional[float] = None) -> None:
    _recent_exit_cache[symbol] = {
        "timestamp": datetime.now(timezone.utc),
        "reason": reason,
        "pnl_pct": pnl_pct,
    }


def _recent_exit_block(symbol: str, action: str) -> Optional[str]:
    if action not in ("buy", "short"):
        return None
    info = _recent_exit_cache.get(symbol)
    if not info:
        return None
    mins = (datetime.now(timezone.utc) - info["timestamp"]).total_seconds() / 60
    if mins > 180:
        _recent_exit_cache.pop(symbol, None)
        return None
    reason = str(info.get("reason") or "")
    pnl_pct = info.get("pnl_pct")
    if mins < 45:
        return f"{symbol} exited {mins:.0f} min ago ({reason}) — churn cooldown active"
    if reason in {"stop_loss", "trailing_stop", "momentum_decay", "gap_down_exit"} and mins < 180:
        return f"{symbol} recently exited via {reason} {mins:.0f} min ago — re-entry blocked until setup resets"
    if pnl_pct is not None and pnl_pct < 0 and mins < 180:
        return f"{symbol} was closed at {pnl_pct:.2f}% {mins:.0f} min ago — avoid revenge re-entry"
    return None


def _quality_gate_reason(
    symbol: str,
    action: str,
    entry_score: Optional[float],
    market_tier: str,
    strategy_key: str,
    high_conviction: bool = False,
) -> Optional[str]:
    if action not in ("buy", "short") or entry_score is None:
        return None
    min_score = 58.0 if strategy_key == "aggressive" else 63.0
    if action == "short" and market_tier == "bull":
        min_score = max(min_score, 72.0)
    elif action == "short" and market_tier == "neutral":
        min_score = max(min_score, 66.0)
    elif action == "buy" and market_tier == "bear":
        min_score = max(min_score, 68.0)
    if high_conviction and action == "buy":
        min_score -= 6.0
    if high_conviction and action == "short" and market_tier != "bull":
        min_score -= 4.0
    min_score = max(min_score, 52.0)
    if entry_score < min_score:
        return f"{symbol} {action} blocked — entry score {entry_score:.1f} below {min_score:.1f} threshold for {market_tier} regime"
    return None


def _news_confirmation_block(
    symbol: str,
    action: str,
    relative_volume: float,
    macd_hist_pct: Optional[float],
    high_conviction: bool = False,
) -> Optional[str]:
    if action not in ("buy", "short"):
        return None
    try:
        from services.news_stream import get_cached_news
        recent_articles = [
            article for article in get_cached_news(limit=120, max_age_minutes=90)
            if symbol in (article.get("symbols") or []) and article.get("event_impact") == "high"
        ]
    except Exception:
        return None

    if not recent_articles:
        return None

    bullish = any(a.get("event_sentiment") == "bullish" for a in recent_articles)
    bearish = any(a.get("event_sentiment") == "bearish" for a in recent_articles)
    mixed = bullish and bearish
    rel_vol_floor = 1.8 if high_conviction else 1.4

    if mixed:
        return f"{symbol} has mixed fresh high-impact news — skipping until direction is clearer"
    if relative_volume < rel_vol_floor:
        return (
            f"{symbol} has fresh high-impact news but only {relative_volume:.1f}x relative volume — "
            "wait for stronger confirmation"
        )
    if action == "buy":
        if bearish:
            return f"{symbol} buy blocked — fresh high-impact news is bearish"
        if macd_hist_pct is not None and macd_hist_pct < 0:
            return f"{symbol} buy blocked — news is fresh but MACD confirmation is still negative"
    if action == "short":
        if bullish:
            return f"{symbol} short blocked — fresh high-impact news is bullish"
        if macd_hist_pct is not None and macd_hist_pct > 0:
            return f"{symbol} short blocked — news is fresh but downside momentum has not confirmed"
    return None


def _predictive_block(
    symbol: str,
    action: str,
    predictive_expectancy_pct: Optional[float],
    predictive_trades: int,
    high_conviction: bool = False,
) -> Optional[str]:
    if action not in ("buy", "short"):
        return None
    if predictive_expectancy_pct is None or predictive_trades < 3:
        return None
    if predictive_expectancy_pct <= -0.35 and not high_conviction:
        return (
            f"{symbol} {action} blocked — historical predictive prior is {predictive_expectancy_pct:.2f}% "
            f"over {predictive_trades} trades"
        )
    return None


def get_status() -> TradingStatus:
    next_run_in = None
    if _next_run_at:
        delta = (_next_run_at - datetime.now(timezone.utc)).total_seconds()
        next_run_in = max(0, int(delta))
    return TradingStatus(
        is_running=_is_running,
        last_analysis_at=_last_analysis_at,
        next_run_in_seconds=next_run_in,
    )


def get_latest_analysis() -> Optional[AIAnalysis]:
    global _latest_analysis
    if _latest_analysis is not None:
        return _latest_analysis
    # Attempt to restore from the persistent cache after a restart
    try:
        cached = cache_get("latest_ai_decision")
        if cached:
            _latest_analysis = AIAnalysis(**cached)
            logger.info("Restored latest AI analysis from Postgres cache.")
            return _latest_analysis
    except Exception as e:
        logger.warning(f"Could not restore latest_ai_decision from cache: {e}")
    return None


def request_urgent_cycle(symbols: list[str], reason: str) -> None:
    """
    Wake the normal trading loop early after high-impact news.
    Only queues urgent context during market hours — if the market is closed,
    the article is already saved to the news cache and Claude will see it in
    Step 2 via realtime_news_by_symbol when the market reopens. No need to
    flag it as urgent since we can't trade on it right now anyway.
    The next cycle still uses the standard risk checks and order guards.
    """
    global _wake_event, _urgent_news_context
    clean_symbols = [s for s in symbols if isinstance(s, str)][:10]

    if not alpaca_service.is_market_open():
        logger.info(f"Urgent news received outside market hours — saved to cache only (not queued as urgent): {reason[:120]}")
        return

    _urgent_news_context.append({
        "symbols": clean_symbols,
        "reason": reason[:500],
        "received_at": datetime.now(timezone.utc).isoformat(),
    })
    _urgent_news_context = _urgent_news_context[-10:]
    if _wake_event is not None:
        _wake_event.set()
    logger.info(f"Urgent trading cycle requested: {reason}")


async def run_premarket_scan():
    """
    Runs at 9:00-9:30 AM EST before market open.
    Identifies top opportunities from overnight news and pre-market movers.
    Stores results in cache so the first trading cycle uses them immediately.
    """
    try:
        now_utc = datetime.now(timezone.utc)
        if not (13 <= now_utc.hour < 14):
            return

        logger.info("Running pre-market scan (13:00-14:00 UTC)...")

        universe = alpaca_service.get_tradeable_universe()
        news_articles = alpaca_service.get_news(limit=20)
        macro = get_macro_context()

        # Build news headlines list
        news_headlines = [
            f"[{art.get('source', '')}] [{', '.join(art.get('symbols', [])[:3])}] {art['headline']}"
            for art in news_articles[:15]
            if art.get("headline")
        ]

        # Derive sentiment from news articles
        sentiment = {}
        for art in news_articles:
            for sym in art.get("symbols", []):
                if sym in universe:
                    sentiment[sym] = sentiment.get(sym, 0) + 1

        # Top movers from news (symbols with most mentions)
        top_movers = sorted(sentiment.items(), key=lambda x: x[1], reverse=True)[:5]
        top_movers_str = ", ".join([f"{sym}({cnt})" for sym, cnt in top_movers]) or "none identified"

        # Format a simple pre-market context string
        market_regime = macro.get("market_regime", "unknown").upper()
        premarket_context = (
            f"Pre-market scan: regime={market_regime}, "
            f"top news movers={top_movers_str}, "
            f"articles={len(news_articles)}"
        )

        cache_set(
            "premarket_scan",
            {
                "headlines": news_headlines,
                "sentiment": sentiment,
                "macro_regime": macro.get("market_regime"),
                "scanned_at": datetime.now(timezone.utc).isoformat(),
            },
            7200,  # 2 hour TTL
        )

        logger.info(f"Pre-market scan complete: {premarket_context}")
    except Exception as e:
        logger.warning(f"Pre-market scan failed (non-fatal): {e}")


async def run_trading_cycle():
    global _last_analysis_at, _next_run_at, _latest_analysis, \
           _position_high_watermarks, _previous_positions, _current_cycle_id, \
           _ai_sold_symbols, _earnings_day_positions, _urgent_news_context
    import uuid
    _current_cycle_id = str(uuid.uuid4())[:8]  # short 8-char id per cycle

    logger.info(f"Running trading cycle [cycle={_current_cycle_id}]...")

    _cycle_urgent_news = _urgent_news_context[:] if _urgent_news_context else None
    if _cycle_urgent_news:
        logger.info(f"Urgent news context for cycle: {_cycle_urgent_news[-3:]}")

    try:
        if not alpaca_service.is_market_open():
            logger.info("Market is closed. Skipping cycle.")
            return
        if _cycle_urgent_news:
            _urgent_news_context = []

        # Circuit breaker: block new buys if down more than daily loss limit.
        # Sells and scale-outs are still allowed — we want to exit losing positions even on bad days.
        loop = asyncio.get_running_loop()
        account = await loop.run_in_executor(None, alpaca_service.get_account)
        # Bug fix: cast to float — Alpaca SDK can return Decimal or string in some versions,
        # which causes TypeError on f"{:.2f}" formatting and comparison with negative threshold.
        _day_pl_pct = float(account.day_pl_percent)
        circuit_breaker_active = _day_pl_pct < -_risk_settings["daily_loss_limit_pct"]
        if circuit_breaker_active:
            logger.warning(
                f"⛔ Circuit breaker active: down {_day_pl_pct:.2f}% today "
                f"(limit: -{_risk_settings['daily_loss_limit_pct']}%). "
                f"New buys blocked — exits and scale-outs still allowed."
            )
            log_circuit_breaker(
                day_pl_percent=_day_pl_pct,
                portfolio_value=account.portfolio_value,
                limit_pct=_risk_settings["daily_loss_limit_pct"],
            )
            log_bot_activity("circuit_breaker",
                             f"Daily loss limit hit: down {_day_pl_pct:.2f}% (limit -{_risk_settings['daily_loss_limit_pct']}%). New buys blocked.",
                             cycle_id=_current_cycle_id)
            await manager.broadcast({
                "type": "circuit_breaker",
                "data": {
                    "reason": f"Daily loss limit of {_risk_settings['daily_loss_limit_pct']}% hit — new buys blocked",
                    "day_pl_percent": _day_pl_pct,
                }
            })

        # ── Macro calendar event check ────────────────────────────────────────
        # FOMC: halve all new position sizes, block entries after 1:45 PM ET
        # CPI / Jobs: reduce new position sizes 30%
        try:
            _macro_event = get_macro_event_today()
            _macro_size_mult = _macro_event["position_size_multiplier"]
            if _macro_event["event"]:
                logger.info(f"Macro event today: {_macro_event['message']}")
                log_bot_activity("macro_event", _macro_event["message"], cycle_id=_current_cycle_id)
        except Exception as _me:
            logger.warning(f"get_macro_event_today failed (non-fatal): {_me}")
            _macro_event = {"event": None, "position_size_multiplier": 1.0, "message": ""}
            _macro_size_mult = 1.0

        positions = await loop.run_in_executor(None, alpaca_service.get_positions)
        universe = await loop.run_in_executor(None, alpaca_service.get_tradeable_universe)
        logger.info(f"Universe: {len(universe)} stocks — 100% market-driven (top movers, volume, news, sectors)")
        # Lightweight snapshot for broad scan (Step 1) — price + 5-day change + closing prices for indicators
        snapshot_light = await loop.run_in_executor(
            None, functools.partial(alpaca_service.get_market_snapshot_light, universe)
        )

        # ── Pre-breakout scan: find stocks near MA20 before they extend ──────
        # Pulls yesterday's EOD watchlist symbols so the thesis carries forward
        # into today's scan — those stocks get guaranteed slots regardless of
        # how many universe candidates score higher.
        sentiment = {}  # initialized here so pre-breakout scan can use it; recomputed after news fetch
        _prebreakout_candidates = []
        _eod_watchlist_syms = []
        try:
            from services.breakout_scanner import scan_prebreakout_candidates
            from services.eod_analysis_service import get_latest_eod_report as _get_eod
            try:
                _eod = _get_eod()
                if _eod and isinstance(_eod, dict):
                    _wl = (_eod.get("analysis") or {}).get("tomorrow_watchlist") or []
                    _all_watchlist = [w.get("symbol") for w in _wl if w.get("symbol")]
                    # Filter out symbols currently held as shorts — they shouldn't
                    # appear as buy candidates and cause "no long position found" warnings
                    _short_symbols = {p.symbol for p in positions if getattr(p, "side", "long") == "short"}
                    _eod_watchlist_syms = [s for s in _all_watchlist if s not in _short_symbols]
                    if _eod_watchlist_syms:
                        logger.info(f"EOD watchlist for breakout scan: {_eod_watchlist_syms}")
                    if _short_symbols & set(_all_watchlist):
                        logger.info(f"EOD watchlist filtered out short positions: {_short_symbols & set(_all_watchlist)}")
            except Exception as _eod_err:
                logger.debug(f"Could not load EOD watchlist for scanner (non-fatal): {_eod_err}")

            _prebreakout_candidates = scan_prebreakout_candidates(
                snapshot_light,
                top_n=10,
                watchlist_symbols=_eod_watchlist_syms,
                sentiment=sentiment,
            )
        except Exception as _pbs_err:
            logger.warning(f"Pre-breakout scan failed (non-fatal): {_pbs_err}")

        # ── Fetch multi-source news ONCE per cycle ──
        # Used for both sentiment scoring AND passing headlines to the AI prompt
        news_articles = []
        try:
            news_articles = await loop.run_in_executor(
                None, functools.partial(alpaca_service.get_news, symbols=universe[:10], limit=30)
            )
            logger.info(f"Fetched {len(news_articles)} news articles from multi-source feed")
        except Exception as e:
            logger.warning(f"Could not fetch news: {e}")

        # Derive sentiment from news: count articles mentioning each symbol
        sentiment = {}
        for art in news_articles:
            for sym in art.get("symbols", []):
                if sym in universe:
                    sentiment[sym] = sentiment.get(sym, 0) + 1

        # Top headlines for AI context (most recent 15)
        news_headlines = [
            f"[{art.get('source', '')}] [{', '.join(art.get('symbols', [])[:3])}] {art['headline']}"
            for art in news_articles[:15]
            if art.get("headline")
        ]

        _gather_results = await asyncio.gather(
            loop.run_in_executor(None, get_macro_context),
            loop.run_in_executor(None, get_sector_rotation),
            return_exceptions=True,
        )
        macro = _gather_results[0] if not isinstance(_gather_results[0], Exception) else {
            "market_regime": "neutral", "vix_level": "normal", "spy_trend": "neutral", "guidance": ""
        }
        sector_info = _gather_results[1] if not isinstance(_gather_results[1], Exception) else ""
        if isinstance(_gather_results[0], Exception):
            logger.warning(f"get_macro_context failed (using neutral defaults): {_gather_results[0]}")
        if isinstance(_gather_results[1], Exception):
            logger.warning(f"get_sector_rotation failed (using empty): {_gather_results[1]}")
        market_tier = _derive_market_tier(macro)
        logger.info(f"Execution market tier: {market_tier}")

        # Sector momentum scores — used to boost/reduce conviction per symbol
        from services.sector_momentum import get_sector_momentum_scores, get_sector_context_for_symbols
        sector_scores = {}
        sector_context = {}
        try:
            sector_scores = await loop.run_in_executor(
                None, functools.partial(get_sector_momentum_scores, lookback_days=3)
            )
            leading_sectors = [f"{s}({v:+.1f}%)" for s, v in sorted(sector_scores.items(), key=lambda x: x[1], reverse=True)[:3]]
            logger.info(f"Sector momentum — Leading: {', '.join(leading_sectors)}")
        except Exception as e:
            logger.warning(f"get_sector_momentum_scores failed (non-fatal): {e}")
        try:
            sector_context = get_sector_context_for_symbols(universe[:30], sector_scores)
        except Exception as e:
            logger.warning(f"get_sector_context_for_symbols failed (non-fatal): {e}")

        # Recent trade outcomes — fed back to Claude so it learns from past decisions
        from services.db import get_recent_trade_outcomes
        recent_trades = []
        try:
            recent_trades = await loop.run_in_executor(None, functools.partial(get_recent_trade_outcomes, limit=10))
        except Exception as e:
            logger.warning(f"Could not fetch trade history (non-fatal): {e}")

        earnings_map = await loop.run_in_executor(
            None, functools.partial(get_upcoming_earnings, universe)
        )

        # Fetch strategy early — needed for scale-out logic and entry confirmation
        from services.strategy import get_strategy as _get_strategy
        strat = _get_strategy()
        strategy_key = strat["key"]

        # Earnings play candidates — small pre-earnings run-up plays
        from services.earnings import get_earnings_play_candidates
        earnings_plays = []
        try:
            earnings_plays = get_earnings_play_candidates(universe, earnings_map)
            if earnings_plays:
                logger.info(f"Earnings play candidates: {[p['symbol'] for p in earnings_plays]}")
        except Exception as e:
            logger.warning(f"Earnings plays failed (non-fatal): {e}")

        step1_cap = 150
        if _cycle_urgent_news or len(news_articles) >= 20:
            step1_cap = 180
        elif macro.get("market_regime") == "volatile":
            step1_cap = 130
        elif len(news_articles) < 5:
            step1_cap = 120
        if market_tier == "bear":
            step1_cap = max(step1_cap, 160)

        step1_symbols = _rank_step1_universe(
            snapshot=snapshot_light,
            positions=positions,
            sentiment=sentiment,
            prebreakout_candidates=_prebreakout_candidates,
            eod_watchlist_symbols=_eod_watchlist_syms,
            urgent_news_context=_cycle_urgent_news,
            earnings_plays=earnings_plays,
            market_tier=market_tier,
            max_symbols=step1_cap,
        )
        snapshot_step1 = {sym: snapshot_light[sym] for sym in step1_symbols if sym in snapshot_light}
        filtered_symbol_set = set(snapshot_step1.keys())
        sentiment_step1 = {sym: count for sym, count in sentiment.items() if sym in filtered_symbol_set}
        earnings_map_step1 = {sym: timing for sym, timing in earnings_map.items() if sym in filtered_symbol_set}
        news_headlines_step1 = [
            f"[{art.get('source', '')}] [{', '.join(art.get('symbols', [])[:3])}] {art['headline']}"
            for art in news_articles
            if art.get("headline") and (filtered_symbol_set & set(art.get("symbols") or []))
        ][:15] or news_headlines[:10]
        logger.info(
            f"Step 1 universe filtered: {len(snapshot_light)} → {len(snapshot_step1)} symbols "
            f"(cap={step1_cap}, tier={market_tier}, must-have/news/prebreakout preserved)"
        )

        try:
            sector_context = get_sector_context_for_symbols(step1_symbols[:150], sector_scores)
        except Exception as e:
            logger.warning(f"get_sector_context_for_symbols failed for filtered universe (non-fatal): {e}")

        geo = await loop.run_in_executor(None, get_geopolitical_context)
        trend_forecast = get_trend_forecast(macro, geo)
        logger.info(f"Macro: {macro['market_regime']} | VIX: {macro['vix_level']} | SPY: {macro['spy_trend']}")
        logger.info(f"Geopolitical risk: {geo['risk_level'].upper()} (score={geo['risk_score']}) | Themes: {geo['dominant_themes']}")

        # ── Detect position closes: symbols that were held last cycle but are gone now ──
        current_symbols = {p.symbol for p in positions}

        # ── Detect NEW positions that appeared since last cycle ──
        # This catches limit/bracket orders that filled between cycles (submitted cycle N,
        # filled async before cycle N+1). Since we skip log_position_open for unfilled orders
        # at submission time, we must detect and log them here when they appear in Alpaca.
        # Guard: skip on first cycle after restart (_previous_positions is empty) to avoid
        # logging duplicate position_open entries for positions that are already in the DB.
        old_symbols = set(_previous_positions.keys())
        if old_symbols:  # only run if we have a baseline from the previous cycle
            for sym in current_symbols:
                pos = next((p for p in positions if p.symbol == sym), None)
                if pos:
                    prev_meta = _previous_positions.get(sym, {})
                    is_new_symbol = sym not in old_symbols
                    needs_entry_log = is_new_symbol or not prev_meta.get("entry_logged", True)
                    if needs_entry_log:
                        logger.info(f"New position detected (filled between cycles): {sym} @ ${pos.avg_entry_price:.2f}")
                        log_position_open(
                            symbol=sym,
                            entry_price=pos.avg_entry_price,
                            quantity=int(float(pos.qty)),
                            strategy=prev_meta.get("strategy", strategy_key),
                            claude_reasoning=prev_meta.get("claude_reasoning") or "Limit order filled between cycles — entry logged on position detection",
                            market_regime=prev_meta.get("market_regime") or macro.get("market_regime"),
                            side=pos.side,
                            entry_rsi=prev_meta.get("entry_rsi"),
                            entry_macd_hist_pct=prev_meta.get("entry_macd_hist_pct"),
                            entry_score=prev_meta.get("entry_score"),
                        )
                        if sym in _previous_positions:
                            _previous_positions[sym]["entry_logged"] = True
                    if (
                        pos.side == "long"
                        and prev_meta.get("partial_exit")
                        and not prev_meta.get("exit_legs_attached", False)
                    ):
                        attached = alpaca_service.ensure_partial_exit_orders(
                            symbol=sym,
                            qty=int(float(pos.qty)),
                            entry_price=float(pos.avg_entry_price),
                            take_profit_pct=float(prev_meta.get("take_profit_pct") or _risk_settings["take_profit_pct"]),
                            stop_loss_pct=float(prev_meta.get("stop_loss_pct") or _risk_settings["stop_loss_pct"]),
                        )
                        if attached and sym in _previous_positions:
                            _previous_positions[sym]["exit_legs_attached"] = True
                    if pos.side == "long":
                        _position_high_watermarks[sym] = pos.current_price
                        _save_watermarks()
        for sym, prev in _previous_positions.items():
            if sym not in current_symbols:
                # Skip symbols handled by an AI sell — reserve + log already applied.
                # Use a persistent set rather than exit_reason (which can be overwritten
                # by _previous_positions rebuild if Alpaca propagation lags a cycle).
                if sym in _ai_sold_symbols:
                    logger.debug(f"Skipping cycle-detect close for {sym} — already handled as ai_sell")
                    _ai_sold_symbols.discard(sym)
                    continue

                # Position was closed — determine exit price from Alpaca orders
                exit_price = prev.get("avg_entry_price", 0)  # fallback
                is_prev_short = prev.get("side") == "short"
                try:
                    recent_orders = alpaca_service.get_orders(limit=20)
                    # Longs close via a sell order; shorts close via a buy-to-cover order
                    close_side = "buy" if is_prev_short else "sell"
                    for o in recent_orders:
                        if o.symbol == sym and o.side == close_side and o.filled_avg_price:
                            exit_price = float(o.filled_avg_price)
                            break
                except Exception:
                    pass
                log_position_close(
                    symbol=sym,
                    exit_price=exit_price,
                    exit_reason=prev.get("exit_reason", "unknown"),
                    entry_price=prev.get("avg_entry_price"),
                    quantity=prev.get("qty"),
                    entry_time=prev.get("entry_time"),
                    side=prev.get("side", "long"),
                    strategy=prev.get("strategy"),
                    claude_reasoning=prev.get("claude_reasoning"),
                    market_regime=prev.get("market_regime"),
                    entry_rsi=prev.get("entry_rsi"),
                    entry_macd_hist_pct=prev.get("entry_macd_hist_pct"),
                    entry_score=prev.get("entry_score"),
                )
                _record_recent_exit(sym, prev.get("exit_reason", "unknown"), prev.get("last_pl_pct"))
                # ── Profit reserve: take % of realized gain before it re-enters trading pool ──
                try:
                    reserve_pct = float(_risk_settings.get("profit_reserve_pct", 0.0)) / 100.0
                    entry_p = prev.get("avg_entry_price") or 0
                    qty_p   = prev.get("qty") or 0
                    if reserve_pct > 0 and entry_p > 0 and qty_p > 0:
                        realized = (exit_price - entry_p) * qty_p if prev.get("side", "long") == "long" else (entry_p - exit_price) * qty_p
                        if realized > 0:
                            add_to_reserve(round(realized * reserve_pct, 2))
                except Exception as _re:
                    logger.warning(f"Profit reserve calc failed (non-fatal): {_re}")
                log_bot_activity("position_closed",
                                 f"Position closed: {sym} exit=${exit_price:.2f} reason={prev.get('exit_reason','unknown')}",
                                 symbol=sym, cycle_id=_current_cycle_id)
                # Clean up earnings-play tracking — position is gone, no EOD exit needed
                _earnings_day_positions.discard(sym)
                if sym in _position_high_watermarks:
                    del _position_high_watermarks[sym]

        # Bug fix: flush stale _ai_sold_symbols entries — symbols that are still in
        # positions should stay guarded; symbols gone for >1 cycle should be cleared
        # so future re-entries don't get their log_position_close permanently suppressed.
        _ai_sold_symbols = {s for s in _ai_sold_symbols if s in current_symbols}

        # Update _previous_positions for next cycle — include side so short closes are identified correctly.
        # Bug fix: preserve entry_time from previous cycle so the 48h stale-exit rule can actually
        # trigger. Old code reset entry_time to now() every cycle, so hours_held was always ~0.17h.
        _previous_positions = {
            p.symbol: {
                "qty": int(float(p.qty)),
                "avg_entry_price": p.avg_entry_price,
                "entry_time": (
                    _previous_positions.get(p.symbol, {}).get("entry_time")
                    or datetime.now(timezone.utc)
                ),
                "exit_reason": _previous_positions.get(p.symbol, {}).get("exit_reason", "unknown"),
                "side": p.side,
                "strategy": _previous_positions.get(p.symbol, {}).get("strategy"),
                "claude_reasoning": _previous_positions.get(p.symbol, {}).get("claude_reasoning"),
                "market_regime": _previous_positions.get(p.symbol, {}).get("market_regime"),
                "entry_rsi": _previous_positions.get(p.symbol, {}).get("entry_rsi"),
                "entry_macd_hist_pct": _previous_positions.get(p.symbol, {}).get("entry_macd_hist_pct"),
                "entry_score": _previous_positions.get(p.symbol, {}).get("entry_score"),
                "partial_exit": _previous_positions.get(p.symbol, {}).get("partial_exit", False),
                "take_profit_pct": _previous_positions.get(p.symbol, {}).get("take_profit_pct"),
                "stop_loss_pct": _previous_positions.get(p.symbol, {}).get("stop_loss_pct"),
                "entry_logged": _previous_positions.get(p.symbol, {}).get("entry_logged", True),
                "exit_legs_attached": _previous_positions.get(p.symbol, {}).get("exit_legs_attached", False),
                "last_pl_pct": p.unrealized_pl_percent,
            }
            for p in positions
        }

        # ── Update watermarks for all open positions ──
        held_symbols = {p.symbol for p in positions}
        for position in positions:
            cp = position.current_price
            if position.side == "short":
                # Track lowest price for shorts (profit as price falls)
                prev_low = _short_low_watermarks.get(position.symbol, float("inf"))
                if cp < prev_low:
                    _short_low_watermarks[position.symbol] = cp
            else:
                # Track highest price for longs (profit as price rises)
                prev_high = _position_high_watermarks.get(position.symbol, 0)
                if cp > prev_high:
                    _position_high_watermarks[position.symbol] = cp
        # Clean up watermarks and scale-out counters for closed positions
        for sym in list(_position_high_watermarks.keys()):
            if sym not in held_symbols:
                del _position_high_watermarks[sym]
        for sym in list(_short_low_watermarks.keys()):
            if sym not in held_symbols:
                del _short_low_watermarks[sym]
        for sym in list(_scale_out_counts.keys()):
            if sym not in held_symbols:
                del _scale_out_counts[sym]
        for sym in list(_pyramid_counts.keys()):
            if sym not in held_symbols:
                del _pyramid_counts[sym]
        for sym in list(_pre_scaleout_qty.keys()):
            if sym not in held_symbols:
                del _pre_scaleout_qty[sym]
        # Persist long watermarks so trailing stops survive server restarts
        _save_watermarks()

        # ── Correlated-position groups ────────────────────────────────────────
        # In AGGRESSIVE mode: allow up to 2 correlated leveraged ETFs — the whole
        # point of aggressive is maximum upside exposure in bull regimes.
        # In BALANCED/CONSERVATIVE: cap at 1 to avoid doubling sector risk.
        _CORR_GROUPS: list[set] = [
            {"TQQQ", "SOXL", "TECL", "FNGU", "SPXL", "UPRO", "UDOW", "TNA"},   # leveraged long
            {"SQQQ", "SOXS", "TECS", "FNGS", "SPXS", "SPXU", "SDOW", "TZA"},   # leveraged short
        ]
        _corr_cap = 3 if strategy_key == "aggressive" else 1

        def _corr_group(sym: str) -> Optional[set]:
            for g in _CORR_GROUPS:
                if sym in g:
                    return g
            return None

        held_symbols_set = {p.symbol for p in positions}

        # ── Tradeable cash: subtract profit reserve so pyramid/scale logic can use it ──
        # Defined early so both pyramiding (below) and Claude's analysis (further down) share the same value.
        _reserved = get_reserved_cash()
        _tradeable_cash = max(0.0, float(account.cash) - _reserved)
        if _reserved > float(account.cash):
            logger.warning(f"⚠️ Profit reserve (${_reserved:,.2f}) exceeds available cash (${account.cash:,.2f}) — tradeable cash is $0.")
        elif _reserved > 0:
            logger.info(f"Cash available for trading: ${_tradeable_cash:,.2f} (${_reserved:,.2f} in profit reserve)")

        # ── PDT guard: cap tradeable cash to day trading buying power on margin accounts ──
        # On margin accounts under $25k, Alpaca enforces a day trading buying power limit
        # that is much lower than total cash. Without this cap the bot sizes trades it
        # cannot actually execute, causing "insufficient day trading buying power" errors.
        # On cash accounts daytrading_buying_power is 0 — skip the cap in that case.
        _dt_bp = float(account.daytrading_buying_power)
        if _dt_bp > 0 and _dt_bp < _tradeable_cash:
            logger.info(f"PDT cap: day trading buying power ${_dt_bp:,.2f} < cash ${_tradeable_cash:,.2f} — capping tradeable cash")
            _tradeable_cash = _dt_bp

        # Track symbols pyramided this cycle so re-entry logic skips them
        # (position.qty is stale pre-pyramid; re-entry on same cycle would compute wrong qty)
        _pyramided_this_cycle: set = set()

        # ── Time-of-day gate (pre-scale-out check) ───────────────────────────
        # Bug fix: was checked AFTER scale-out/momentum-decay order submissions.
        # Pre-market orders (window="closed") were firing trailing stops and stale
        # exits before market open. Now checked here so no orders go out when closed.
        # "exits_only" still allows scale-outs/trailing stops (intentional).
        from services.entry_timing import is_good_trading_window
        window_mode, window_reason = is_good_trading_window()
        if window_mode == "closed":
            logger.info(f"Trading window: {window_reason} — skipping cycle")
            return
        entries_allowed = (window_mode == "full")
        if not entries_allowed:
            logger.info(f"Trading window: {window_reason}")

        # ── Pyramid: add to winning positions (aggressive only) ──────────────
        # Two tiers — let the best trades compound as far as they'll go:
        #
        # Tier 1 (first add): P&L 5-18%, MACD > 0.05, RSI < 72 → +25% of current qty
        # Tier 2 (second add): P&L 22-40%, MACD > 0.03, RSI < 65 → +15% of current qty
        #
        # Guards: no scale-out taken yet (still in profit-building phase),
        # max 2 pyramids per position, never spend >40% of cash on one pyramid.
        if strategy_key == "aggressive":
            for position in positions:
                if position.side == "short":
                    continue
                pnl = position.unrealized_pl_percent
                pyrs_taken = _pyramid_counts.get(position.symbol, 0)
                # Max 2 pyramid adds per position
                if pyrs_taken >= 2:
                    continue
                # Never pyramid once scale-outs have started — position is in exit phase
                if _scale_out_counts.get(position.symbol, 0) > 0:
                    continue

                sym_data_p = snapshot_light.get(position.symbol, {})
                cp = sym_data_p.get("closing_prices", [])
                if not cp:
                    continue
                try:
                    from services.indicators import compute_macd as _cmacd
                    _mh = (_cmacd(cp) or {}).get("histogram", 0.0)
                except Exception:
                    _mh = 0.0
                _rsi_p = (compute_rsi(cp) or 50.0)
                p_price = sym_data_p.get("current_price") or position.current_price

                # Tier 1: sweet spot — clearly working, not yet overbought
                tier1 = (5.0 <= pnl <= 21.0) and _mh > 0.05 and _rsi_p < 75 and pyrs_taken == 0
                # Tier 2: extended winner — add smaller on continued strength
                # Bug fix: was 22.0 leaving an 18-22% dead zone. Now 19.0 closes the gap.
                tier2 = (19.0 <= pnl <= 40.0) and _mh > 0.03 and _rsi_p < 65 and pyrs_taken == 1

                if tier1:
                    add_pct, tier_label = 0.25, "Tier-1"
                elif tier2:
                    add_pct, tier_label = 0.15, "Tier-2"
                else:
                    continue

                add_qty = max(1, int(float(position.qty) * add_pct))
                # Skip pyramid if it adds less than 3 shares — a 1-share add on a tiny
                # position is meaningless noise (wastes an order, barely moves the position).
                # This happens when position.qty is small (e.g. 1 share × 25% = 0 → min=1).
                if add_qty < 3:
                    logger.debug(f"Pyramid skipped: {position.symbol} add_qty={add_qty} < 3 — position too small to pyramid meaningfully")
                    continue
                add_cost = add_qty * p_price
                if add_cost <= _tradeable_cash * 0.40:  # never spend >40% of cash on one pyramid
                    pyramid_reason = (
                        f"{position.symbol} pyramid {tier_label}: up {pnl:.1f}%, "
                        f"MACD={_mh:.3f}, RSI={_rsi_p:.0f} — adding {add_qty} shares (+{add_pct*100:.0f}%)"
                    )
                    logger.info(f"PYRAMID {tier_label}: {pyramid_reason}")
                    log_bot_activity("approved", pyramid_reason,
                                     symbol=position.symbol, cycle_id=_current_cycle_id)
                    pyr_order = alpaca_service.submit_market_order(
                        symbol=position.symbol, qty=add_qty, side="buy"
                    )
                    if pyr_order:
                        _pyramid_counts[position.symbol] = pyrs_taken + 1
                        _tradeable_cash -= add_cost
                        _pyramided_this_cycle.add(position.symbol)
                        await manager.broadcast({"type": "order_filled",
                                                 "data": pyr_order.model_dump(mode="json")})

        # ── Scale-out: review existing positions for profit-taking / loss cuts ──
        for position in positions:
            sym_data = snapshot_light.get(position.symbol, {})
            closing_prices = sym_data.get("closing_prices", [])
            # compute_rsi returns None when <15 bars — fall back to 50 (neutral, no rule fires)
            rsi = (compute_rsi(closing_prices) or 50.0) if closing_prices else 50.0
            # ── Dynamic trailing stop: tighten as profit grows ────────────────
            # Locks in progressively more of a big gain rather than letting a
            # 30% winner give back 5% before stopping out.
            _pnl_for_trail = position.unrealized_pl_percent
            if _pnl_for_trail >= 25.0:
                trail_pct = 0.02   # up 25%+ → 2% trail — protect most of the gain
            elif _pnl_for_trail >= 15.0:
                trail_pct = 0.03   # up 15-25% → 3% trail
            else:
                trail_pct = float(_risk_settings.get("stop_loss_pct", 0.05))

            is_short = position.side == "short"

            # ── Gap-down exit: opening window (9:30–9:55 AM ET) ──────────────
            # If a held long is already down >2% in the first 25 minutes of trading,
            # exit immediately. Opening weakness is highly predictive — stocks falling
            # hard at open rarely recover same day. Don't wait for the -6% circuit breaker.
            if not is_short:
                try:
                    from zoneinfo import ZoneInfo
                    _now_et_gap = datetime.now(ZoneInfo("America/New_York"))
                except Exception:
                    _now_et_gap = datetime.now(timezone.utc) - timedelta(hours=4)
                _mins_gap = _now_et_gap.hour * 60 + _now_et_gap.minute
                _in_opening_window = (9 * 60 + 30) <= _mins_gap <= (9 * 60 + 55)
                if _in_opening_window and position.unrealized_pl_percent < -2.0:
                    gap_reason = (
                        f"{position.symbol} down {position.unrealized_pl_percent:.1f}% "
                        f"in opening window (first 25 min) — gap-down exit before further loss"
                    )
                    logger.info(f"GAP_DOWN_EXIT: {gap_reason}")
                    log_bot_activity("scale_out", gap_reason,
                                     symbol=position.symbol, cycle_id=_current_cycle_id)
                    if position.symbol in _previous_positions:
                        _previous_positions[position.symbol]["exit_reason"] = "gap_down_exit"
                    # Cancel any open bracket/GTC orders first
                    try:
                        open_orders = alpaca_service.get_orders(limit=20)
                        for o in open_orders:
                            if o.symbol == position.symbol and o.side == "sell" and o.status in ("new", "partially_filled", "accepted"):
                                alpaca_service.cancel_order(o.id)
                    except Exception:
                        pass
                    gap_order = alpaca_service.submit_market_order(
                        symbol=position.symbol,
                        qty=int(float(position.qty)),
                        side="sell",
                    )
                    if gap_order:
                        _ai_sold_symbols.add(position.symbol)
                        try:
                            reserve_pct = float(_risk_settings.get("profit_reserve_pct", 0.0)) / 100.0
                            _prev_gap = _previous_positions.get(position.symbol, {})
                            entry_p_gap = _prev_gap.get("avg_entry_price") or float(position.avg_entry_price or 0)
                            exit_p_gap = float(position.current_price or entry_p_gap)
                            qty_gap = int(float(position.qty))
                            if reserve_pct > 0 and entry_p_gap > 0 and qty_gap > 0:
                                realized_gap = (exit_p_gap - entry_p_gap) * qty_gap
                                if realized_gap > 0:
                                    add_to_reserve(round(realized_gap * reserve_pct, 2))
                        except Exception as _re:
                            logger.warning(f"Profit reserve (gap_down) failed (non-fatal): {_re}")
                        await manager.broadcast({"type": "order_filled", "data": gap_order.model_dump(mode="json")})
                        await manager.broadcast({"type": "ai_analysis", "data": {
                            "reasoning": gap_reason, "last_action": "sell",
                            "symbol": position.symbol,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }})
                    continue  # skip all other exit checks for this position

            # ── Stale-position cleanup ────────────────────────────────────────
            # A position flat for 48+ hours is dead money — exit and redeploy.
            # Only triggers for longs between -1% and +3% P&L (truly flat).
            # Does NOT fire when already being handled by scale-out or trailing stop.
            if not is_short:
                entry_time = _previous_positions.get(position.symbol, {}).get("entry_time")
                if entry_time:
                    hours_held = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
                    pnl = position.unrealized_pl_percent
                    if hours_held >= 48 and -1.0 <= pnl <= 3.0:
                        stale_reason = (
                            f"{position.symbol} held {hours_held:.0f}h flat at {pnl:+.1f}% "
                            f"— redeploying capital to better opportunities"
                        )
                        log_bot_activity("scale_out", stale_reason,
                                         symbol=position.symbol, cycle_id=_current_cycle_id)
                        if position.symbol in _previous_positions:
                            _previous_positions[position.symbol]["exit_reason"] = "stale_exit"
                        try:
                            open_orders = alpaca_service.get_orders(limit=20)
                            for o in open_orders:
                                if o.symbol == position.symbol and o.side == "sell" and o.status in ("new", "partially_filled", "accepted"):
                                    alpaca_service.cancel_order(o.id)
                        except Exception:
                            pass
                        order = alpaca_service.submit_market_order(
                            symbol=position.symbol, qty=int(float(position.qty)), side="sell"
                        )
                        if order:
                            try:
                                reserve_pct = float(_risk_settings.get("profit_reserve_pct", 0.0)) / 100.0
                                _prev_s = _previous_positions.get(position.symbol, {})
                                entry_p_s = _prev_s.get("avg_entry_price") or float(position.avg_entry_price or 0)
                                exit_p_s  = float(position.current_price or entry_p_s)
                                qty_s     = int(float(position.qty))
                                if reserve_pct > 0 and entry_p_s > 0 and qty_s > 0:
                                    realized_s = (exit_p_s - entry_p_s) * qty_s
                                    if realized_s > 0:
                                        add_to_reserve(round(realized_s * reserve_pct, 2))
                            except Exception as _re:
                                logger.warning(f"Profit reserve (stale_exit) failed (non-fatal): {_re}")
                            await manager.broadcast({"type": "order_filled", "data": order.model_dump(mode="json")})
                            await manager.broadcast({"type": "ai_analysis", "data": {
                                "reasoning": stale_reason, "last_action": "sell",
                                "symbol": position.symbol, "timestamp": datetime.now(timezone.utc).isoformat(),
                            }})
                        continue  # skip scale-out check for this position

            # ── Earnings play forced EOD exit ─────────────────────────────────
            # Positions entered as earnings plays must exit by 3:45 PM ET —
            # never hold through the actual earnings report after close.
            # Two-pronged check so server restarts don't bypass this rule:
            # 1. Symbol is in _earnings_day_positions (in-memory, set at order time)
            # 2. OR: position entered today + still has earnings today/tomorrow in earnings_map
            #    (covers restart scenario where in-memory set was wiped)
            _ep_entry_time = _previous_positions.get(position.symbol, {}).get("entry_time")
            _ep_entered_today = (
                _ep_entry_time is not None and
                _ep_entry_time.date() == datetime.now(timezone.utc).date()
            )
            _ep_has_earnings = (
                earnings_map is not None and
                earnings_map.get(position.symbol) == "today/tomorrow"
            )
            _is_earnings_play = (
                position.symbol in _earnings_day_positions or
                (_ep_entered_today and _ep_has_earnings)
            )
            if _is_earnings_play:
                try:
                    from zoneinfo import ZoneInfo
                    _now_et = datetime.now(ZoneInfo("America/New_York"))
                except Exception:
                    from datetime import timedelta
                    _now_et = datetime.now(timezone.utc) - timedelta(hours=4)
                _mins_et = _now_et.hour * 60 + _now_et.minute
                if _mins_et >= 15 * 60 + 45:  # 3:45 PM ET
                    _ep_exit_reason = (
                        f"{position.symbol} earnings play — forced exit at 3:45 PM ET "
                        f"(P&L: {position.unrealized_pl_percent:+.1f}%) — never hold through report"
                    )
                    logger.info(f"EARNINGS EOD EXIT: {_ep_exit_reason}")
                    log_bot_activity("scale_out", _ep_exit_reason,
                                     symbol=position.symbol, cycle_id=_current_cycle_id)
                    if position.symbol in _previous_positions:
                        _previous_positions[position.symbol]["exit_reason"] = "earnings_eod_exit"
                    _ep_side = "sell" if position.side == "long" else "buy"
                    _ep_order = alpaca_service.submit_market_order(
                        symbol=position.symbol, qty=int(float(position.qty)), side=_ep_side
                    )
                    if _ep_order:
                        _earnings_day_positions.discard(position.symbol)
                        try:
                            reserve_pct = float(_risk_settings.get("profit_reserve_pct", 0.0)) / 100.0
                            _prev_ep = _previous_positions.get(position.symbol, {})
                            entry_p_ep = _prev_ep.get("avg_entry_price") or float(position.avg_entry_price or 0)
                            exit_p_ep  = float(position.current_price or entry_p_ep)
                            qty_ep     = int(float(position.qty))
                            is_short_ep = position.side == "short"
                            if reserve_pct > 0 and entry_p_ep > 0 and qty_ep > 0:
                                realized_ep = (exit_p_ep - entry_p_ep) * qty_ep if not is_short_ep else (entry_p_ep - exit_p_ep) * qty_ep
                                if realized_ep > 0:
                                    add_to_reserve(round(realized_ep * reserve_pct, 2))
                        except Exception as _re:
                            logger.warning(f"Profit reserve (earnings_eod) failed (non-fatal): {_re}")
                        await manager.broadcast({"type": "order_filled", "data": _ep_order.model_dump(mode="json")})
                        await manager.broadcast({"type": "ai_analysis", "data": {
                            "reasoning": _ep_exit_reason, "last_action": _ep_side,
                            "symbol": position.symbol, "timestamp": datetime.now(timezone.utc).isoformat(),
                        }})
                    continue  # skip other exit checks for this position

            # ── Momentum-decay exit ───────────────────────────────────────────
            # When MACD histogram turns clearly negative while position still has
            # some profit (1-15%), exit before momentum fully reverses.
            # Only for longs in normal conditions (not when deeply overbought — that's
            # handled by scale-out — and not when trailing stop is about to fire).
            if not is_short:
                macd_data = {}
                try:
                    from services.indicators import compute_macd
                    macd_data = compute_macd(closing_prices) if closing_prices else {}
                except Exception:
                    pass
                macd_hist = macd_data.get("histogram", 0.0)
                pnl = position.unrealized_pl_percent
                # Threshold -0.3: requires a clear, sustained negative histogram —
                # not just intraday noise. A brief dip to -0.05 or -0.1 is normal
                # consolidation; -0.3% is genuine momentum reversal.
                _hist_pct = (macd_hist / position.current_price * 100) if position.current_price and position.current_price > 0 else 0
                if _hist_pct < -0.3 and 1.0 <= pnl <= 15.0:
                    decay_reason = (
                        f"{position.symbol} momentum decaying: MACD hist {macd_hist:.3f} ({_hist_pct:.2f}%) "
                        f"while up {pnl:.1f}% — exiting before reversal"
                    )
                    logger.info(f"MOMENTUM_DECAY: {decay_reason}")
                    log_bot_activity("scale_out", decay_reason,
                                     symbol=position.symbol, cycle_id=_current_cycle_id)
                    if position.symbol in _previous_positions:
                        _previous_positions[position.symbol]["exit_reason"] = "momentum_decay"
                    try:
                        open_orders = alpaca_service.get_orders(limit=20)
                        for o in open_orders:
                            if o.symbol == position.symbol and o.side == "sell" and o.status in ("new", "partially_filled", "accepted"):
                                alpaca_service.cancel_order(o.id)
                    except Exception:
                        pass
                    order = alpaca_service.submit_market_order(
                        symbol=position.symbol, qty=int(float(position.qty)), side="sell"
                    )
                    if order:
                        try:
                            reserve_pct = float(_risk_settings.get("profit_reserve_pct", 0.0)) / 100.0
                            _prev_md = _previous_positions.get(position.symbol, {})
                            entry_p_md = _prev_md.get("avg_entry_price") or float(position.avg_entry_price or 0)
                            exit_p_md  = float(position.current_price or entry_p_md)
                            qty_md     = int(float(position.qty))
                            if reserve_pct > 0 and entry_p_md > 0 and qty_md > 0:
                                realized_md = (exit_p_md - entry_p_md) * qty_md
                                if realized_md > 0:
                                    add_to_reserve(round(realized_md * reserve_pct, 2))
                        except Exception as _re:
                            logger.warning(f"Profit reserve (momentum_decay) failed (non-fatal): {_re}")
                        await manager.broadcast({"type": "order_filled", "data": order.model_dump(mode="json")})
                        await manager.broadcast({"type": "ai_analysis", "data": {
                            "reasoning": decay_reason, "last_action": "sell",
                            "symbol": position.symbol, "timestamp": datetime.now(timezone.utc).isoformat(),
                        }})
                    continue

            if is_short:
                # Short position: check if we should cover (buy back)
                cover, fraction, reason = should_cover_short(
                    symbol=position.symbol,
                    position_unrealized_pl_percent=position.unrealized_pl_percent,
                    rsi=rsi,
                    low_watermark=_short_low_watermarks.get(position.symbol),
                    current_price=position.current_price,
                    trail_pct=trail_pct,
                    strategy_key=strategy_key,
                )
                # ── Staircase gate for shorts: prevent cascading covers ────────────
                # After covering 50% at 10%, the remaining position still shows the
                # same P&L% (same entry price), so should_cover_short fires again
                # every cycle. Gate requires each successive cover to reach 15pp higher:
                # 10% → 25% → 40% → 55% ... Trailing stop / RSI / stop-loss bypass.
                if cover and fraction < 1.0 and "trailing stop" not in reason.lower() and "stop loss" not in reason.lower() and "rsi" not in reason.lower():
                    count = _cover_short_counts.get(position.symbol, 0)
                    next_threshold = 10.0 + count * 15.0  # 10, 25, 40, 55 ...
                    pnl = position.unrealized_pl_percent
                    if pnl < next_threshold:
                        logger.debug(
                            f"Cover gate: {position.symbol} at {pnl:.1f}% — "
                            f"next partial cover requires {next_threshold:.0f}% (count={count})"
                        )
                        cover = False
                should_exit = cover
            else:
                scale_out, fraction, reason = should_scale_out(
                    position_unrealized_pl_percent=position.unrealized_pl_percent,
                    rsi=rsi,
                    symbol=position.symbol,
                    strategy_key=strategy_key,
                    high_watermark=_position_high_watermarks.get(position.symbol),
                    current_price=position.current_price,
                    trail_pct=trail_pct,
                )
                # ── Staircase gate: prevent cascading scale-outs ──────────────────
                # After the first scale-out at 20%, the remaining position still
                # shows the same P&L% (same entry price). Without a gate,
                # should_scale_out fires every cycle, halving the position each time
                # until it's nearly gone. Instead we require each successive trim to
                # reach a 15pp higher threshold: 20% → 35% → 50% → 65% ...
                if scale_out and "trailing stop" not in reason.lower() and "loss" not in reason.lower() and "rsi" not in reason.lower():
                    count = _scale_out_counts.get(position.symbol, 0)
                    next_threshold = 20.0 + count * 15.0  # 20, 35, 50, 65 ...
                    pnl = position.unrealized_pl_percent
                    if pnl < next_threshold:
                        logger.debug(
                            f"Scale-out gate: {position.symbol} at {pnl:.1f}% — "
                            f"next trim requires {next_threshold:.0f}% (count={count})"
                        )
                        scale_out = False  # suppress until position runs further
                should_exit = scale_out

            if should_exit:
                exit_qty = max(1, int(abs(float(position.qty)) * fraction))
                action_label = "cover_short" if is_short else "scale_out"
                order_side = "buy" if is_short else "sell"
                logger.info(f"{action_label.upper()} triggered: {reason} — {'covering' if is_short else 'selling'} {exit_qty} shares")
                if position.symbol in _previous_positions:
                    _previous_positions[position.symbol]["exit_reason"] = _classify_exit_reason(
                        reason, is_short=is_short, fraction=fraction
                    )
                log_bot_activity(
                    action_label, reason,
                    symbol=position.symbol, cycle_id=_current_cycle_id
                )
                # Advance staircase counter so next trim needs 15pp more gain
                if action_label == "scale_out":
                    # Record pre-trim qty on first scale-out (for re-entry logic)
                    if _scale_out_counts.get(position.symbol, 0) == 0:
                        _pre_scaleout_qty[position.symbol] = int(float(position.qty))
                    _scale_out_counts[position.symbol] = _scale_out_counts.get(position.symbol, 0) + 1
                    logger.info(
                        f"Scale-out #{_scale_out_counts[position.symbol]} for {position.symbol} — "
                        f"next trim at {20.0 + _scale_out_counts[position.symbol] * 15.0:.0f}%"
                    )
                elif action_label == "cover_short" and fraction < 1.0:
                    _cover_short_counts[position.symbol] = _cover_short_counts.get(position.symbol, 0) + 1
                    logger.info(
                        f"Cover #{_cover_short_counts[position.symbol]} for {position.symbol} — "
                        f"next partial cover at {10.0 + _cover_short_counts[position.symbol] * 15.0:.0f}%"
                    )
                # Cancel any open GTC bracket orders before submitting a market exit
                # Shorts: cancel GTC limit buy (cover order placed at entry via submit_short_order)
                # Longs: cancel GTC limit sell / stop-loss legs placed at entry via bracket order
                # Without this, the orphaned GTC order can fill later and create an unintended position
                try:
                    open_orders = alpaca_service.get_orders(limit=20)
                    cancel_side = "buy" if is_short else "sell"
                    for o in open_orders:
                        if o.symbol == position.symbol and o.side == cancel_side and o.status in ("new", "partially_filled", "accepted"):
                            alpaca_service.cancel_order(o.id)
                            logger.info(f"Cancelled open {cancel_side} order {o.id} for {position.symbol} before engine exit")
                except Exception as ce:
                    logger.warning(f"Could not cancel open orders for {position.symbol}: {ce}")
                order = alpaca_service.submit_market_order(
                    symbol=position.symbol,
                    qty=exit_qty,
                    side=order_side,
                )
                if order:
                    # ── Profit reserve on scale-outs and trailing stops ──
                    # Previously only AI sells and cycle-detect closes added to reserve.
                    # Scale-outs/trailing stops realize gains too — they should contribute.
                    try:
                        reserve_pct = float(_risk_settings.get("profit_reserve_pct", 0.0)) / 100.0
                        prev = _previous_positions.get(position.symbol, {})
                        entry_p = prev.get("avg_entry_price") or float(position.avg_entry_price or 0)
                        exit_p  = float(position.current_price or entry_p)
                        if reserve_pct > 0 and entry_p > 0 and exit_qty > 0:
                            realized = (exit_p - entry_p) * exit_qty if not is_short else (entry_p - exit_p) * exit_qty
                            if realized > 0:
                                add_to_reserve(round(realized * reserve_pct, 2))
                                logger.info(f"Profit reserve: +${realized * reserve_pct:.2f} from {action_label} {position.symbol}")
                    except Exception as _re:
                        logger.warning(f"Profit reserve ({action_label}) failed (non-fatal): {_re}")

                    await manager.broadcast({"type": "order_filled", "data": order.model_dump(mode="json")})
                    await manager.broadcast({
                        "type": "ai_analysis",
                        "data": {
                            "reasoning": reason,
                            "last_action": order_side,
                            "symbol": position.symbol,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        },
                    })

        # ── Re-entry after scale-out (aggressive only) ───────────────────────
        # After trimming a winner, if the stock pulls back to near MA20 and
        # MACD turns positive again, we re-buy a portion to get back into the move.
        # Condition: still holding (partial position remains), scale-out taken,
        # price near MA20 (within 6%), MACD histogram > 0, RSI < 58 (not overbought).
        if strategy_key == "aggressive":
            for position in positions:
                if position.side == "short":
                    continue
                sym = position.symbol
                if _scale_out_counts.get(sym, 0) == 0:
                    continue  # no scale-out taken yet, nothing to re-enter
                # Skip if we just pyramided this cycle — position.qty is stale pre-pyramid
                if sym in _pyramided_this_cycle:
                    continue
                pnl = position.unrealized_pl_percent
                if pnl <= 0:
                    continue  # don't re-enter a losing position
                orig_qty = _pre_scaleout_qty.get(sym, 0)
                current_qty = int(float(position.qty))
                if orig_qty <= current_qty:
                    continue  # already back to original size
                reentry_qty = min(orig_qty - current_qty, max(1, int(orig_qty * 0.25)))
                sd = snapshot_light.get(sym, {})
                cp_re = sd.get("closing_prices", [])
                if not cp_re:
                    continue
                try:
                    from services.indicators import compute_macd as _cmacd_re, compute_moving_averages as _cma_re
                    _mh_re = (_cmacd_re(cp_re) or {}).get("histogram", 0.0)
                    _ma20_re = _cma_re(cp_re).get("ma20")
                except Exception:
                    continue
                _rsi_re = (compute_rsi(cp_re) or 50.0)
                _price_re = sd.get("current_price") or position.current_price
                # Must be near MA20 (pulled back properly) with resuming momentum
                near_ma20 = _ma20_re and (_price_re <= _ma20_re * 1.10)
                if not (near_ma20 and _mh_re > 0.0 and _rsi_re < 65):
                    continue
                reentry_cost = reentry_qty * _price_re
                if reentry_cost > _tradeable_cash * 0.35:
                    continue
                reentry_reason = (
                    f"{sym} re-entry after scale-out: pulled back to MA20 "
                    f"(${_ma20_re:.2f}), MACD={_mh_re:.3f}, RSI={_rsi_re:.0f} "
                    f"— re-buying {reentry_qty} shares (was {orig_qty}, now {current_qty})"
                )
                logger.info(f"RE-ENTRY: {reentry_reason}")
                log_bot_activity("approved", reentry_reason, symbol=sym, cycle_id=_current_cycle_id)
                re_order = alpaca_service.submit_market_order(symbol=sym, qty=reentry_qty, side="buy")
                if re_order:
                    _tradeable_cash -= reentry_cost
                    # Bug fix: reset to 1 not 0 — requires 35% gain before next scale-out.
                    # Resetting to 0 creates an infinite scale-out/re-entry loop every cycle.
                    _scale_out_counts[sym] = 1
                    _pre_scaleout_qty.pop(sym, None)
                    await manager.broadcast({"type": "order_filled", "data": re_order.model_dump(mode="json")})

        # ── Time-of-day filter ──
        # window_mode / entries_allowed already set above (before scale-out block)

        # ── Daily trade floor: configurable via /api/risk/settings ──
        now_utc = datetime.now(timezone.utc)
        trades_today = _daily_trade_count.get(now_utc.date(), 0)
        min_trades = int(_risk_settings.get("min_daily_trades", 2))
        # afternoon_pressure_hour is in EST; UTC offset is +4 (EDT) or +5 (EST)
        # Using +4 (EDT, summer) as default — close enough for this purpose
        # Bug fix: use ET timezone for afternoon pressure (DST-aware) instead of hardcoded UTC+4
        try:
            from zoneinfo import ZoneInfo as _ZI
            _now_et = datetime.now(_ZI("America/New_York"))
            _now_et_hour = _now_et.hour
        except Exception:
            _now_et_hour = now_utc.hour - 4  # fallback: approximate EDT
        pressure_hour_est = int(_risk_settings.get("afternoon_pressure_hour", 14))
        afternoon_pressure = (_now_et_hour >= pressure_hour_est and trades_today < min_trades)
        if afternoon_pressure:
            logger.info(f"Afternoon pressure: only {trades_today}/{min_trades} trades today — lowering thresholds")

        # Per-cycle counter for new opens (buy/short). Sells, covers, and trailing stops never count.
        _cycle_open_count = 0
        _max_trades_this_cycle = int(_risk_settings.get("max_trades_per_cycle", 6))
        logger.info(f"Trade cap this cycle: {_max_trades_this_cycle}")
        # Symbols approved as earnings plays this cycle — added to _earnings_day_positions
        # only AFTER the order is submitted (avoids polluting the set with blocked trades)
        _earnings_play_pending: set = set()

        # _tradeable_cash already computed above (before pyramiding block) — reused here

        # Pass cooldown symbols so Claude stops nominating already-rejected stocks
        _cooldown_syms = get_cooldown_symbols()

        # Also exclude symbols already at concentration cap — no point nominating
        # SOXL/ACHR etc. in Step 1 if the position can't grow anyway.
        # Handles both regular stocks (max_position_pct) and penny stocks (<$5, max_penny_position_pct).
        try:
            _port_val = float(account.portfolio_value)
            _std_cap_dollars  = _port_val * strat["max_position_pct"]
            _penny_cap_dollars = _port_val * (_risk_settings.get("max_penny_position_pct", 3.0) / 100.0)
            _capped_syms = []
            for _cp in positions:
                _cp_price = float(_cp.current_price)
                _cp_val   = abs(float(_cp.market_value))
                _cap_limit = _penny_cap_dollars if _cp_price < 5.0 else _std_cap_dollars
                if _cp_val >= _cap_limit * 0.95:
                    _capped_syms.append(_cp.symbol)
            if _capped_syms:
                logger.info(f"Concentration-capped symbols (excluded from Step 1): {_capped_syms}")
            _cooldown_syms = list(set(_cooldown_syms + _capped_syms))
        except Exception as _cap_exc:
            logger.debug(f"Cap-symbol exclusion skipped (non-fatal): {_cap_exc}")

        if _cooldown_syms:
            logger.info(f"Rejection cooldown symbols (excluded from Step 1): {_cooldown_syms}")

        decisions = await loop.run_in_executor(
            None,
            functools.partial(
                claude_service.analyze_and_decide,
                market_snapshot=snapshot_step1,
                positions=positions,
                account_cash=_tradeable_cash,
                portfolio_value=account.portfolio_value,
                sentiment=sentiment_step1,
                macro=macro,
                sector_info=sector_info,
                earnings_map=earnings_map_step1,
                geo_context=geo,
                trend_forecast=trend_forecast,
                news_headlines=news_headlines_step1,
                full_data_fetcher=lambda symbols: alpaca_service.get_market_snapshot(symbols),
                sector_context=sector_context,
                recent_trades=recent_trades,
                earnings_plays=earnings_plays,
                afternoon_pressure=afternoon_pressure,
                rejected_symbols=_cooldown_syms,
                prebreakout_candidates=_prebreakout_candidates,
                urgent_news_context=_cycle_urgent_news,
            )
        )

        _last_analysis_at = datetime.now(timezone.utc)

        # Sort decisions: sells first, then buys/shorts
        # This ensures rotation sells always free up cash before buys consume the cycle cap
        ACTION_PRIORITY = {"sell": 0, "short": 1, "buy": 1, "hold": 2}
        decisions = sorted(decisions, key=lambda d: ACTION_PRIORITY.get(d.action, 2))

        # Broadcast + log each decision
        for decision in decisions:
            _latest_analysis = AIAnalysis(
                reasoning=decision.reasoning,
                last_action=decision.action,
                symbol=decision.symbol,
                timestamp=_last_analysis_at,
            )
            cache_set("latest_ai_decision", _latest_analysis.model_dump(mode="json"), 86400)
            _rsn_up = (decision.reasoning or "").upper()
            _conf_val = "high" if "[HIGH]" in _rsn_up else ("low" if "[LOW]" in _rsn_up else "medium")
            log_trade_decision({
                "timestamp":       _last_analysis_at,
                "action":          decision.action,
                "symbol":          decision.symbol,
                "quantity":        decision.quantity,
                "reasoning":       decision.reasoning,
                "confidence":      _conf_val,
                "market_regime":   macro.get("market_regime"),
                "geo_risk":        geo.get("risk_level"),
                "take_profit_pct": decision.take_profit_pct,
                "stop_loss_pct":   decision.stop_loss_pct,
                "partial_exit":    decision.partial_exit,
            })
            logger.info(f"AI decision: {decision.action} {decision.symbol} x{decision.quantity}")
            await manager.broadcast({"type": "ai_analysis", "data": _latest_analysis.model_dump(mode="json")})

            if decision.action not in ("buy", "sell", "short") or not decision.symbol or not decision.quantity:
                continue

            # ── Correlated-position cap ───────────────────────────────────────
            # Aggressive: allow up to 2 correlated leveraged ETFs (TQQQ + SOXL is a
            # valid bull-regime stack). Balanced/conservative: cap at 1.
            if decision.action in ("buy", "short"):
                grp = _corr_group(decision.symbol)
                if grp:
                    already_held = [s for s in held_symbols_set if s != decision.symbol and s in grp]
                    if len(already_held) >= _corr_cap:
                        corr_msg = (
                            f"{decision.action.upper()} {decision.symbol} blocked — "
                            f"already hold {already_held} in same correlated group (cap={_corr_cap})"
                        )
                        logger.info(f"Correlated cap: {corr_msg}")
                        log_bot_activity("entry_rejected", corr_msg,
                                         symbol=decision.symbol, cycle_id=_current_cycle_id)
                        continue

            # ── Same-symbol conflict check ────────────────────────────────────
            # Block going long on a symbol we're already short, and vice versa.
            # SOXL long + SOXL short = net zero exposure while paying twice the spread.
            if decision.action in ("buy", "short"):
                _existing_conflict = next((p for p in positions if p.symbol == decision.symbol), None)
                if _existing_conflict:
                    _is_conflict = (
                        (decision.action == "buy" and _existing_conflict.side == "short") or
                        (decision.action == "short" and _existing_conflict.side == "long")
                    )
                    if _is_conflict:
                        _conflict_msg = (
                            f"{decision.action.upper()} {decision.symbol} blocked — "
                            f"already holding {_existing_conflict.side} position. "
                            f"Conflicting long+short on same symbol creates net-zero exposure."
                        )
                        logger.info(f"Conflict check: {_conflict_msg}")
                        log_bot_activity("entry_rejected", _conflict_msg,
                                         symbol=decision.symbol, cycle_id=_current_cycle_id)
                        continue

            # ── Opening window: block new entries during first 15 min but allow exits ──
            if decision.action in ("buy", "short") and not entries_allowed:
                logger.info(
                    f"Opening window: skipping {decision.action.upper()} {decision.symbol} "
                    f"— new entries blocked until 9:45 AM EST"
                )
                continue

            # ── FOMC rate decision gate: block new entries after 1:45 PM ET ────
            # Fed announces at 2 PM ET — 15-min buffer eliminates announcement spike risk.
            if decision.action in ("buy", "short") and is_fomc_entry_blocked():
                _fomc_block_msg = (
                    f"{decision.action.upper()} {decision.symbol} blocked — "
                    f"FOMC rate decision at 2 PM ET today. No new entries after 1:45 PM ET."
                )
                logger.info(f"FOMC gate: {_fomc_block_msg}")
                log_bot_activity("entry_rejected", _fomc_block_msg,
                                 symbol=decision.symbol, cycle_id=_current_cycle_id)
                continue

            # ── Circuit breaker: block new buys AND shorts when daily loss limit hit ──
            if decision.action in ("buy", "short") and circuit_breaker_active:
                logger.info(f"Circuit breaker: skipping {decision.action.upper()} {decision.symbol} — daily loss limit active")
                log_bot_activity("circuit_breaker",
                                 f"{decision.action.upper()} {decision.symbol} blocked — daily loss limit active",
                                 symbol=decision.symbol, cycle_id=_current_cycle_id)
                continue

            # ── Earnings play duplicate block ─────────────────────────────────
            # Once a stock is entered as an earnings play and is in _earnings_day_positions,
            # block any further buys. The 5% cap applies per-buy, so without this guard
            # the bot accumulates 3× the intended exposure across 3 cycles.
            if decision.action in ("buy", "short") and decision.symbol in _earnings_day_positions:
                _ep_dup_msg = (
                    f"{decision.action.upper()} {decision.symbol} blocked — already holding as earnings play "
                    f"(forced EOD exit at 3:45 PM ET). No additional buys allowed."
                )
                logger.info(f"Earnings play duplicate: {_ep_dup_msg}")
                log_bot_activity("earnings_block", _ep_dup_msg,
                                 symbol=decision.symbol, cycle_id=_current_cycle_id)
                continue

            # ── Earnings prediction gate: replace hard block with AI directional call ──
            # Instead of always blocking, ask Claude to predict bullish/bearish/uncertain.
            # - bullish/bearish + any confidence → small position (5% max), forced EOD exit
            # - uncertain → hard block (old behaviour)
            # Never hold through the actual report — all earnings plays must exit by 3:45 PM ET.
            if decision.action in ("buy", "short") and earnings_map and earnings_map.get(decision.symbol) == "today/tomorrow":
                try:
                    from services.claude_service import predict_earnings_direction
                    _sym_data = snapshot_light.get(decision.symbol, {})
                    _ep = predict_earnings_direction(
                        symbol=decision.symbol,
                        snapshot_data=_sym_data,
                        sentiment=sentiment,
                        news_headlines=news_headlines,
                    )
                except Exception as _ep_err:
                    logger.warning(f"Earnings prediction error for {decision.symbol}: {_ep_err}")
                    _ep = {"direction": "uncertain", "confidence": "low", "reasoning": "error"}

                _ep_direction   = _ep.get("direction", "uncertain")
                _ep_confidence  = _ep.get("confidence", "low")
                _ep_reasoning   = _ep.get("reasoning", "")

                if _ep_direction == "uncertain":
                    # Uncertain earnings — aggressive mode gets a tiny 2% position
                    # with forced EOD exit to capture intraday momentum while avoiding
                    # overnight gap risk. Balanced mode still blocks outright.
                    if strategy_key == "aggressive":
                        _ep_price_u = _sym_data.get("current_price")
                        if not _ep_price_u or _ep_price_u <= 0:
                            logger.warning(f"Earnings uncertain {decision.symbol}: no price, blocking")
                            continue
                        _ep_max_qty_u = max(1, int(float(account.portfolio_value) * 0.02 / _ep_price_u))
                        capped_qty_u = min(decision.quantity or _ep_max_qty_u, _ep_max_qty_u)
                        decision = decision.model_copy(update={"quantity": capped_qty_u})
                        _earnings_play_pending.add(decision.symbol)
                        logger.info(
                            f"EARNINGS UNCERTAIN (aggressive): {decision.action.upper()} {decision.symbol} "
                            f"x{capped_qty_u} — tiny 2% position, forced EOD exit"
                        )
                        log_bot_activity("approved",
                                         f"EARNINGS UNCERTAIN: {decision.action.upper()} {decision.symbol} x{capped_qty_u} "
                                         f"— 2% position cap, forced EOD exit (intraday momentum only)",
                                         symbol=decision.symbol, cycle_id=_current_cycle_id)
                        # Fall through to normal order execution
                    else:
                        logger.warning(
                            f"EARNINGS BLOCK: {decision.symbol} — prediction uncertain, gap risk too high."
                        )
                        log_bot_activity("earnings_block",
                                         f"{decision.action.upper()} {decision.symbol} blocked — earnings today/tomorrow, "
                                         f"AI prediction: uncertain. Gap risk too high.",
                                         symbol=decision.symbol, cycle_id=_current_cycle_id)
                        await manager.broadcast({"type": "ai_analysis", "data": {
                            "reasoning": f"Earnings block: {decision.symbol} — prediction uncertain, binary gap risk. Skipping.",
                            "last_action": "waiting",
                            "symbol": decision.symbol,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }})
                        continue
                else:
                    # Directional signal — allow small position, cap at 5% portfolio, force EOD exit
                    _ep_price = _sym_data.get("current_price")
                    if not _ep_price or _ep_price <= 0:
                        logger.warning(f"Earnings play {decision.symbol}: no valid price, blocking")
                        log_bot_activity("earnings_block",
                                         f"{decision.symbol} earnings play skipped — price unavailable",
                                         symbol=decision.symbol, cycle_id=_current_cycle_id)
                        continue
                    # Use portfolio_value for consistent sizing (same as all other position limits)
                    _ep_max_qty = max(1, int(float(account.portfolio_value) * 0.05 / _ep_price))
                    # Override direction: if AI predicted bearish but decision is BUY, flip to short (and vice versa)
                    _ep_action = "buy" if _ep_direction == "bullish" else "short"
                    if _ep_action != decision.action:
                        logger.info(
                            f"Earnings play {decision.symbol}: AI predicted {_ep_direction}, "
                            f"overriding action {decision.action} → {_ep_action}"
                        )
                    capped_qty = min(decision.quantity or _ep_max_qty, _ep_max_qty)
                    # Apply both overrides in one model_copy call
                    decision = decision.model_copy(update={"action": _ep_action, "quantity": capped_qty})
                    # Stage for registration — added to _earnings_day_positions only after order submits
                    _earnings_play_pending.add(decision.symbol)
                    logger.info(
                        f"EARNINGS PLAY: {decision.action.upper()} {decision.symbol} x{capped_qty} "
                        f"— AI: {_ep_direction} [{_ep_confidence}]: {_ep_reasoning} | forced EOD exit"
                    )
                    log_bot_activity("approved",
                                     f"EARNINGS PLAY: {decision.action.upper()} {decision.symbol} x{capped_qty} "
                                     f"[{_ep_direction.upper()} {_ep_confidence}] {_ep_reasoning} — forced EOD exit",
                                     symbol=decision.symbol, cycle_id=_current_cycle_id)
                    # Fall through to normal order execution below

            # ── FDA binary event gate: same risk handling as earnings ──────────
            # Detects FDA approval/rejection news for biotech/pharma stocks.
            # Predicts direction — uncertain blocks outright, directional gets
            # a small (5% portfolio) position with forced EOD exit.
            if decision.action in ("buy", "short") and decision.symbol not in _earnings_play_pending:
                try:
                    from services.sector_momentum import get_sector_for_symbol as _get_sector
                    _sym_sector = _get_sector(decision.symbol)
                except Exception:
                    _sym_sector = ""
                _fda = check_fda_binary_event(decision.symbol, news_headlines, sector=_sym_sector)
                if _fda.get("has_fda_event"):
                    try:
                        from services.claude_service import predict_earnings_direction as _pred_fda
                        _fda_snap = snapshot_light.get(decision.symbol, {})
                        _fda_ep = _pred_fda(
                            symbol=decision.symbol,
                            snapshot_data=_fda_snap,
                            sentiment=sentiment,
                            news_headlines=news_headlines,
                        )
                    except Exception as _fda_err:
                        logger.warning(f"FDA prediction error for {decision.symbol}: {_fda_err}")
                        _fda_ep = {"direction": "uncertain", "confidence": "low", "reasoning": "error"}

                    _fda_dir  = _fda_ep.get("direction", "uncertain")
                    _fda_conf = _fda_ep.get("confidence", "low")
                    _fda_rsn  = _fda_ep.get("reasoning", "")

                    if _fda_dir == "uncertain":
                        logger.warning(
                            f"FDA BLOCK: {decision.symbol} — prediction uncertain, binary gap risk. Blocking."
                        )
                        log_bot_activity("earnings_block",
                                         f"{decision.action.upper()} {decision.symbol} blocked — "
                                         f"FDA binary event today/tomorrow, AI prediction: uncertain. Blocking.",
                                         symbol=decision.symbol, cycle_id=_current_cycle_id)
                        continue
                    else:
                        _fda_price = snapshot_light.get(decision.symbol, {}).get("current_price")
                        if not _fda_price or _fda_price <= 0:
                            logger.warning(f"FDA play {decision.symbol}: no valid price, blocking")
                            log_bot_activity("earnings_block",
                                             f"{decision.symbol} FDA play skipped — price unavailable",
                                             symbol=decision.symbol, cycle_id=_current_cycle_id)
                            continue
                        _fda_max_qty = max(1, int(float(account.portfolio_value) * 0.05 / _fda_price))
                        _fda_action  = "buy" if _fda_dir == "bullish" else "short"
                        if _fda_action != decision.action:
                            logger.info(
                                f"FDA play {decision.symbol}: AI predicted {_fda_dir}, "
                                f"overriding {decision.action} → {_fda_action}"
                            )
                        capped_fda_qty = min(decision.quantity or _fda_max_qty, _fda_max_qty)
                        decision = decision.model_copy(update={"action": _fda_action, "quantity": capped_fda_qty})
                        _earnings_play_pending.add(decision.symbol)  # reuse EOD exit logic
                        logger.info(
                            f"FDA PLAY: {decision.action.upper()} {decision.symbol} x{capped_fda_qty} "
                            f"— AI: {_fda_dir} [{_fda_conf}]: {_fda_rsn} | forced EOD exit"
                        )
                        log_bot_activity("approved",
                                         f"FDA PLAY: {decision.action.upper()} {decision.symbol} x{capped_fda_qty} "
                                         f"[{_fda_dir.upper()} {_fda_conf}] {_fda_rsn} — forced EOD exit",
                                         symbol=decision.symbol, cycle_id=_current_cycle_id)
                        # Fall through to normal order execution below

            # ── Entry confirmation (strategy-aware) ──
            deep = await loop.run_in_executor(
                None, functools.partial(alpaca_service.get_market_snapshot, [decision.symbol])
            )
            sym_data = deep.get(decision.symbol) or snapshot_light.get(decision.symbol, {})
            closing_prices = sym_data.get("closing_prices", [])
            current_price = sym_data.get("current_price") or 0

            _rel_vol = sym_data.get("relative_volume", 1.0)
            _macd_hist = None
            try:
                from services.indicators import compute_macd
                _macd_data = compute_macd(closing_prices) if closing_prices else {}
                _macd_hist = _macd_data.get("histogram")
            except Exception:
                pass
            _entry_rsi = compute_rsi(closing_prices) if closing_prices else None
            _entry_macd_hist_pct = (
                (_macd_hist / current_price * 100)
                if (_macd_hist is not None and current_price and current_price > 0)
                else None
            )
            _entry_score = _compute_entry_score(
                action=decision.action,
                confidence=_conf_val,
                relative_volume=_rel_vol,
                rsi=_entry_rsi,
                macd_hist_pct=_entry_macd_hist_pct,
                market_tier=market_tier,
            )
            _cooldown_block = _recent_exit_block(decision.symbol, decision.action)
            if _cooldown_block:
                logger.info(f"Churn guard: {_cooldown_block}")
                log_bot_activity("entry_rejected", _cooldown_block,
                                 symbol=decision.symbol, cycle_id=_current_cycle_id)
                continue
            _quality_block = _quality_gate_reason(
                symbol=decision.symbol,
                action=decision.action,
                entry_score=_entry_score,
                market_tier=market_tier,
                strategy_key=strategy_key,
                high_conviction=decision.high_conviction,
            )
            if _quality_block:
                logger.info(f"Quality gate: {_quality_block}")
                log_bot_activity("entry_rejected", _quality_block,
                                 symbol=decision.symbol, cycle_id=_current_cycle_id)
                continue
            _news_block = _news_confirmation_block(
                symbol=decision.symbol,
                action=decision.action,
                relative_volume=_rel_vol,
                macd_hist_pct=_entry_macd_hist_pct,
                high_conviction=decision.high_conviction,
            )
            if _news_block:
                logger.info(f"News confirmation gate: {_news_block}")
                log_bot_activity("entry_rejected", _news_block,
                                 symbol=decision.symbol, cycle_id=_current_cycle_id)
                continue
            _predictive_reject = _predictive_block(
                symbol=decision.symbol,
                action=decision.action,
                predictive_expectancy_pct=decision.predictive_expectancy_pct,
                predictive_trades=decision.predictive_trades,
                high_conviction=decision.high_conviction,
            )
            if _predictive_reject:
                logger.info(f"Predictive gate: {_predictive_reject}")
                log_bot_activity("entry_rejected", _predictive_reject,
                                 symbol=decision.symbol, cycle_id=_current_cycle_id)
                continue

            confirmed, confirm_reason = should_confirm_entry(
                symbol=decision.symbol,
                action=decision.action,
                closing_prices=closing_prices,
                current_price=current_price,
                strategy_key=strategy_key,
                market_tier=market_tier,
                positions_count=len(positions),
                relative_volume=_rel_vol,
                macd_histogram=_macd_hist,
            )

            if not confirmed:
                logger.info(f"Entry rejected: {confirm_reason}")
                log_bot_activity("entry_rejected", confirm_reason,
                                 symbol=decision.symbol, cycle_id=_current_cycle_id)
                await manager.broadcast({"type": "ai_analysis", "data": {
                    "reasoning": f"Entry not confirmed: {confirm_reason}",
                    "last_action": "waiting", "symbol": decision.symbol,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }})
                continue  # Try next decision, don't abort the whole cycle

            # ── Cycle trade cap: limit confirmed new opens per cycle ─────────
            # Entry rejections should not burn a slot; confirmed attempts still
            # consume one even if Alpaca later rejects the order.
            if decision.action in ("buy", "short"):
                if _cycle_open_count >= _max_trades_this_cycle:
                    logger.info(
                        f"Cycle cap ({_max_trades_this_cycle}) reached — skipping "
                        f"{decision.action.upper()} {decision.symbol}"
                    )
                    log_bot_activity(
                        "trade_cap",
                        f"Max {_max_trades_this_cycle} confirmed new opens/cycle reached — skipping "
                        f"{decision.action.upper()} {decision.symbol}",
                        symbol=decision.symbol, cycle_id=_current_cycle_id,
                    )
                    continue
                _cycle_open_count += 1

            if decision.action in ("buy", "short") and decision.symbol not in _earnings_play_pending:
                # Volatility-adjusted sizing
                # Skip for earnings plays — their quantity was already capped at 2-5%
                # of portfolio; running vol-adjust here would silently override that cap.
                atr = compute_atr(
                    highs=sym_data.get("high_prices", []),
                    lows=sym_data.get("low_prices", []),
                    closes=closing_prices,
                )
                price = sym_data.get("current_price")
                # Bug fix: initialize vol_qty to decision.quantity so it's always defined.
                # Previously vol_qty was only assigned inside `if price and price > 0`,
                # causing a NameError crash when price data was missing.
                vol_qty = decision.quantity
                if price and price > 0:
                    # Penny stock guard: stocks under $5 get a smaller max position %
                    # to prevent massive share counts from inflating notional risk.
                    # e.g. $3.56 stock × normal 30% of $100k = 8,427 shares → too concentrated.
                    # With max_penny_position_pct=3%, same stock → 843 shares (~$3k) instead.
                    is_penny = price < 5.0
                    if is_penny:
                        penny_pct = _risk_settings.get("max_penny_position_pct", 3.0) / 100.0
                        effective_max_pct = penny_pct
                        logger.info(
                            f"Penny stock guard: {decision.symbol} @ ${price:.2f} < $5 — "
                            f"capping position at {penny_pct*100:.1f}% (was {strat['max_position_pct']*100:.0f}%)"
                        )
                    else:
                        effective_max_pct = strat["max_position_pct"]
                    if decision.action == "short":
                        # Shorts can squeeze hard; keep the aggressive cap, but risk less per trade
                        # than longs unless the strategy explicitly sets a lower value.
                        effective_max_pct = min(effective_max_pct, strat["max_position_pct"] * 0.50)

                    regime_mult = _regime_risk_multiplier(macro, geo)
                    base_risk_pct = strat.get("risk_per_trade_pct", 0.01) * (
                        0.50 if decision.action == "short" else 1.0
                    )
                    if regime_mult < 1.0:
                        effective_max_pct *= regime_mult
                        base_risk_pct *= regime_mult
                        logger.info(
                            f"Regime throttle: {decision.symbol} {decision.action} sized at "
                            f"{regime_mult:.2f}x due to regime={macro.get('market_regime')} "
                            f"vix={macro.get('vix_level')} geo={geo.get('risk_level')}"
                        )

                    vol_qty = volatility_adjusted_quantity(
                        portfolio_value=account.portfolio_value,
                        max_position_pct=effective_max_pct,
                        current_price=price,
                        atr=atr,
                        risk_per_trade_pct=base_risk_pct,
                    )
                    if vol_qty != decision.quantity:
                        logger.info(f"Vol-adjust: {decision.symbol} {decision.quantity}→{vol_qty} shares (ATR={atr:.2f})")

                    # ── Conviction-based sizing multiplier ────────────────────
                    # Claude embeds [HIGH]/[MEDIUM]/[LOW] in reasoning.
                    # High-conviction trades deserve a bigger position — up to 1.25×.
                    # Low-conviction trades get 0.8× to limit exposure on uncertain setups.
                    # Bug fix: guard against None reasoning (AI occasionally omits it)
                    _reasoning_upper = (decision.reasoning or "").upper()
                    if "[HIGH]" in _reasoning_upper:
                        _conv_mult = 1.25
                    elif "[LOW]" in _reasoning_upper:
                        _conv_mult = 0.80
                    else:
                        _conv_mult = 1.0
                    if _conv_mult != 1.0:
                        _pre_conv = vol_qty
                        vol_qty = max(1, int(vol_qty * _conv_mult))
                        logger.info(
                            f"Conviction sizing: {decision.symbol} "
                            f"{'HIGH' if _conv_mult > 1 else 'LOW'} confidence → "
                            f"{_pre_conv}→{vol_qty} shares ({_conv_mult:.2f}×)"
                        )
                    if decision.high_conviction and decision.action == "buy":
                        _pre_hc = vol_qty
                        vol_qty = max(1, int(vol_qty * 1.20))
                        logger.info(
                            f"Rocket setup sizing: {decision.symbol} {_pre_hc}→{vol_qty} shares "
                            f"(high conviction breakout/catalyst)"
                        )
                    if (
                        decision.action in ("buy", "short")
                        and decision.predictive_expectancy_pct is not None
                        and decision.predictive_trades >= 3
                        and decision.predictive_expectancy_pct > 0.35
                    ):
                        _pre_pred = vol_qty
                        pred_mult = 1.10 if decision.predictive_expectancy_pct < 0.80 else 1.15
                        vol_qty = max(1, int(vol_qty * pred_mult))
                        logger.info(
                            f"Predictive sizing: {decision.symbol} {_pre_pred}→{vol_qty} shares "
                            f"(prior {decision.predictive_expectancy_pct:+.2f}% over {decision.predictive_trades} trades)"
                        )

                    # ── Sector momentum tilt ──────────────────────────────────
                    # Hot sectors (score > +2%) get 20% more shares.
                    # Cold sectors (score < -1%) get 20% fewer shares.
                    # Uses sector_scores computed at cycle start (already in scope).
                    try:
                        _sym_sector = (sector_context or {}).get(decision.symbol, {}).get("sector")
                        _sec_score = (sector_scores or {}).get(_sym_sector, 0.0) if _sym_sector else 0.0
                        if decision.action == "buy" and _sec_score > 2.0:
                            _pre_sec = vol_qty
                            vol_qty = max(1, int(vol_qty * 1.20))
                            logger.info(
                                f"Sector tilt ({_sym_sector} score={_sec_score:+.1f}%): "
                                f"{decision.symbol} {_pre_sec}→{vol_qty} shares (+20%)"
                            )
                        elif decision.action == "buy" and _sec_score < -1.0:
                            _pre_sec = vol_qty
                            vol_qty = max(1, int(vol_qty * 0.80))
                            logger.info(
                                f"Sector tilt ({_sym_sector} score={_sec_score:+.1f}%): "
                                f"{decision.symbol} {_pre_sec}→{vol_qty} shares (-20%)"
                            )
                        elif decision.action == "short" and _sec_score < -1.0:
                            _pre_sec = vol_qty
                            vol_qty = max(1, int(vol_qty * 1.15))
                            logger.info(
                                f"Short sector tilt ({_sym_sector} score={_sec_score:+.1f}%): "
                                f"{decision.symbol} {_pre_sec}→{vol_qty} shares (+15%)"
                            )
                        elif decision.action == "short" and _sec_score > 2.0:
                            _pre_sec = vol_qty
                            vol_qty = max(1, int(vol_qty * 0.70))
                            logger.info(
                                f"Short sector tilt ({_sym_sector} score={_sec_score:+.1f}%): "
                                f"{decision.symbol} {_pre_sec}→{vol_qty} shares (-30%)"
                            )
                    except Exception:
                        pass

                    decision.quantity = vol_qty

                if decision.action == "buy":
                    # Scale-in (strategy-aware — aggressive takes full position)
                    existing_pos = next((p for p in positions if p.symbol == decision.symbol), None)
                    existing_qty = float(existing_pos.qty) if existing_pos else 0
                    scaled_qty = get_scale_in_quantity(
                        base_quantity=decision.quantity,
                        confidence="high",
                        existing_position_qty=existing_qty,
                        max_total_qty=decision.quantity,
                        strategy_key=strategy_key,
                    )
                    if scaled_qty != decision.quantity:
                        logger.info(f"Scale-in: {decision.symbol} buying {scaled_qty}/{decision.quantity} planned")
                    decision.quantity = scaled_qty

            # ── Macro event position size reduction ───────────────────────────
            # FOMC day (0.5×) or CPI/Jobs day (0.7×) — shrink all new opens.
            # Earnings/FDA plays already capped at 5% portfolio max — skip those.
            if decision.action in ("buy", "short") and _macro_size_mult != 1.0 and decision.symbol not in _earnings_play_pending:
                _pre_macro = decision.quantity
                decision = decision.model_copy(update={"quantity": max(1, int(decision.quantity * _macro_size_mult))})
                if decision.quantity != _pre_macro:
                    logger.info(
                        f"Macro sizing ({_macro_event['event'].upper()}): {decision.symbol} "
                        f"{_pre_macro}→{decision.quantity} shares ({_macro_size_mult:.2f}×)"
                    )

            # ── Per-stock concentration cap ───────────────────────────────────
            # Prevents accumulating too much of any single stock across repeated cycles.
            # Each cycle buy passes individual sizing checks but without a total cap,
            # 11 buys of QUCY at 3%/buy = 33% portfolio in one name.
            # Hard cap: 10% for regular stocks, same as max_penny_position_pct for stocks <$5.
            # Reduces qty to fit if partially over; blocks entirely if already at/over cap.
            if decision.action in ("buy", "short"):
                _conc_existing = next((p for p in positions if p.symbol == decision.symbol), None)
                # abs() because Alpaca returns negative market_value for short positions
                _conc_existing_val = abs(float(_conc_existing.market_value)) if _conc_existing else 0.0
                _conc_port_val = float(account.portfolio_value)
                _conc_price = current_price or 1
                _conc_new_cost = decision.quantity * _conc_price
                # Use penny cap for <$5 stocks, otherwise respect strategy's max_position_pct.
                # Using strategy's own limit means: once a full-size position is held,
                # further buys of the same stock are blocked. Pyramid adds (which go through
                # a separate code path above) are unaffected.
                _is_penny_conc = _conc_price < 5.0
                if _is_penny_conc:
                    _conc_cap_pct = _risk_settings.get("max_penny_position_pct", 3.0) / 100.0
                else:
                    _conc_cap_pct = strat["max_position_pct"]  # e.g. 0.30 aggressive, 0.10 balanced
                _conc_cap_dollars = _conc_port_val * _conc_cap_pct

                if _conc_existing_val >= _conc_cap_dollars * 0.95:
                    # Already at or above the cap — block entirely
                    _conc_msg = (
                        f"{decision.action.upper()} {decision.symbol} blocked — "
                        f"concentration cap reached: ${_conc_existing_val:,.0f} held "
                        f"({_conc_existing_val / _conc_port_val * 100:.1f}%) ≥ "
                        f"{_conc_cap_pct * 100:.0f}% cap (${_conc_cap_dollars:,.0f})"
                    )
                    logger.info(f"Concentration cap: {_conc_msg}")
                    log_bot_activity("entry_rejected", _conc_msg,
                                     symbol=decision.symbol, cycle_id=_current_cycle_id)
                    continue
                elif _conc_existing_val + _conc_new_cost > _conc_cap_dollars:
                    # Partially over — reduce qty to stay within cap
                    _allowed = max(0.0, _conc_cap_dollars - _conc_existing_val)
                    _capped_qty = max(1, int(_allowed / _conc_price))
                    logger.info(
                        f"Concentration cap: {decision.symbol} reducing "
                        f"{decision.quantity}→{_capped_qty} shares "
                        f"(existing ${_conc_existing_val:,.0f} + new fits "
                        f"within {_conc_cap_pct * 100:.0f}% cap ${_conc_cap_dollars:,.0f})"
                    )
                    decision = decision.model_copy(update={"quantity": _capped_qty})

            # ── Final affordability check against tradeable cash (PDT-aware) ──
            # Vol-adjust, conviction, and sector sizing all use portfolio_value which
            # can far exceed day trading buying power on margin accounts. This final
            # check ensures the order actually fits within available cash after all
            # sizing multipliers have been applied.
            if decision.action == "buy" and current_price and current_price > 0:
                _max_affordable = int(_tradeable_cash / current_price)
                if decision.quantity > _max_affordable:
                    if _max_affordable < 1:
                        logger.info(
                            f"Skipping {decision.symbol} — insufficient buying power "
                            f"(need ${current_price:.2f}/share, have ${_tradeable_cash:.2f})"
                        )
                        continue
                    logger.info(
                        f"Affordability cap: {decision.symbol} {decision.quantity}→{_max_affordable} shares "
                        f"(buying power ${_tradeable_cash:.2f} / price ${current_price:.2f})"
                    )
                    decision = decision.model_copy(update={"quantity": _max_affordable})

            # ── Pre-sell: cancel any open orders on this symbol ──
            # Rotation sells (and scale-outs) fail with "insufficient qty available"
            # when a GTC bracket/limit sell order from the original entry is still open,
            # holding shares in reserve. Cancel those first so Alpaca frees the qty.
            if decision.action in ("sell",):
                try:
                    open_orders = alpaca_service.get_orders(limit=20)
                    for o in open_orders:
                        if o.symbol == decision.symbol and o.status in ("new", "partially_filled", "accepted"):
                            alpaca_service.cancel_order(o.id)
                            logger.info(f"Pre-sell cancel: {o.id} ({o.side}) on {decision.symbol} to free qty for rotation")
                except Exception as ce:
                    logger.warning(f"Pre-sell cancel failed for {decision.symbol} (non-fatal): {ce}")

            # Route to correct order executor
            if decision.action == "short":
                order = alpaca_service.submit_short_order(
                    symbol=decision.symbol,
                    qty=decision.quantity,
                    stop_loss_pct=decision.stop_loss_pct or _risk_settings["stop_loss_pct"],
                    take_profit_pct=decision.take_profit_pct or _risk_settings["take_profit_pct"],
                    high_conviction=decision.high_conviction,
                )
            else:
                order = alpaca_service.submit_market_order(
                    symbol=decision.symbol,
                    qty=decision.quantity,
                    side=decision.action,
                    stop_loss_pct=decision.stop_loss_pct or _risk_settings["stop_loss_pct"],
                    take_profit_pct=decision.take_profit_pct or _risk_settings["take_profit_pct"],
                    partial_exit=decision.partial_exit,
                    high_conviction=decision.high_conviction,
                )
            if order:
                # Track daily trade count (_cycle_open_count already incremented above)
                today = datetime.now(timezone.utc).date()
                _daily_trade_count[today] = _daily_trade_count.get(today, 0) + 1
                logger.info(
                    f"✅ Order executed: {decision.action.upper()} {decision.symbol} x{decision.quantity} "
                    f"| trades today: {_daily_trade_count[today]} | cycle opens: {_cycle_open_count}/{_max_trades_this_cycle}"
                )

                # Log position open / close to position_log.
                # For limit orders, only log and seed watermarks if the order actually filled.
                # An unfilled limit (filled_avg_price is None, status != "filled") must NOT be
                # logged as an open position — doing so pollutes performance metrics with phantom
                # trades and causes the cycle-detect logic to fire a false close next cycle.
                order_filled = order.filled_avg_price is not None or order.status == "filled"
                fill_price = float(order.filled_avg_price or sym_data.get("current_price") or 0)
                if not order_filled:
                    # Bracket/limit order submitted but not yet filled.
                    # We still seed _previous_positions and watermarks with the estimated
                    # entry price (current market price) so that:
                    # (a) trailing stop logic works from the next cycle, and
                    # (b) close detection has entry data when the position eventually closes.
                    # log_position_open is deferred — the new-position detection block at cycle
                    # start will call it with the actual Alpaca fill price once confirmed.
                    logger.info(
                        f"Order submitted but not yet filled ({order.status}) — "
                        f"seeding watermarks with estimated price ${fill_price:.2f} for {decision.symbol}. "
                        f"position_log entry deferred until fill confirmed."
                    )
                    if decision.action == "buy" and fill_price > 0:
                        _position_high_watermarks[decision.symbol] = fill_price
                        _save_watermarks()
                        _previous_positions[decision.symbol] = {
                            "qty": decision.quantity,
                            "avg_entry_price": fill_price,
                            "entry_time": datetime.now(timezone.utc),
                            "exit_reason": "unknown",
                            "side": "long",
                            "strategy": strategy_key,
                            "claude_reasoning": decision.reasoning,
                            "market_regime": macro.get("market_regime"),
                            "entry_rsi": _entry_rsi,
                            "entry_macd_hist_pct": _entry_macd_hist_pct,
                            "entry_score": _entry_score,
                            "partial_exit": decision.partial_exit,
                            "take_profit_pct": decision.take_profit_pct or _risk_settings["take_profit_pct"],
                            "stop_loss_pct": decision.stop_loss_pct or _risk_settings["stop_loss_pct"],
                            "entry_logged": False,
                            "exit_legs_attached": False,
                        }
                    elif decision.action == "short" and fill_price > 0:
                        _short_low_watermarks[decision.symbol] = fill_price
                        _save_watermarks()
                        _previous_positions[decision.symbol] = {
                            "qty": decision.quantity,
                            "avg_entry_price": fill_price,
                            "entry_time": datetime.now(timezone.utc),
                            "exit_reason": "unknown",
                            "side": "short",
                            "strategy": f"{strategy_key}_short",
                            "claude_reasoning": decision.reasoning,
                            "market_regime": macro.get("market_regime"),
                            "entry_rsi": _entry_rsi,
                            "entry_macd_hist_pct": _entry_macd_hist_pct,
                            "entry_score": _entry_score,
                            "partial_exit": False,
                            "take_profit_pct": decision.take_profit_pct or _risk_settings["take_profit_pct"],
                            "stop_loss_pct": decision.stop_loss_pct or _risk_settings["stop_loss_pct"],
                            "entry_logged": False,
                            "exit_legs_attached": False,
                        }
                elif decision.action == "short" and fill_price > 0:
                    log_position_open(
                        symbol=decision.symbol,
                        entry_price=fill_price,
                        quantity=decision.quantity,
                        strategy=f"{strategy_key}_short",
                        claude_reasoning=decision.reasoning,
                        market_regime=macro.get("market_regime"),
                        side="short",
                        entry_rsi=_entry_rsi,
                        entry_macd_hist_pct=_entry_macd_hist_pct,
                        entry_score=_entry_score,
                    )
                    # Seed low watermark for new short position and immediately persist
                    _short_low_watermarks[decision.symbol] = fill_price
                    _save_watermarks()
                    _previous_positions[decision.symbol] = {
                        "qty": decision.quantity,
                        "avg_entry_price": fill_price,
                        "entry_time": datetime.now(timezone.utc),
                        "exit_reason": "unknown",
                        "side": "short",
                        "strategy": f"{strategy_key}_short",
                        "claude_reasoning": decision.reasoning,
                        "market_regime": macro.get("market_regime"),
                        "entry_rsi": _entry_rsi,
                        "entry_macd_hist_pct": _entry_macd_hist_pct,
                        "entry_score": _entry_score,
                        "partial_exit": False,
                        "take_profit_pct": decision.take_profit_pct or _risk_settings["take_profit_pct"],
                        "stop_loss_pct": decision.stop_loss_pct or _risk_settings["stop_loss_pct"],
                        "entry_logged": True,
                        "exit_legs_attached": False,
                    }
                elif decision.action == "buy" and fill_price > 0:
                    log_position_open(
                        symbol=decision.symbol,
                        entry_price=fill_price,
                        quantity=decision.quantity,
                        strategy=strategy_key,
                        claude_reasoning=decision.reasoning,
                        market_regime=macro.get("market_regime"),
                        side="long",
                        entry_rsi=_entry_rsi,
                        entry_macd_hist_pct=_entry_macd_hist_pct,
                        entry_score=_entry_score,
                    )
                    # Seed watermark for new position
                    _position_high_watermarks[decision.symbol] = fill_price
                    _save_watermarks()
                    # Tag in previous_positions so close detection knows entry price
                    _previous_positions[decision.symbol] = {
                        "qty": decision.quantity,
                        "avg_entry_price": fill_price,
                        "entry_time": datetime.now(timezone.utc),
                        "exit_reason": "unknown",
                        "side": "long",
                        "strategy": strategy_key,
                        "claude_reasoning": decision.reasoning,
                        "market_regime": macro.get("market_regime"),
                        "entry_rsi": _entry_rsi,
                        "entry_macd_hist_pct": _entry_macd_hist_pct,
                        "entry_score": _entry_score,
                        "partial_exit": decision.partial_exit,
                        "take_profit_pct": decision.take_profit_pct or _risk_settings["take_profit_pct"],
                        "stop_loss_pct": decision.stop_loss_pct or _risk_settings["stop_loss_pct"],
                        "entry_logged": True,
                        "exit_legs_attached": False,
                    }
                elif decision.action == "sell" and fill_price:
                    prev = _previous_positions.get(decision.symbol, {})
                    if decision.symbol in _previous_positions:
                        _previous_positions[decision.symbol]["exit_reason"] = "ai_sell"
                    log_position_close(
                        symbol=decision.symbol,
                        exit_price=fill_price,
                        exit_reason="ai_sell",
                        entry_price=prev.get("avg_entry_price"),
                        quantity=prev.get("qty"),
                        entry_time=prev.get("entry_time"),
                        side=prev.get("side", "long"),
                        strategy=prev.get("strategy"),
                        claude_reasoning=prev.get("claude_reasoning"),
                        market_regime=prev.get("market_regime"),
                        entry_rsi=prev.get("entry_rsi"),
                        entry_macd_hist_pct=prev.get("entry_macd_hist_pct"),
                        entry_score=prev.get("entry_score"),
                    )
                    _record_recent_exit(decision.symbol, "ai_sell", prev.get("last_pl_pct"))
                    # ── Profit reserve on AI-initiated sells ──
                    _ai_sold_symbols.add(decision.symbol)  # guard cycle-detect from double-counting
                    try:
                        reserve_pct = float(_risk_settings.get("profit_reserve_pct", 0.0)) / 100.0
                        entry_p  = prev.get("avg_entry_price") or 0
                        qty_p    = prev.get("qty") or 0
                        p_side   = prev.get("side", "long")
                        if reserve_pct > 0 and entry_p > 0 and qty_p > 0:
                            realized = (fill_price - entry_p) * qty_p if p_side == "long" else (entry_p - fill_price) * qty_p
                            if realized > 0:
                                add_to_reserve(round(realized * reserve_pct, 2))
                    except Exception as _re:
                        logger.warning(f"Profit reserve (ai_sell) failed (non-fatal): {_re}")

                # Confirm earnings play registration — order submitted, safe to track for EOD exit
                if decision.action in ("buy", "short") and decision.symbol in _earnings_play_pending:
                    _earnings_day_positions.add(decision.symbol)
                    _earnings_play_pending.discard(decision.symbol)
                    logger.info(f"Registered {decision.symbol} in _earnings_day_positions — forced EOD exit at 3:45 PM ET")

                # "approved" = confirmed fill; "order_placed" = submitted but pending fill
                # (bracket orders return status "new"/"accepted" at submission time)
                _log_event_type = "approved" if order_filled else "order_placed"
                _status_note = f"filled @ ${fill_price:.2f}" if order_filled else f"pending fill (status={order.status})"
                log_bot_activity(
                    _log_event_type,
                    f"{decision.action.upper()} {decision.symbol} x{decision.quantity} @ ${fill_price:.2f} — "
                    f"[{_status_note}] {decision.reasoning[:100]}",
                    symbol=decision.symbol, cycle_id=_current_cycle_id,
                )

                await manager.broadcast({"type": "order_filled", "data": order.model_dump(mode="json")})
                updated_positions = await loop.run_in_executor(None, alpaca_service.get_positions)
                await manager.broadcast({"type": "position_update", "data": [p.model_dump(mode="json") for p in updated_positions]})

    except Exception as e:
        logger.error(f"Trading cycle error: {e}", exc_info=True)


async def _save_eod_snapshot():
    """
    Save an end-of-day performance snapshot to daily_summary.
    Called once automatically when the market transitions from open → closed.
    """
    try:
        from services.db import save_daily_summary, get_trade_metrics_report, set_setting
        from services.strategy import get_strategy
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.historical import StockHistoricalDataClient
        from config import settings as _settings

        account = alpaca_service.get_account()
        today = datetime.now(timezone.utc).date()

        # Count today's AI decisions from trade_log
        from services.db import _get_conn
        conn = _get_conn()
        totals = {"total": 0, "buy": 0, "sell": 0, "hold": 0}
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT action, COUNT(*) FROM trade_log
                        WHERE timestamp::date = %s
                        GROUP BY action
                    """, (today,))
                    for action, cnt in cur.fetchall():
                        totals["total"] += cnt
                        if action in totals:
                            totals[action] += cnt
            except Exception as e:
                logger.warning(f"EOD: could not count today's decisions: {e}")

        # Get SPY close price
        spy_close = None
        try:
            data_client = StockHistoricalDataClient(_settings.alpaca_api_key, _settings.alpaca_secret_key)
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=2)
            bars = data_client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=["SPY"], timeframe=TimeFrame.Day, start=start, end=end
            ))
            spy_bars = bars.get("SPY", [])
            if spy_bars:
                spy_close = float(spy_bars[-1].close)
        except Exception as e:
            logger.warning(f"EOD: could not fetch SPY close: {e}")

        strat = get_strategy()
        save_daily_summary({
            "date":             today,
            "portfolio_value":  account.portfolio_value,
            "cash":             account.cash,
            "day_pl":           account.day_pl,
            "day_pl_pct":       account.day_pl_percent,
            "total_decisions":  totals["total"],
            "buy_decisions":    totals["buy"],
            "sell_decisions":   totals["sell"],
            "hold_decisions":   totals["hold"],
            "strategy":         strat["key"],
            "spy_close":        spy_close,
        })
        try:
            report = get_trade_metrics_report(days=30)
            report["generated_for_date"] = today.isoformat()
            set_setting("latest_daily_trade_report", report)
        except Exception as report_exc:
            logger.warning(f"EOD report snapshot failed (non-fatal): {report_exc}")
        logger.info(f"EOD snapshot saved for {today}: portfolio=${account.portfolio_value:,.2f}, day_pl={account.day_pl_percent:.2f}%")
    except Exception as e:
        logger.warning(f"EOD snapshot failed (non-fatal): {e}")


async def _trading_loop():
    global _next_run_at
    from datetime import timedelta
    _cleanup_counter = 0
    _premarket_scanned_date = None  # track which date we last ran the pre-market scan
    # Restore _eod_saved_date from cache so multiple deploys on the same day
    # don't re-run the EOD report (cache_get returns None if key missing/expired)
    _eod_saved_date_str = cache_get("eod_saved_date")
    try:
        from datetime import date as _date
        _eod_saved_date = _date.fromisoformat(_eod_saved_date_str) if _eod_saved_date_str else None
    except Exception:
        _eod_saved_date = None
    _market_was_open = False        # detect open→closed transition
    while _is_running:
        # ── EOD catchup: handles bot restarts after market close ──────────────
        # Normal flow relies on open→closed transition which is missed if the
        # bot (re)starts after 4 PM ET. Check once per day and self-guard with
        # _eod_saved_date so it never runs twice on the same calendar day.
        now_utc = datetime.now(timezone.utc)
        today = now_utc.date()
        if _eod_saved_date != today and not alpaca_service.is_market_open():
            try:
                from zoneinfo import ZoneInfo as _ZI_eod
                _now_et_eod = datetime.now(_ZI_eod("America/New_York"))
            except Exception:
                _now_et_eod = datetime.now(timezone.utc) - timedelta(hours=4)
            if _now_et_eod.hour >= 16:   # 4 PM ET — market has definitely closed
                logger.info("Post-market restart detected — running EOD catchup")
                _eod_saved_date = today   # guard first so a crash can't double-fire
                cache_set("eod_saved_date", today.isoformat(), 86400)  # persist across restarts
                await _save_eod_snapshot()
                try:
                    from services.eod_analysis_service import run_eod_analysis as _run_eod_catchup
                    _catchup_future = asyncio.get_running_loop().run_in_executor(None, _run_eod_catchup)
                    _catchup_future.add_done_callback(
                        lambda f: logger.warning(f"EOD catchup failed: {f.exception()}") if f.exception() else None
                    )
                    logger.info("EOD catchup analysis triggered.")
                except Exception as _ce:
                    logger.warning(f"EOD catchup trigger failed: {_ce}")
        # Run pre-market scan once per day at 13:00-14:00 UTC (9-10 AM EST)
        now_utc = datetime.now(timezone.utc)
        today = now_utc.date()
        if 13 <= now_utc.hour < 14 and _premarket_scanned_date != today:
            _premarket_scanned_date = today
            await run_premarket_scan()

        try:
            await run_trading_cycle()
        except asyncio.CancelledError:
            raise  # let stop() work correctly
        except Exception as _loop_err:
            logger.error(f"Unhandled error in trading loop — cycle skipped, loop continues: {_loop_err}", exc_info=True)

        # Detect market close → save EOD snapshot once per day
        market_open_now = alpaca_service.is_market_open()
        if _market_was_open and not market_open_now and _eod_saved_date != today:
            _eod_saved_date = today
            cache_set("eod_saved_date", today.isoformat(), 86400)  # persist across restarts
            logger.info("Market just closed — saving EOD snapshot and running AI analysis.")
            await _save_eod_snapshot()
            # Run Claude-powered EOD analysis in a background thread so it doesn't block the loop.
            # run_eod_analysis() is synchronous (calls ask_ai which is blocking I/O),
            # so we offload it to the executor. get_running_loop() is correct here
            # (we're inside an async function) and avoids the DeprecationWarning from get_event_loop().
            try:
                from services.eod_analysis_service import run_eod_analysis as _run_eod_analysis
                _eod_future = asyncio.get_running_loop().run_in_executor(None, _run_eod_analysis)
                _eod_future.add_done_callback(
                    lambda f: logger.warning(f"EOD analysis failed: {f.exception()}") if f.exception() else None
                )
                logger.info("EOD analysis triggered in background thread.")
            except Exception as _eod_err:
                logger.warning(f"Could not trigger EOD analysis: {_eod_err}")
        _market_was_open = market_open_now

        # Run DB cleanup once every ~144 cycles (~24 hours at 10-min intervals)
        _cleanup_counter += 1
        if _cleanup_counter >= 144:
            _cleanup_counter = 0
            from services.db import cleanup_old_trade_logs, cleanup_expired_cache, cleanup_old_bot_activity
            cleanup_old_trade_logs(days=90)
            cleanup_expired_cache()
            cleanup_old_bot_activity(days=30)
            logger.info("DB cleanup complete.")

        # Sleep longer when market is closed (nights / weekends)
        # so we don't spin every 5 min for 18 hours doing nothing
        if market_open_now:
            sleep_seconds = int(_risk_settings.get("cycle_interval_seconds", TRADING_INTERVAL_SECONDS))
        else:
            sleep_seconds = 900  # check every 15 min when closed
            logger.info("Market closed — checking again in 15 minutes")

        _next_run_at = datetime.now(timezone.utc) + timedelta(seconds=sleep_seconds)
        if _wake_event is None:
            await asyncio.sleep(sleep_seconds)
        else:
            # Clear stale wake flags before sleeping. Urgent context is stored
            # separately, so this avoids closed-market spin while preserving the
            # news for the next open cycle.
            if _wake_event.is_set():
                _wake_event.clear()
            try:
                await asyncio.wait_for(_wake_event.wait(), timeout=sleep_seconds)
                logger.info("Trading loop woke early for high-impact news.")
            except asyncio.TimeoutError:
                pass


def start():
    global _is_running, _task, _wake_event
    if _is_running:
        return
    _is_running = True
    _wake_event = asyncio.Event()
    _task = asyncio.create_task(_trading_loop())
    try:
        from services import news_stream
        news_stream.start()
    except Exception as e:
        logger.warning(f"Could not start news stream: {e}")
    logger.info("Trading engine started.")


def stop():
    global _is_running, _task
    _is_running = False
    if _task:
        _task.cancel()
        _task = None
    try:
        from services import news_stream
        news_stream.stop()
    except Exception as e:
        logger.warning(f"Could not stop news stream: {e}")
    logger.info("Trading engine stopped.")
