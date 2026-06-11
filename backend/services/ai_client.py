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
            "pro_primary":       "claude-sonnet-4-6",
            "pro_fallback":      "gemini-2.5-flash",
            "standard_primary":  "claude-haiku-4-5-20251001",
            "standard_fallback": "gemini-2.5-flash",
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

    # Disable thinking on ALL Gemini models — thinking tokens are 11x more expensive
    # and add no meaningful benefit for structured JSON trade decisions.
    # Both Flash and Pro support thinkingBudget=0 as of 2025.
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


# ── Tier 3: Pure-AI trader — Claude with server-side web search ─────────────
# Used ONLY by the pure-AI experiment (services/pureai_engine.py).
# The model runs its own web searches mid-reasoning; we never curate context.

def ask_ai_with_search(prompt: str, model: str = "claude-opus-4-8",
                       max_tokens: int = 8000,
                       max_searches: int = 8) -> tuple[str, list[str]]:
    """
    Single-turn call where Claude can search the web before answering.
    Returns (response_text, search_queries_used).

    Notes:
      - web_search_20260209 is a server-side tool: Anthropic executes the
        searches; we just declare the tool and handle pause_turn continuations.
      - No structured-output mode: search citations are incompatible with it,
        so the caller parses JSON from text via parse_ai_json (json-repair).
      - No sampling params: removed on Opus 4.7+ (400 if sent).
    """
    tools = [{
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": max_searches,
        "allowed_callers": ["direct"],  # required for Haiku; harmless on Opus/Sonnet
    }]
    messages = [{"role": "user", "content": prompt}]
    searches: list[str] = []
    response = None

    # Server-side tool loop can pause at its iteration limit (stop_reason
    # "pause_turn") — re-send to let it resume. Cap continuations at 5.
    # Adaptive thinking only supported on Opus and Sonnet, not Haiku
    _supports_thinking = any(x in model for x in ("opus", "sonnet"))
    _kwargs = {"thinking": {"type": "adaptive"}} if _supports_thinking else {}

    for _ in range(5):
        response = _anthropic.messages.create(
            model=model,
            max_tokens=max_tokens,
            tools=tools,
            messages=messages,
            **_kwargs,
        )
        for block in response.content:
            if getattr(block, "type", "") == "server_tool_use" and \
               getattr(block, "name", "") == "web_search":
                q = (getattr(block, "input", None) or {}).get("query")
                if q:
                    searches.append(q)
        if response.stop_reason != "pause_turn":
            break
        messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": response.content},
        ]

    text = "".join(
        block.text for block in (response.content if response else [])
        if getattr(block, "type", "") == "text"
    ).strip()
    logger.info(f"ask_ai_with_search: {model}, {len(searches)} searches, stop={getattr(response, 'stop_reason', '?')}")
    return text, searches
