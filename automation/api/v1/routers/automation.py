from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session

from automation.device_manager.service import device_service
from automation.device_manager.models import DeviceStatus
from automation.runner.service import runner_service
from automation.auth.security import require_role
from automation.database.config import get_db
from automation.database.models import TestRun

router = APIRouter(prefix="/automation", tags=["Automation"])

class RunRequest(BaseModel):
    project_id: str
    device_id: str
    run_id: Optional[str] = None
    triggered_by: Optional[str] = None

class StopRequest(BaseModel):
    run_id: str

@router.get("/engine-status")
def engine_status():
    """Live health of the automation engine so the UI can show what's happening
    instead of a run looking like it hung. Cheap checks only."""
    import subprocess
    from automation.projects.builder import app_builder
    appium = app_builder._appium_healthy()
    try:
        out = subprocess.run(["xcrun", "simctl", "list", "devices", "booted"],
                             capture_output=True, text=True, timeout=8).stdout
        booted = [ln.strip() for ln in out.splitlines()
                  if "Booted" in ln and ("iPhone" in ln or "iPad" in ln)]
    except Exception:
        booted = []
    return {
        "appium": {"healthy": appium, "url": app_builder.APPIUM_URL,
                   "note": "ready" if appium else "not running — it will auto-start on the next run"},
        "booted_simulators": booted,
        "ready": appium,
    }


@router.get("/devices")
def get_devices():
    # Discover local iOS simulators directly so the dropdown is populated even
    # when no agent is running (booted sims first — those are runnable now).
    try:
        device_service.discover_local_simulators(booted_only=False)
    except Exception:
        pass
    devices = device_service.get_all_devices()
    # Sort ONLINE (booted) first so a runnable device is preselected.
    devices = sorted(devices, key=lambda d: 0 if getattr(d, "status", None) == DeviceStatus.ONLINE else 1)
    return {
        "count": len(devices),
        "devices": devices,
        "warning": None if devices else "No simulators found. Boot an iOS simulator (Xcode ▸ Open Developer Tool ▸ Simulator) and refresh."
    }

@router.post("/run")
def start_run(request: RunRequest):
    try:
        run_id = runner_service.execute_project(
            project_id=request.project_id,
            device_id=request.device_id,
            run_id=request.run_id,
            triggered_by=request.triggered_by
        )
        return {"status": "started", "run_id": run_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/stop")
def stop_run(req: StopRequest):
    runner_service.stop_run(req.run_id)
    return {"status": "Stopped"}

@router.post("/cancel-all-queued")
def cancel_all_queued(
    db: Session = Depends(get_db),
    current_user=Depends(require_role(["admin"])),
):
    """Cancel every job still sitting in the queue. Admin only.

    Drains the backlog so agents stop picking up stale work.
    """
    count = (
        db.query(TestRun)
        .filter(TestRun.job_state == "queued")
        .update({"job_state": "cancelled", "status": "cancelled"}, synchronize_session=False)
    )
    db.commit()

    return {
        "cancelled": count,
        "message": f"Cancelled {count} queued job{'s' if count != 1 else ''}.",
    }
