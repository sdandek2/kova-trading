"""
ai_client.py — unified AI wrapper for Kova.

Two tiers:
  ask_ai_pro()  — critical calls (trade decisions, EOD, daily picks, earnings direction)
                  Primary: Gemini 2.5 Pro (no thinking)  |  Fallback: Claude Sonnet 4.6
                  ~$0.019/call vs Sonnet's $0.032 — 40% cheaper, same quality.

  ask_ai()      — non-critical calls (stock predictions, suggestions)
                  Primary: Gemini 2.5 Flash (no thinking)  |  Fallback: Claude Haiku 4.5
                  ~$0.0006/call — essentially free.

Thinking is disabled for all models:
  - Flash: thinking tokens ($3.50/1M) are 11× more expensive than output ($0.30/1M)
  - Pro: thinking adds cost but no meaningful benefit for structured JSON responses
  JSON mime type forces clean output, json-repair handles any remaining issues.

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

    generation_config: dict = {
        "maxOutputTokens": max_tokens,
        "temperature": 0.2,
        "responseMimeType": "application/json",  # cleaner output, fewer tokens billed
    }

    # Flash: disable thinking entirely (thinkingBudget=0, valid range 0–24576).
    # Pro: no thinkingConfig → dynamic thinking (Pro cannot disable it; range 128–32768).
    # Letting Pro think freely is worth the cost for critical trade decisions.
    if "flash" in model:
        generation_config["thinkingConfig"] = {"thinkingBudget": 0}

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
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


# ── Shared JSON parser ─────────────────────────────────────────────────────

def parse_ai_json(raw: str) -> dict:
    """
    Robustly parse JSON from an AI response using json-repair.
    Handles all common LLM JSON issues: markdown fences, trailing commas,
    missing commas, single quotes, Python literals, unquoted keys, etc.
    """
    import re, json
    from json_repair import repair_json

    text = raw.strip()

    # 1. Strip markdown code fences
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{") or candidate.startswith("["):
                text = candidate
                break

    # 2. Extract the outermost JSON block if model added prose around it
    match = re.search(r'(\{.*\}|\[.*\])', text, re.DOTALL)
    if match:
        text = match.group()

    # 3. Fast path — valid JSON, no repair needed
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 4. Repair and parse — handles trailing commas, missing commas,
    #    single quotes, Python literals, unquoted keys, truncated JSON, etc.
    repaired = repair_json(text, return_objects=True)
    if isinstance(repaired, (dict, list)):
        return repaired
    raise ValueError(f"Could not parse AI JSON even after repair. raw[:200]: {raw[:200]}")


# ── Tier 1: Critical calls ─────────────────────────────────────────────────
# Gemini 2.5 Pro (no thinking, no grounding) | Fallback: Claude Sonnet 4.6
# Used for: Step 1, Step 2, daily picks, EOD analysis, earnings direction.

def ask_ai_pro(prompt: str, max_tokens: int = 600) -> str:
    """
    Critical calls — trade decisions, EOD analysis, daily picks, earnings direction.
    Grounding disabled (saves $0.035/call). Thinking already off (thinkingBudget=0).
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
# Gemini 2.5 Flash (no thinking) | Fallback: Claude Haiku 4.5

def ask_ai(prompt: str, max_tokens: int = 600) -> str:
    """
    Non-critical calls — stock predictions, suggestions.
    Flash without thinking: ~$0.0006/call — essentially free at scale.
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
