"""
prompt.py — Read-only prompt viewer + append-only override for Kova.

GET  /api/prompt/last     — returns the last Step 1 + Step 2 prompts sent to Claude
GET  /api/prompt/override — returns current override text (or null)
POST /api/prompt/override — appends extra instructions to every Claude prompt
DELETE /api/prompt/override — clears the override
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api/prompt", tags=["prompt"])


@router.get("/last")
def get_last_prompts():
    """
    Return the most recent Step 1 and Step 2 prompts sent to Claude.
    Includes timestamp so the app can show when the last cycle ran.
    """
    from services.db import get_setting
    data = get_setting("last_prompts")
    if not data:
        return {
            "available": False,
            "message": "No prompts recorded yet — waiting for first trading cycle.",
            "saved_at": None,
            "step1": None,
            "step2": None,
        }
    return {
        "available": True,
        "saved_at": data.get("saved_at"),
        "step1": data.get("step1"),
        "step2": data.get("step2"),
    }


class OverrideRequest(BaseModel):
    text: Optional[str] = None  # None or empty string = clear override


@router.get("/override")
def get_override():
    """Return the current prompt override text, or null if none set."""
    from services.db import get_setting
    text = get_setting("prompt_override")
    return {
        "override": text or None,
        "active": bool(text and text.strip()),
    }


@router.post("/override")
def set_override(req: OverrideRequest):
    """
    Set append-only override instructions injected into every Claude prompt.
    Examples: "avoid tech stocks today", "focus on energy sector", "be conservative".
    The core prompt rules stay intact — this only appends extra context.
    Pass text=null or text="" to clear.
    """
    from services.db import set_setting
    text = (req.text or "").strip()
    if text:
        set_setting("prompt_override", text)
        return {"message": "Override set — will apply from next trading cycle", "override": text}
    else:
        set_setting("prompt_override", None)
        return {"message": "Override cleared", "override": None}


@router.delete("/override")
def clear_override():
    """Clear the prompt override — Claude returns to default behaviour."""
    from services.db import set_setting
    set_setting("prompt_override", None)
    return {"message": "Override cleared", "override": None}
