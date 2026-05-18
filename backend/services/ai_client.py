"""
ai_client.py — unified AI wrapper for Kova.

Two tiers:
  ask_ai_pro()  — critical calls (trade decisions, EOD, daily picks, earnings direction)
                  Primary: Gemini 2.5 Pro  |  Fallback: Claude Sonnet 4.6

  ask_ai()      — non-critical calls (stock predictions, suggestions)
                  Primary: Gemini 2.5 Flash  |  Fallback: Claude Haiku 4.5

Both functions return a plain string so callers don't care which model ran.
"""

import logging

import anthropic
import httpx

from config import settings

logger = logging.getLogger(__name__)

# ── Clients (initialised once at import time) ──────────────────────────────

_anthropic = anthropic.Anthropic(api_key=settings.anthropic_api_key)

# Gemini via REST API — no google-genai package needed (avoids websockets conflict with alpaca-py)
_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def _get_model_config() -> dict:
    """Load model config from DB. Returns defaults if DB is unavailable."""
    try:
        from routers.model_settings import get_model_settings
        return get_model_settings()
    except Exception:
        return {
            "pro_primary":       "gemini-2.5-pro",
            "pro_fallback":      "claude-sonnet-4-6",
            "standard_primary":  "gemini-2.5-flash",
            "standard_fallback": "claude-haiku-4-5-20251001",
        }


def _is_gemini(model: str) -> bool:
    return model.startswith("gemini")


def _call_gemini(model: str, prompt: str, max_tokens: int) -> str:
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY not set.")
    url = f"{_GEMINI_BASE}/{model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": 0.2,
        },
    }
    response = httpx.post(
        url,
        json=payload,
        params={"key": settings.gemini_api_key},
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()
    candidates = data.get("candidates", [])
    if not candidates:
        # Safety block or empty response — treat as failure so fallback kicks in
        block_reason = data.get("promptFeedback", {}).get("blockReason", "unknown")
        raise RuntimeError(f"Gemini returned no candidates (blockReason: {block_reason})")
    return candidates[0]["content"]["parts"][0]["text"].strip()


def _call_claude(model: str, prompt: str, max_tokens: int) -> str:
    msg = _anthropic.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _call_model(model: str, prompt: str, max_tokens: int) -> str:
    if _is_gemini(model):
        return _call_gemini(model, prompt, max_tokens)
    return _call_claude(model, prompt, max_tokens)


# ── Tier 1: Critical calls ─────────────────────────────────────────────────
# Default: Gemini 2.5 Pro primary | Claude Sonnet 4.6 fallback
# Configurable via /api/settings/models

def ask_ai_pro(prompt: str, max_tokens: int = 600) -> str:
    """
    Critical calls — trade decisions, EOD analysis, daily picks, earnings direction.
    Models loaded from DB on each call so UI changes take effect immediately.
    Raises RuntimeError only if both providers fail.
    """
    config = _get_model_config()
    primary  = config["pro_primary"]
    fallback = config["pro_fallback"]

    try:
        text = _call_model(primary, prompt, max_tokens)
        logger.info(f"ask_ai_pro: served by {primary}")
        return text
    except Exception as exc:
        logger.warning(f"{primary} failed ({exc}); falling back to {fallback}.")

    try:
        text = _call_model(fallback, prompt, max_tokens)
        logger.info(f"ask_ai_pro: served by {fallback} (fallback)")
        return text
    except Exception as exc:
        logger.warning(f"{fallback} fallback also failed ({exc}).")

    raise RuntimeError(f"Both {primary} and {fallback} failed.")


# ── Tier 2: Non-critical calls ─────────────────────────────────────────────
# Default: Gemini 2.5 Flash primary | Claude Haiku 4.5 fallback
# Configurable via /api/settings/models

def ask_ai(prompt: str, max_tokens: int = 600) -> str:
    """
    Non-critical calls — stock predictions, suggestions.
    Models loaded from DB on each call so UI changes take effect immediately.
    Raises RuntimeError only if both providers fail.
    """
    config = _get_model_config()
    primary  = config["standard_primary"]
    fallback = config["standard_fallback"]

    try:
        text = _call_model(primary, prompt, max_tokens)
        logger.info(f"ask_ai: served by {primary}")
        return text
    except Exception as exc:
        logger.warning(f"{primary} failed ({exc}); falling back to {fallback}.")

    try:
        text = _call_model(fallback, prompt, max_tokens)
        logger.info(f"ask_ai: served by {fallback} (fallback)")
        return text
    except Exception as exc:
        logger.warning(f"{fallback} fallback also failed ({exc}).")

    raise RuntimeError(f"Both {primary} and {fallback} failed.")
