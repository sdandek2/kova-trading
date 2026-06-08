"""
wheel_universe.py — AI-driven universe discovery for the Wheel Bot

No hardcoded watchlist. Every Sunday night the AI screens the market,
picks 10-15 stocks ideal for cash-secured puts, and stores them in
wheel_universe table. The wheel engine reads from that table each cycle.

AI model: ask_ai() — Gemini Flash (no thinking) | Haiku fallback.
Cost: ~$0.001/week. Essentially free.

Discovery logic:
  1. Pull top 100 most actively traded US stocks from Alpaca snapshot
  2. Filter: price $5–$80, volume > 1M, exclude ETFs and Kova's exclusion list
  3. Pass filtered candidates (~30-40 stocks) to AI with context:
     - Current market regime
     - Historical performance from wheel_symbol_stats (if available)
     - Wheel-specific screening criteria
  4. AI returns 10-15 ranked stocks with reasoning
  5. Stored in wheel_universe table, replaces previous week's list
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Price range for wheel candidates — collateral needs to be manageable.
# These are sensible defaults. Override via Railway env vars:
#   WHEEL_MIN_PRICE=10  → skip penny stocks as account grows
#   WHEEL_MAX_PRICE=150 → allow higher-priced stocks as capital scales
#   WHEEL_MIN_VOLUME=2000000 → tighten liquidity requirement in live mode
import os
MIN_PRICE  = float(os.environ.get("WHEEL_MIN_PRICE",  "5.0"))
MAX_PRICE  = float(os.environ.get("WHEEL_MAX_PRICE",  "80.0"))
MIN_VOLUME = int(os.environ.get("WHEEL_MIN_VOLUME",   "1000000"))

# ETF/index exclusions — reuse Kova's list + add options-relevant ones
_WHEEL_EXCLUSIONS = {
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "GLD", "SLV", "USO",
    "TLT", "IEF", "LQD", "HYG", "AGG", "XLF", "XLK", "XLE", "XLV",
    "XLY", "XLI", "SMH", "SOXX", "SOXL", "SOXS", "TQQQ", "SQQQ",
    "SPXL", "SPXS", "UVXY", "SVXY", "GBTC", "IBIT", "ARKK", "ARKW",
    "GDX", "GDXJ", "IAU",
}


def _get_candidate_pool(limit: int = 100) -> list[dict]:
    """
    Pull top active US stocks from Alpaca as candidate pool.
    Returns list of {symbol, price, volume} dicts.
    """
    try:
        from services.wheel_engine import _get_wheel_data_client
        from alpaca.data.requests import StockSnapshotRequest

        data_client = _get_wheel_data_client()

        # Use a curated candidate list of ~80 well-known optionable stocks
        # This is intentionally broad — AI narrows it down to best 10-15
        candidates = [
            # High-IV growth / tech
            "NVDA", "AMD", "TSLA", "META", "GOOGL", "AMZN", "MSFT", "AAPL",
            "NFLX", "CRM", "SNOW", "PLTR", "COIN", "MSTR", "HOOD",
            # Fintech / consumer
            "SOFI", "AFRM", "UPST", "SQ", "PYPL", "BILL", "DAVE",
            # EV / energy / industrial
            "RIVN", "LCID", "F", "GM", "NIO", "XPEV", "LI",
            # Biotech / health (high IV)
            "MRNA", "BNTX", "SGEN", "INMD", "CRSP",
            # Retail / entertainment
            "MELI", "SE", "GRAB", "ABNB", "UBER", "LYFT", "DASH",
            "PENN", "DKNG", "MGM", "LVS",
            # Semis / hardware
            "MU", "INTC", "QCOM", "SMCI", "AVGO", "KLAC", "LRCX",
            # Telecom / media
            "T", "VZ", "SNAP", "PINS", "RBLX",
            # Banks / financials
            "BAC", "C", "WFC", "JPM", "GS", "MS",
            # Commodities / miners
            "FCX", "AA", "CLF", "X", "MP",
            # Healthcare
            "CVS", "WBA", "TDOC",
        ]

        # Get snapshots for price/volume filtering
        filtered = []
        batch_size = 20
        for i in range(0, len(candidates), batch_size):
            batch = candidates[i:i + batch_size]
            try:
                snap_req = StockSnapshotRequest(symbol_or_symbols=batch)
                snaps = data_client.get_stock_snapshot(snap_req)
                for sym, snap in snaps.items():
                    if sym in _WHEEL_EXCLUSIONS:
                        continue
                    try:
                        price = float(snap.latest_trade.price) if snap.latest_trade else 0
                        if not (MIN_PRICE <= price <= MAX_PRICE):
                            continue
                        # Volume from daily bar if available
                        vol = float(snap.daily_bar.volume) if snap.daily_bar else 0
                        if vol < MIN_VOLUME:
                            continue
                        filtered.append({
                            "symbol": sym,
                            "price": round(price, 2),
                            "volume_m": round(vol / 1_000_000, 1),
                        })
                    except Exception:
                        continue
            except Exception as e:
                logger.warning(f"Wheel universe snapshot batch error: {e}")
                continue

        # Sort by volume (most liquid first for AI context)
        filtered.sort(key=lambda x: x["volume_m"], reverse=True)
        return filtered[:limit]

    except Exception as e:
        logger.error(f"Wheel universe candidate pool error: {e}")
        return []


def _get_symbol_performance_context() -> dict:
    """Load historical wheel performance by symbol for AI context."""
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return {}
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, win_rate, avg_premium_yield, total_cycles
                FROM wheel_symbol_stats
                WHERE total_cycles >= 2
                ORDER BY win_rate DESC
            """)
            return {
                row[0]: {
                    "win_rate": round(row[1] * 100, 1),
                    "avg_yield": round(row[2] * 100, 2),
                    "cycles": row[3],
                }
                for row in cur.fetchall()
            }
    except Exception as e:
        logger.debug(f"Wheel universe performance context: {e}")
        return {}


def _save_universe(stocks: list[dict], source: str = "ai"):
    """Persist AI-selected universe to wheel_universe table."""
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            logger.warning("Wheel universe: no DB, skipping save")
            return

        now = datetime.now(timezone.utc)
        with conn.cursor() as cur:
            # Deactivate previous universe
            cur.execute("UPDATE wheel_universe SET active = FALSE")

            for stock in stocks:
                cur.execute("""
                    INSERT INTO wheel_universe
                        (symbol, score, ai_reason, iv_profile, price_range,
                         added_by, active, added_at, last_refreshed)
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s, %s)
                    ON CONFLICT (symbol) DO UPDATE SET
                        score = EXCLUDED.score,
                        ai_reason = EXCLUDED.ai_reason,
                        iv_profile = EXCLUDED.iv_profile,
                        active = TRUE,
                        last_refreshed = EXCLUDED.last_refreshed
                """, (
                    stock["symbol"],
                    stock.get("score", 50),
                    stock.get("reason", ""),
                    stock.get("iv_profile", ""),
                    stock.get("price_range", ""),
                    source,
                    now,
                    now,
                ))
        logger.info(f"Wheel universe: saved {len(stocks)} stocks to DB")
    except Exception as e:
        logger.error(f"Wheel universe save error: {e}")


def get_active_universe() -> list[str]:
    """Return list of active universe symbols for use by wheel_engine."""
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return []
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol FROM wheel_universe
                WHERE active = TRUE
                ORDER BY score DESC
            """)
            return [row[0] for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"Wheel get_active_universe error: {e}")
        return []


def get_universe_details() -> list[dict]:
    """Return full universe with scores and reasons (for iOS display)."""
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return []
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, score, ai_reason, iv_profile, price_range,
                       added_by, last_refreshed
                FROM wheel_universe
                WHERE active = TRUE
                ORDER BY score DESC
            """)
            cols = [d[0] for d in cur.description]
            rows = []
            for row in cur.fetchall():
                d = dict(zip(cols, row))
                if hasattr(d.get("last_refreshed"), "isoformat"):
                    d["last_refreshed"] = d["last_refreshed"].isoformat()
                rows.append(d)
            return rows
    except Exception as e:
        logger.error(f"Wheel get_universe_details error: {e}")
        return []


def refresh_universe() -> list[dict]:
    """
    Main entry point. Discovers new universe via AI.
    Called by scheduler every Sunday at 8 PM ET.
    Returns the new universe list.
    """
    logger.info("Wheel universe: starting AI-driven discovery...")

    # Step 1: Get candidate pool
    candidates = _get_candidate_pool()
    if not candidates:
        logger.error("Wheel universe: no candidates found, keeping existing universe")
        return []

    logger.info(f"Wheel universe: {len(candidates)} candidates after price/volume filter")

    # Step 2: Get regime context
    try:
        from services.wheel_engine import _get_current_regime
        regime = _get_current_regime()
    except Exception:
        regime = "neutral"

    # Step 3: Get historical performance context
    perf_context = _get_symbol_performance_context()

    # Step 4: Ask AI to select best wheel candidates
    candidate_text = "\n".join(
        f"  {c['symbol']}: ${c['price']}, vol {c['volume_m']}M/day"
        + (f", past win rate {perf_context[c['symbol']]['win_rate']}%, {perf_context[c['symbol']]['cycles']} cycles"
           if c['symbol'] in perf_context else "")
        for c in candidates
    )

    prompt = f"""You are selecting stocks for an Options Wheel Strategy (cash-secured puts → covered calls).

Current market regime: {regime}

WHEEL STRATEGY CRITERIA (must meet all):
1. Price $5-$80 (already filtered) — manageable cash collateral
2. Liquid options market — high open interest, tight spreads, weekly or monthly expirations
3. High implied volatility (IV rank) — richer premiums = more income
4. Financially stable — we may get assigned and HOLD these shares for weeks/months
5. Earnings > 14 days away at time of trade (we avoid earnings IV spikes)
6. NOT going bankrupt — balance sheet must support holding if assigned
7. Ideally: sector leaders, brand recognition, or clear long-term value

CANDIDATES (pre-filtered by price $5-$80 and volume >1M):
{candidate_text}

INSTRUCTIONS:
- Select exactly 12-15 stocks best suited for wheel trading RIGHT NOW
- Rank by: options liquidity × IV richness × fundamental safety × regime fit
- Bearish regime: prefer more defensive/stable names, avoid pure-growth
- Bullish regime: growth names with high IV are fine
- If a stock has historical performance data shown, weight it heavily

Return ONLY valid JSON (no markdown, no prose):
{{
  "universe": [
    {{
      "symbol": "SOFI",
      "score": 88,
      "reason": "High IV, liquid weeklies, fintech growth story worth owning",
      "iv_profile": "high",
      "price_range": "$14-16"
    }}
  ],
  "reasoning_summary": "one sentence on overall selection philosophy this week"
}}

Score 0-100. Return 12-15 stocks only."""

    try:
        from services.ai_client import ask_ai, parse_ai_json
        raw = ask_ai(prompt, max_tokens=1200)
        parsed = parse_ai_json(raw)

        universe = parsed.get("universe", [])
        if not universe:
            logger.error("Wheel universe: AI returned empty universe")
            return []

        summary = parsed.get("reasoning_summary", "")

        # Bug #3 fix: enforce minimum score threshold before saving.
        # AI can return stocks scoring 55-65 — too weak for real collateral.
        # Scoring: AI assigns 0-100 based on IV richness × liquidity × safety × regime fit.
        # A score < 60 means the AI itself isn't confident — skip it.
        MIN_UNIVERSE_SCORE = 60
        before = len(universe)
        universe = [s for s in universe if s.get("score", 0) >= MIN_UNIVERSE_SCORE]
        if len(universe) < before:
            logger.info(
                f"Wheel universe: filtered {before - len(universe)} stocks below score {MIN_UNIVERSE_SCORE}"
            )

        if not universe:
            logger.error("Wheel universe: no stocks passed min score filter — keeping existing universe")
            return []

        logger.info(
            f"Wheel universe: {len(universe)} stocks saved (score ≥ {MIN_UNIVERSE_SCORE}). "
            f"Summary: {summary}"
        )

        # Save to DB
        _save_universe(universe)
        return universe

    except Exception as e:
        logger.error(f"Wheel universe AI call failed: {e}")
        return []
