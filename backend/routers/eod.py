from fastapi import APIRouter, BackgroundTasks
from services.eod_analysis_service import get_latest_eod_report, run_eod_analysis

router = APIRouter(prefix="/api/eod", tags=["eod"])


@router.get("/latest")
def get_eod_report():
    """Return the latest EOD analysis report. Generated automatically at market close."""
    report = get_latest_eod_report()
    if not report:
        return {"available": False, "message": "No EOD report yet — runs automatically at market close (4 PM ET)."}
    return {"available": True, **report}


@router.post("/run")
def trigger_eod_analysis(background_tasks: BackgroundTasks):
    """Manually trigger an EOD analysis (runs in background, takes ~10s)."""
    background_tasks.add_task(run_eod_analysis)
    return {"status": "started", "message": "EOD analysis running — check /api/eod/latest in ~15 seconds."}
