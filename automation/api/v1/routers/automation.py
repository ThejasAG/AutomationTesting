from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional
from sqlalchemy.orm import Session

from automation.device_manager.service import device_service
from automation.runner.service import runner_service
from automation.ai.services.intelligence import AIGitImpactAnalyzer, AITestRecommendationEngine
from automation.auth.security import require_role
from automation.database.config import get_db
from automation.database.database import create_ai_recommendation
from automation.projects.repository import ProjectRepository
from automation.database.models import TestProject, TestRun

router = APIRouter(prefix="/automation", tags=["Automation"])

class RunRequest(BaseModel):
    project_id: str
    device_id: str
    run_id: Optional[str] = None
    triggered_by: Optional[str] = None

class StopRequest(BaseModel):
    run_id: str

@router.get("/devices")
def get_devices():
    devices = device_service.get_all_devices()
    return {
        "count": len(devices),
        "devices": devices,
        "warning": None if devices else "No devices connected. Connect a physical or emulator device via ADB and restart the agent."
    }

@router.get("/devices/{device_id}")
def get_device(device_id: str):
    device = device_service.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device

@router.get("/devices/{device_id}/health")
def get_device_health(device_id: str):
    health = device_service.get_device_health(device_id)
    if not health:
        raise HTTPException(status_code=404, detail="Health data not available")
    return health

@router.post("/devices/refresh")
def refresh_devices():
    device_service.refresh_devices()
    return {"status": "Refreshed"}

@router.post("/devices/{device_id}/health-check")
def force_health_check(device_id: str):
    device_service.refresh_devices()
    health = device_service.get_device_health(device_id)
    if not health:
        raise HTTPException(status_code=404, detail="Health data not available")
    return health

@router.get("/tests")
def get_tests():
    return {"suites": runner_service.discover_tests()}

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

@router.get("/status/{run_id}")
def get_status(run_id: str):
    return runner_service.get_run_status(run_id)

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

@router.post("/plan")
def generate_plan(request: RunRequest, db: Session = Depends(get_db)):
    try:
        # 1. Fetch the real project and get actual git diff
        project = db.query(TestProject).filter(TestProject.id == request.project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        repo = ProjectRepository(project.id, project.git_url)
        real_diff = repo.get_git_diff()

        # 2. Analyze Git Impact using actual diff
        analyzer = AIGitImpactAnalyzer()
        impact = analyzer.analyze(real_diff or "No recent changes detected")
        
        # 3. Recommend Tests
        recommender = AITestRecommendationEngine()
        tests = recommender.recommend(impact)
        
        # 4. Create Recommendation
        payload = {
            "risk_score": impact.risk_level,
            "impacted_modules": impact.impacted_modules,
            "suggested_tests": tests,
            "git_diff_analyzed": bool(real_diff)
        }
        
        rec = create_ai_recommendation(db, {
            "project_id": request.project_id,
            "type": "test_plan",
            "payload": payload,
            "status": "pending"
        })
        
        return {"status": "plan_generated", "recommendation_id": rec.id, "plan": payload}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
