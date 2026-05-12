"""
ai_client.py — unified AI wrapper for Kova.

Tries Gemini first; if it fails (quota, network, bad response) falls back
to Claude (Anthropic).  Both paths return a plain string so callers don't
care which model actually ran.
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


# ── Public helper ──────────────────────────────────────────────────────────

def ask_ai(prompt: str, max_tokens: int = 600) -> str:
    """
    Send *prompt* to Claude (primary).  If that fails, fall back to Gemini.
    Returns the model's text reply as a plain string.

    Raises RuntimeError only if both providers fail.
    """
    # --- Try Claude first ---
    try:
        msg = _anthropic.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        logger.info("ask_ai: served by Claude")
        return text
    except Exception as exc:
        logger.warning(f"Claude failed ({exc}); falling back to Gemini.")

    # --- Fall back to Gemini ---
    if _gemini:
        try:
            response = _gemini.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
                config=_genai_types.GenerateContentConfig(
                    max_output_tokens=max_tokens,
                    temperature=0.2,
                ),
            )
            text = response.text.strip()
            logger.info("ask_ai: served by Gemini (fallback)")
            return text
        except Exception as exc:
            logger.warning(f"Gemini fallback also failed ({exc}).")

    raise RuntimeError("Both Claude and Gemini failed.")
