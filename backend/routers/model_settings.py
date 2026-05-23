"""
model_settings.py — AI model configuration for Kova.

GET  /api/settings/models  — returns current primary + fallback model for each tier
POST /api/settings/models  — updates model selection, persisted to DB

Tiers:
  pro   — Step 2 trade decisions only
  standard — Step 1, earnings prediction, EOD analysis, daily picks, predictions
"""

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix="/api/settings", tags=["model_settings"])

# ── Available models — same list for all slots, user can freely mix providers ──

ALL_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-3-pro",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "claude-opus-4-6",
]

# Aliases for backwards compatibility
PRO_PRIMARY_OPTIONS     = ALL_MODELS
PRO_FALLBACK_OPTIONS    = ALL_MODELS
STANDARD_PRIMARY_OPTIONS  = ALL_MODELS
STANDARD_FALLBACK_OPTIONS = ALL_MODELS

# ── Defaults ──────────────────────────────────────────────────────────────

DEFAULTS = {
    # Pro tier: Step 2 trade decisions only — highest quality model
    "pro_primary":        "claude-sonnet-4-6",
    "pro_fallback":       "gemini-2.5-flash",
    # Standard tier: Step 1 nomination, earnings prediction, suggestions — fast + cheap
    "standard_primary":   "gemini-2.5-flash",
    "standard_fallback":  "claude-haiku-4-5-20251001",
}


def get_model_settings() -> dict:
    """Return current model settings from DB, falling back to defaults."""
    from services.db import get_setting
    saved = get_setting("model_settings") or {}
    return {
        "pro_primary":       saved.get("pro_primary",       DEFAULTS["pro_primary"]),
        "pro_fallback":      saved.get("pro_fallback",      DEFAULTS["pro_fallback"]),
        "standard_primary":  saved.get("standard_primary",  DEFAULTS["standard_primary"]),
        "standard_fallback": saved.get("standard_fallback", DEFAULTS["standard_fallback"]),
    }


# ── Request / Response models ─────────────────────────────────────────────

class ModelSettingsRequest(BaseModel):
    pro_primary:       Optional[str] = None
    pro_fallback:      Optional[str] = None
    standard_primary:  Optional[str] = None
    standard_fallback: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.get("/models")
def get_models():
    """Return current AI model configuration for both tiers."""
    current = get_model_settings()
    return {
        "pro": {
            "primary":          current["pro_primary"],
            "fallback":         current["pro_fallback"],
            "primary_options":  PRO_PRIMARY_OPTIONS,
            "fallback_options": PRO_FALLBACK_OPTIONS,
        },
        "standard": {
            "primary":          current["standard_primary"],
            "fallback":         current["standard_fallback"],
            "primary_options":  STANDARD_PRIMARY_OPTIONS,
            "fallback_options": STANDARD_FALLBACK_OPTIONS,
        },
    }


@router.post("/models")
def update_models(req: ModelSettingsRequest):
    """Update AI model selection. Only provided fields are updated."""
    from services.db import get_setting, set_setting

    current = get_setting("model_settings") or {}

    if req.pro_primary is not None:
        if req.pro_primary not in PRO_PRIMARY_OPTIONS:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail=f"Invalid pro primary model: {req.pro_primary}")
        current["pro_primary"] = req.pro_primary

    if req.pro_fallback is not None:
        if req.pro_fallback not in PRO_FALLBACK_OPTIONS:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail=f"Invalid pro fallback model: {req.pro_fallback}")
        current["pro_fallback"] = req.pro_fallback

    if req.standard_primary is not None:
        if req.standard_primary not in STANDARD_PRIMARY_OPTIONS:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail=f"Invalid standard primary model: {req.standard_primary}")
        current["standard_primary"] = req.standard_primary

    if req.standard_fallback is not None:
        if req.standard_fallback not in STANDARD_FALLBACK_OPTIONS:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail=f"Invalid standard fallback model: {req.standard_fallback}")
        current["standard_fallback"] = req.standard_fallback

    set_setting("model_settings", current)

    return {
        "message": "Model settings updated — takes effect on next AI call",
        "settings": get_model_settings(),
    }
