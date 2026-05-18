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

from config import settings

logger = logging.getLogger(__name__)

# ── Clients (initialised once at import time) ──────────────────────────────

_anthropic = anthropic.Anthropic(api_key=settings.anthropic_api_key)

# Gemini is optional — only available if google-genai is installed
_gemini = None
_genai_types = None
try:
    from google import genai as _google_genai
    from google.genai import types as _genai_types
    if settings.gemini_api_key:
        _gemini = _google_genai.Client(api_key=settings.gemini_api_key)
        logger.info("Gemini client initialized.")
except ImportError:
    logger.info("google-genai not installed — using Claude only.")


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
    if not _gemini:
        raise RuntimeError("Gemini client not initialised.")
    response = _gemini.models.generate_content(
        model=model,
        contents=prompt,
        config=_genai_types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            temperature=0.2,
        ),
    )
    return response.text.strip()


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
