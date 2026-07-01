from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from sqlalchemy.orm import Session
from datetime import datetime
import json
from automation.ai.services.execution import AISelfHealingEngine
from automation.reporting.engine import reporting_engine
from automation.database.database import create_ai_recommendation
from automation.database.config import get_db
from automation.database.models import TestRun, TestProject
from automation.auth.security import get_current_user

router = APIRouter(prefix="/jobs", tags=["Jobs"])

class PollJobRequest(BaseModel):
    agent_id: str
    connected_devices: List[str] # List of device IDs

class JobStatusRequest(BaseModel):
    status: str
    logs: List[str]
    timeline_event: Optional[str] = None
    error_message: Optional[str] = None

class EvidenceUploadRequest(BaseModel):
    evidence: Dict[str, Any]

class StreamRequest(BaseModel):
    logs: str
    screen_state: str

@router.post("/poll")
def poll_job(req: PollJobRequest, db: Session = Depends(get_db)):
    """Agent asks for the next queued job it can run."""
    # Find a job that is 'queued' and where the requested device matches one of the agent's connected devices
    # (For simplicity, our job has a `device_name` string which currently holds the requested device ID)
    
    # We query queued jobs, ordered by created_at ascending (FIFO)
    queued_jobs = db.query(TestRun).filter(TestRun.job_state == "queued").order_by(TestRun.created_at.asc()).all()
    
    for job in queued_jobs:
        # If this job asks for a device that this agent has
        if job.device_name in req.connected_devices:
            # Assign this job to the agent
            job.job_state = "assigned"
            job.agent_id = req.agent_id
            job.started_at = datetime.utcnow()
            
            project = db.query(TestProject).filter(TestProject.id == job.project_id).first()
            
            db.commit()
            
            return {
                "job_id": job.id,
                "project_id": job.project_id,
                "git_url": project.git_url if project else "",
                "branch": project.default_branch if project else "main",
                "device_id": job.device_name
            }
            
    # No jobs found for this agent
    return {"job_id": None}

@router.post("/{job_id}/status")
def update_job_status(job_id: str, req: JobStatusRequest, db: Session = Depends(get_db)):
    """Agent updates the status, logs, and timeline of a running job."""
    job = db.query(TestRun).filter(TestRun.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
        
    job.job_state = req.status
    if req.status in ["passed", "failed", "stopped"]:
        job.status = req.status
        job.completed_at = datetime.utcnow()
        if job.started_at:
            job.duration_ms = int((job.completed_at - job.started_at).total_seconds() * 1000)
    elif req.status == "running":
        job.status = "running"
        
    if req.error_message:
        job.error_message = req.error_message
        
    if req.timeline_event:
        timeline = []
        if job.timeline:
            try:
                timeline = json.loads(job.timeline)
            except:
                pass
        timeline.append({
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "event": req.timeline_event
        })
        job.timeline = json.dumps(timeline)
        
    db.commit()
    
    # Live logs will be integrated into the RunnerService for websocket streaming,
    # or we can push to a redis/memory cache for the frontend to consume.
    from automation.runner.service import runner_service
    runner_service.append_agent_logs(job_id, req.logs, req.status)
    
    return {"status": "ok"}
    
@router.post("/{job_id}/stream")
def stream_logs(job_id: str, req: StreamRequest, db: Session = Depends(get_db)):
    """Live execution AI monitor."""
    job = db.query(TestRun).filter(TestRun.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
        
    # Analyze stream for hangs/errors
    if "Error" in req.logs or "Exception" in req.logs or "ElementNotFound" in req.logs:
        engine = AISelfHealingEngine()
        action = engine.heal(req.logs, req.screen_state)
        
        # Create recommendation for self-healing
        rec = create_ai_recommendation(db, {
            "project_id": job.project_id,
            "run_id": job.id,
            "type": "self_heal",
            "payload": {"logs": req.logs, "suggested_action": action.model_dump()},
            "status": "pending"
        })
        return {"status": "healing_suggested", "recommendation_id": rec.id, "action": action.model_dump()}
        
    return {"status": "ok"}

@router.post("/{job_id}/evidence")
def upload_evidence(job_id: str, req: EvidenceUploadRequest, db: Session = Depends(get_db)):
    """Agent uploads evidence upon completion."""
    from automation.database.database import insert_evidence
    
    # Insert evidence
    insert_evidence(db, req.evidence)
    
    # If the job failed, we can trigger RCA on the platform side here, rather than the agent side!
    # Because the agent might not have the OpenAI API key, the platform does.
    job = db.query(TestRun).filter(TestRun.id == job_id).first()
    if job and job.status == "failed":
        from automation.ai.services.reporting import AIBugReportGenerator
        from automation.ai.services.intelligence import AIFailureClusteringEngine
        from automation.database.database import create_ai_recommendation
        
        # Generate Bug Report
        bug_gen = AIBugReportGenerator()
        bug_report = bug_gen.generate(
            {"run_id": job.id, "error": job.error_message},
            req.evidence
        )
        
        # Cluster Failure
        cluster_engine = AIFailureClusteringEngine()
        clusters = cluster_engine.cluster([{"run_id": job.id, "error": job.error_message}])
        
        # Save Recommendation
        create_ai_recommendation(db, {
            "project_id": job.project_id,
            "run_id": job.id,
            "type": "bug_report",
            "payload": bug_report.model_dump(),
            "status": "pending"
        })
        
        from automation.database.database import insert_rca_report
        from automation.ai.provider import default_config
        rca_data = {
            "run_id": job_id,
            "root_cause": "Identified by AIFailureClusteringEngine",
            "failure_category": clusters[0].cluster_label if clusters else "Unknown",
            "confidence": 80,
            "possible_reason": bug_report.description if bug_report else "AI Generated Report",
            "impact": "High",
            "suggested_fix": bug_report.suggested_fix if bug_report else "Review AI Bug Report",
            "priority": "High",
            "severity": "High",
            "responsible_module": "Unknown",
            "summary": bug_report.summary if bug_report else "AI generated analysis",
            "llm_provider": default_config.get("type", "not_configured"),
            "llm_model": default_config.get("model_name", "not_configured"),
            "generated_at": datetime.utcnow(),
        }
        insert_rca_report(db, rca_data)
        
    report_path = reporting_engine.generate_html_report(job_id, {"status": job.status, "duration_ms": job.duration_ms, "logs": []})
        
    return {"status": "ok", "report_path": report_path}
