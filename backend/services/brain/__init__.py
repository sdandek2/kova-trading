from .regime import detect_regime, RegimeResult
from .rs_ranking import rank_universe, RSScore, get_rs_map, filter_by_rs
from .kelly import kelly_size, KellyResult
from .signals import score_universe, ScoredCandidate, format_candidates_for_prompt

# ai_brain imports pydantic models — lazy import to avoid startup errors
# in environments without pydantic (e.g. local testing).
def decide(*args, **kwargs):
    from .ai_brain import decide as _decide
    return _decide(*args, **kwargs)

__all__ = [
    "detect_regime", "RegimeResult",
    "rank_universe", "RSScore", "get_rs_map", "filter_by_rs",
    "kelly_size", "KellyResult",
    "score_universe", "ScoredCandidate", "format_candidates_for_prompt",
    "decide",
]
