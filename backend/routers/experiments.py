"""Experiment engines endpoints — squeeze, spillover, revision."""

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/experiments", tags=["experiments"])

_ENGINES = {"squeeze", "spillover", "revision"}


def _engine_module(engine: str):
    if engine == "squeeze":
        from services import squeeze_engine
        return squeeze_engine
    if engine == "spillover":
        from services import spillover_engine
        return spillover_engine
    if engine == "revision":
        from services import revision_engine
        return revision_engine
    raise HTTPException(status_code=404, detail=f"Unknown engine: {engine}")


@router.get("/status")
def all_status():
    """Status for all three experiment engines."""
    from services import squeeze_engine, spillover_engine, revision_engine
    return {
        "squeeze":   squeeze_engine.get_status(),
        "spillover": spillover_engine.get_status(),
        "revision":  revision_engine.get_status(),
    }


@router.get("/{engine}/positions")
def positions(engine: str, status: str = None):
    """Open + closed positions for the given engine."""
    if engine not in _ENGINES:
        raise HTTPException(status_code=404, detail=f"Unknown engine: {engine}")
    mod = _engine_module(engine)
    return mod.get_positions(status_filter=status)


@router.get("/{engine}/summary")
def summary(engine: str):
    """P&L, win rate, trade count, best/worst for the given engine."""
    if engine not in _ENGINES:
        raise HTTPException(status_code=404, detail=f"Unknown engine: {engine}")
    mod = _engine_module(engine)
    return mod.get_summary()


@router.post("/{engine}/run")
def manual_run(engine: str):
    """Trigger one scan cycle manually (ignores market-hours check)."""
    if engine not in _ENGINES:
        raise HTTPException(status_code=404, detail=f"Unknown engine: {engine}")
    mod = _engine_module(engine)
    # Reset last_scan_date so the scan actually runs
    if hasattr(mod, "_last_scan_date"):
        mod._last_scan_date = None
    result = mod.run_scan()
    return {"engine": engine, "result": result}


@router.post("/{engine}/close/{position_id}")
def close_position(engine: str, position_id: int):
    """Manually close a position by ID."""
    if engine not in _ENGINES:
        raise HTTPException(status_code=404, detail=f"Unknown engine: {engine}")
    mod = _engine_module(engine)
    return mod.close_position_by_id(position_id)
