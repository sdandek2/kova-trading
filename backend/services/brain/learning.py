"""
Phase 6 — ML Learning Loop.

After each closed trade, extract features and outcome.
Train a gradient boosting classifier on Kova's own trade history.
Predictions adjust Kelly sizing: high ML confidence → larger position.

Pipeline:
  1. load_training_data()  — pull closed trades from DB, extract features
  2. train_model()         — fit GradientBoostingClassifier (sklearn)
  3. predict_win_prob()    — P(win) for a candidate trade
  4. ml_conviction_mult()  — translate P(win) → size multiplier for kelly.py

Minimum 50 trades before ML activates. Below that, multiplier = 1.0 (no effect).
Model is retrained every N new trades (default: every 10 closed trades).
Persisted to disk between restarts so we don't lose training data on deploy.
"""
import json
import logging
import os
import pickle
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

_MIN_TRADES       = 50     # trades needed before ML activates
_RETRAIN_EVERY    = 10     # retrain after this many new closed trades since last fit
_MODEL_PATH       = os.path.join(os.path.dirname(__file__), "_learning_model.pkl")
_META_PATH        = os.path.join(os.path.dirname(__file__), "_learning_meta.json")

# Multiplier bounds — never let ML push size below 0.5x or above 1.5x Kelly
_MULT_MIN = 0.5
_MULT_MAX = 1.5
_MULT_NEUTRAL = 1.0   # returned when ML is not yet active


# ── Feature schema ────────────────────────────────────────────────────────────
#
# One-hot encoded categoricals + numeric features.
# Keep this list stable — adding features invalidates old pickled models.

_SIGNAL_TYPES   = ["momentum", "breakout", "reversal", "oversold", "short_candidate", "inverse_etf"]
_REGIMES        = ["bull", "bear", "chop"]
_VIX_LEVELS     = ["low", "normal", "high", "extreme"]
_TIME_BUCKETS   = ["open", "morning", "midday", "afternoon", "close"]   # time-of-day
_DAYS_OF_WEEK   = ["Mon", "Tue", "Wed", "Thu", "Fri"]


def _one_hot(value: str, categories: list[str]) -> list[int]:
    return [1 if value == c else 0 for c in categories]


def _time_bucket(hour_et: int) -> str:
    if hour_et < 10:   return "open"
    if hour_et < 11:   return "morning"
    if hour_et < 13:   return "midday"
    if hour_et < 15:   return "afternoon"
    return "close"


def extract_features(trade: dict) -> Optional[list[float]]:
    """
    Convert a closed-trade dict into a flat feature vector.
    Returns None if required fields are missing.

    Expected trade keys (all optional except symbol):
      signal_type, regime, rs_percentile, rsi_at_entry, macd_at_entry,
      vix_level, entry_hour_et, entry_dow (0=Mon), pl_pct
    """
    try:
        signal  = trade.get("signal_type", "momentum")
        regime  = trade.get("regime", "chop")
        vix     = trade.get("vix_level", "normal")
        rs_pct  = float(trade.get("rs_percentile") or 50.0)
        rsi     = float(trade.get("rsi_at_entry") or 50.0)
        macd    = float(trade.get("macd_at_entry") or 0.0)
        hour    = int(trade.get("entry_hour_et") or 11)
        dow     = int(trade.get("entry_dow") or 2)      # 0=Mon … 4=Fri

        dow_name = _DAYS_OF_WEEK[min(dow, 4)]
        time_bkt = _time_bucket(hour)

        features: list[float] = []
        features += _one_hot(signal, _SIGNAL_TYPES)
        features += _one_hot(regime, _REGIMES)
        features += _one_hot(vix, _VIX_LEVELS)
        features += _one_hot(time_bkt, _TIME_BUCKETS)
        features += _one_hot(dow_name, _DAYS_OF_WEEK)
        features += [
            rs_pct / 100.0,       # normalise 0-1
            rsi / 100.0,
            max(-1.0, min(1.0, macd)),  # clamp extreme MACD values
        ]
        return features
    except Exception as e:
        logger.debug("extract_features failed: %s", e)
        return None


# ── Data loading ──────────────────────────────────────────────────────────────

def load_training_data() -> tuple[list[list[float]], list[int]]:
    """
    Pull all closed trades from DB and return (X, y).
    y=1 for winning trade (pl_pct > 0), y=0 for losing trade.
    """
    trades = _load_trades_from_db()
    X, y = [], []
    for t in trades:
        feats = extract_features(t)
        if feats is None:
            continue
        pl = float(t.get("pl_pct") or 0.0)
        X.append(feats)
        y.append(1 if pl > 0 else 0)
    logger.info("learning: loaded %d labelled trades for training", len(X))
    return X, y


def _load_trades_from_db() -> list[dict]:
    try:
        from services.db import _get_conn
        conn = _get_conn()
        if not conn:
            return []
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    symbol,
                    action                          AS signal_type,
                    EXTRACT(HOUR FROM opened_at)    AS entry_hour_et,
                    EXTRACT(DOW FROM opened_at) - 1 AS entry_dow,
                    CASE WHEN exit_price IS NOT NULL AND entry_price > 0
                         THEN (exit_price - entry_price) / entry_price * 100
                         ELSE NULL END               AS pl_pct
                FROM position_log
                WHERE exit_price IS NOT NULL
                  AND entry_price IS NOT NULL
                  AND entry_price > 0
                ORDER BY closed_at DESC
                LIMIT 2000
            """)
            rows = cur.fetchall()
            cols = ["symbol", "signal_type", "entry_hour_et", "entry_dow", "pl_pct"]
            return [dict(zip(cols, r)) for r in rows if r[-1] is not None]
    except Exception as e:
        logger.warning("learning: DB load failed (%s) — returning []", e)
        return []


# ── Model persistence ─────────────────────────────────────────────────────────

def _save_model(model, n_trades: int) -> None:
    try:
        with open(_MODEL_PATH, "wb") as f:
            pickle.dump(model, f)
        with open(_META_PATH, "w") as f:
            json.dump({
                "n_trades": n_trades,
                "trained_at": datetime.now(timezone.utc).isoformat(),
            }, f)
        logger.info("learning: model saved (%d training samples)", n_trades)
    except Exception as e:
        logger.warning("learning: could not save model — %s", e)


def _load_model():
    """Return (model, n_trades) from disk, or (None, 0) if unavailable."""
    try:
        if not os.path.exists(_MODEL_PATH):
            return None, 0
        with open(_MODEL_PATH, "rb") as f:
            model = pickle.load(f)
        n_trades = 0
        if os.path.exists(_META_PATH):
            with open(_META_PATH) as f:
                meta = json.load(f)
                n_trades = meta.get("n_trades", 0)
        return model, n_trades
    except Exception as e:
        logger.warning("learning: could not load saved model — %s", e)
        return None, 0


# ── Training ──────────────────────────────────────────────────────────────────

def train_model(force: bool = False) -> bool:
    """
    Train (or retrain) the gradient boosting classifier.

    Only trains when:
      - force=True, OR
      - n_new_trades >= _RETRAIN_EVERY since last fit, AND total >= _MIN_TRADES

    Returns True if a model was trained, False otherwise.
    """
    try:
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.model_selection import cross_val_score
        import numpy as np
    except ImportError:
        logger.warning("learning: scikit-learn not installed — ML loop disabled. pip install scikit-learn")
        return False

    X, y = load_training_data()
    n = len(X)

    if n < _MIN_TRADES and not force:
        logger.info("learning: only %d trades — need %d before ML activates", n, _MIN_TRADES)
        return False

    _, prev_n = _load_model()
    new_since_last = n - prev_n

    if not force and new_since_last < _RETRAIN_EVERY:
        logger.debug("learning: %d new trades since last fit — skipping retrain (need %d)", new_since_last, _RETRAIN_EVERY)
        return False

    X_arr = np.array(X, dtype=float)
    y_arr = np.array(y, dtype=int)

    model = GradientBoostingClassifier(
        n_estimators=150,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        min_samples_leaf=5,
        random_state=42,
    )

    # Cross-val to log accuracy before fitting on full set
    if n >= 60:
        try:
            cv_scores = cross_val_score(model, X_arr, y_arr, cv=5, scoring="accuracy")
            logger.info(
                "learning: cross-val accuracy %.1f%% ± %.1f%%",
                cv_scores.mean() * 100,
                cv_scores.std() * 100,
            )
        except Exception:
            pass

    model.fit(X_arr, y_arr)
    _save_model(model, n)

    win_rate_data = sum(y_arr) / len(y_arr) * 100
    logger.info("learning: model trained on %d trades (base win rate %.1f%%)", n, win_rate_data)
    return True


# ── Inference ─────────────────────────────────────────────────────────────────

@dataclass
class MLPrediction:
    win_probability: float    # 0.0 – 1.0
    conviction_mult: float    # multiplier for Kelly sizing (0.5 – 1.5)
    ml_active: bool           # False when insufficient history
    rationale: str


def predict_win_prob(trade_features: dict) -> MLPrediction:
    """
    Predict win probability for a candidate trade.

    Args:
        trade_features: dict with the same keys as extract_features() expects
                        (signal_type, regime, rs_percentile, rsi_at_entry, etc.)

    Returns:
        MLPrediction. If ML not active, win_probability=0.5, mult=1.0.
    """
    model, n_trained = _load_model()

    if model is None or n_trained < _MIN_TRADES:
        return MLPrediction(
            win_probability=0.5,
            conviction_mult=_MULT_NEUTRAL,
            ml_active=False,
            rationale=f"ML inactive ({n_trained}/{_MIN_TRADES} trades)",
        )

    feats = extract_features(trade_features)
    if feats is None:
        return MLPrediction(
            win_probability=0.5,
            conviction_mult=_MULT_NEUTRAL,
            ml_active=False,
            rationale="ML inactive (feature extraction failed)",
        )

    try:
        import numpy as np
        proba = model.predict_proba(np.array([feats]))[0]
        # proba[1] = P(win)
        win_prob = float(proba[1])
        mult = ml_conviction_mult(win_prob)

        return MLPrediction(
            win_probability=round(win_prob, 4),
            conviction_mult=round(mult, 4),
            ml_active=True,
            rationale=(
                f"ML P(win)={win_prob*100:.1f}% → size mult={mult:.2f}x "
                f"(trained on {n_trained} trades)"
            ),
        )
    except Exception as e:
        logger.warning("learning: predict failed — %s", e)
        return MLPrediction(
            win_probability=0.5,
            conviction_mult=_MULT_NEUTRAL,
            ml_active=False,
            rationale=f"ML predict error: {e}",
        )


def ml_conviction_mult(win_probability: float) -> float:
    """
    Translate P(win) into a Kelly size multiplier.

    Mapping:
      P(win) >= 0.70  → 1.5x  (strong edge — size up)
      P(win) >= 0.60  → 1.2x
      P(win) >= 0.50  → 1.0x  (neutral — no change)
      P(win) >= 0.40  → 0.75x (weak edge — size down)
      P(win) <  0.40  → 0.5x  (poor edge — minimum size)
    """
    if win_probability >= 0.70:
        mult = 1.5
    elif win_probability >= 0.60:
        # Linear interpolation 1.2→1.5 over 0.60–0.70
        mult = 1.2 + (win_probability - 0.60) / 0.10 * 0.3
    elif win_probability >= 0.50:
        # Linear interpolation 1.0→1.2 over 0.50–0.60
        mult = 1.0 + (win_probability - 0.50) / 0.10 * 0.2
    elif win_probability >= 0.40:
        # Linear interpolation 0.75→1.0 over 0.40–0.50
        mult = 0.75 + (win_probability - 0.40) / 0.10 * 0.25
    else:
        mult = 0.5

    return round(max(_MULT_MIN, min(_MULT_MAX, mult)), 4)


# ── Post-trade hook ───────────────────────────────────────────────────────────

def on_trade_closed(trade: dict) -> None:
    """
    Call this immediately after a trade closes.
    Triggers a retrain if enough new data has accumulated.

    trade dict should include: symbol, signal_type, regime, rs_percentile,
    rsi_at_entry, macd_at_entry, vix_level, entry_hour_et, entry_dow, pl_pct
    """
    pl = trade.get("pl_pct", 0.0)
    outcome = "WIN" if pl > 0 else "LOSS"
    logger.info(
        "learning: trade closed %s %s pl=%.2f%% — checking retrain trigger",
        trade.get("symbol", "?"), outcome, pl,
    )
    # Fire-and-forget retrain check (non-blocking — skips if not enough new data)
    try:
        train_model(force=False)
    except Exception as e:
        logger.warning("learning: on_trade_closed retrain failed — %s", e)


# ── Feature importance report ─────────────────────────────────────────────────

def feature_importance_report() -> str:
    """Return a human-readable feature importance summary from the trained model."""
    model, n = _load_model()
    if model is None:
        return "Model not yet trained."

    feature_names = (
        [f"signal_{s}" for s in _SIGNAL_TYPES]
        + [f"regime_{r}" for r in _REGIMES]
        + [f"vix_{v}" for v in _VIX_LEVELS]
        + [f"time_{t}" for t in _TIME_BUCKETS]
        + [f"dow_{d}" for d in _DAYS_OF_WEEK]
        + ["rs_percentile", "rsi", "macd_hist"]
    )

    try:
        importances = model.feature_importances_
        paired = sorted(zip(feature_names, importances), key=lambda x: x[1], reverse=True)
        lines = [f"Feature importance (trained on {n} trades):"]
        for name, imp in paired[:12]:   # top 12
            bar = "█" * int(imp * 200)
            lines.append(f"  {name:<28} {imp:.4f}  {bar}")
        return "\n".join(lines)
    except Exception as e:
        return f"Could not compute feature importance: {e}"


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cmd = sys.argv[1] if len(sys.argv) > 1 else "train"

    if cmd == "train":
        trained = train_model(force=True)
        print("Model trained." if trained else "Training skipped (insufficient data).")
    elif cmd == "report":
        print(feature_importance_report())
    elif cmd == "predict":
        # Example prediction with dummy features
        sample = {
            "signal_type": "breakout",
            "regime": "bull",
            "vix_level": "normal",
            "rs_percentile": 82,
            "rsi_at_entry": 58,
            "macd_at_entry": 0.12,
            "entry_hour_et": 10,
            "entry_dow": 1,
        }
        pred = predict_win_prob(sample)
        print(f"P(win)={pred.win_probability:.1%}  mult={pred.conviction_mult:.2f}x  {pred.rationale}")
    else:
        print("Usage: python -m services.brain.learning [train|report|predict]")
