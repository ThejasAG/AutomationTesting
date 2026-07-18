from fastapi import APIRouter, Depends, HTTPException, Header, Request
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from sqlalchemy.orm import Session
from datetime import datetime
import json
import logging
import os
import re
import httpx
from automation.ai.services.execution import AISelfHealingEngine
from automation.reporting.engine import reporting_engine
from automation.database.database import create_ai_recommendation, utc_iso
from automation.database.config import get_db
from automation.database.models import TestRun, TestProject, ScenarioResult
from automation.auth.security import get_current_user

logger = logging.getLogger("jobs")

router = APIRouter(prefix="/jobs", tags=["Jobs"])

# `/runs/...` cannot live on the /jobs-prefixed router. This sibling router is
# mounted at /api/v1 in main.py so the bot hits /api/v1/runs/{id}/scenario-result.
runs_router = APIRouter(prefix="/runs", tags=["Runs"])


# ── Android bot bridge ───────────────────────────────────────────────────────

class ScenarioResultIn(BaseModel):
    scenario_num: str
    scenario_name: str
    status: str                      # PASS | FAIL — recomputed from the two sides
    # The Vya bot reports ONE role per call (role=Consumer/Business). When role is
    # given, that side's status is set and the row is merged. When it is absent,
    # consumer_status/business_status are taken directly (Android-bridge form).
    role: Optional[str] = None
    consumer_status: str = "N/A"
    business_status: str = "N/A"
    error: Optional[str] = None
    reasons: Optional[List[str]] = None
    launch_time: Optional[float] = None


class TriggerAndroidBotIn(BaseModel):
    scenarios: List[Any] = []
    consumer_device: Optional[str] = None
    business_device: Optional[str] = None
    bot_url: str = "http://localhost:9000"


def _both_pass_status(consumer: str, business: str, incoming: str) -> str:
    """The cross-app gate: FAIL if either side failed; else the incoming status."""
    if consumer == "FAIL" or business == "FAIL":
        return "FAIL"
    return incoming


def _recompute_run_status(db: Session, run: TestRun) -> str:
    """A run passes only when every scenario passed; any FAIL fails the run."""
    rows = db.query(ScenarioResult).filter(ScenarioResult.run_id == run.id).all()
    if not rows:
        return run.status
    any_fail = any(r.status == "FAIL" for r in rows)
    run.status = "failed" if any_fail else "passed"
    run.job_state = run.status
    return run.status


def trigger_android_bot(
    db: Session,
    run: TestRun,
    scenarios: List[Any],
    consumer_device: Optional[str],
    business_device: Optional[str],
    bot_url: str,
) -> Dict[str, Any]:
    """Mark a run as an Android bot run and hand it to the bot adapter.

    Shared by the JWT endpoint and the GitHub webhook so both trigger the bot the
    same way. The callback_url is where the bot POSTs each scenario result back.
    """
    run.job_state = "running"
    run.bot_type = "android"
    db.commit()

    platform_base = os.getenv("PLATFORM_BASE_URL", "http://localhost:8000")
    callback_url = f"{platform_base}/api/v1/runs/{run.id}/scenario-result"

    payload = {
        "run_id": run.id,
        "scenarios": scenarios,
        "callback_url": callback_url,
        "consumer_device": consumer_device,
        "business_device": business_device,
    }

    delivered = False
    try:
        with httpx.Client(timeout=10.0) as client:
            client.post(f"{bot_url.rstrip('/')}/run", json=payload)
        delivered = True
    except Exception as exc:
        # Not fatal: the run stays "running" and the bot can still pick the job
        # up by polling the adapter. Surface it rather than pretending it sent.
        logger.warning("Could not reach Android bot adapter at %s: %s", bot_url, exc)

    return {
        "triggered": True,
        "run_id": run.id,
        "callback_url": callback_url,
        "delivered_to_bot": delivered,
        "message": "Android bot triggered",
    }


@runs_router.post("/{run_id}/scenario-result")
def post_scenario_result(
    run_id: str,
    body: ScenarioResultIn,
    db: Session = Depends(get_db),
    x_bot_secret: Optional[str] = Header(default=None),
):
    """Android bot posts ONE scenario result. No JWT — the bot is not a browser.

    Authenticated with the X-Bot-Secret header when BOT_SECRET is set; open in
    dev when it is not.
    """
    expected = os.getenv("BOT_SECRET", "")
    if expected and x_bot_secret != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Bot-Secret")

    run = db.query(TestRun).filter(TestRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Upsert ONE row per (run_id, scenario_num) so the Consumer post and the
    # Business post for the same scenario merge into a single Scenarios-tab row.
    row = (
        db.query(ScenarioResult)
        .filter_by(run_id=run_id, scenario_num=str(body.scenario_num))
        .first()
    )
    if row is None:
        row = ScenarioResult(
            run_id=run_id, scenario_num=str(body.scenario_num),
            consumer_status="N/A", business_status="N/A", reasons=[],
        )
        db.add(row)
    row.scenario_name = body.scenario_name or row.scenario_name

    role = (body.role or "").strip().lower()
    if role == "consumer":
        row.consumer_status = body.status
    elif role == "business":
        row.business_status = body.status
    else:
        # Android-bridge form: both sides supplied directly.
        if body.consumer_status != "N/A":
            row.consumer_status = body.consumer_status
        if body.business_status != "N/A":
            row.business_status = body.business_status

    # Merge reasons/errors from each side (role-tagged when we know the role).
    merged = list(row.reasons or [])
    for r in (body.reasons or ([body.error] if body.error else [])):
        tagged = f"[{body.role}] {r}" if body.role else r
        if r and tagged not in merged:
            merged.append(tagged)
    row.reasons = merged
    fails = [m for m in merged if "FAIL" in m or "fail" in m]
    row.error = "; ".join(fails) or (body.error if body.status == "FAIL" else row.error)
    if body.launch_time is not None:
        row.launch_time = body.launch_time

    row.status = _both_pass_status(row.consumer_status, row.business_status, body.status)
    db.flush()

    overall = _recompute_run_status(db, run)
    db.commit()

    return {"saved": True, "overall_status": row.status, "run_status": overall}


@runs_router.post("/{run_id}/trigger-android-bot")
def trigger_android_bot_endpoint(
    run_id: str,
    body: TriggerAndroidBotIn,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Kick off the Android cross-app bot for an existing run."""
    run = db.query(TestRun).filter(TestRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return trigger_android_bot(
        db, run, body.scenarios, body.consumer_device, body.business_device, body.bot_url
    )


class CrossAppRunIn(BaseModel):
    consumer_udid: str = "DA24A392-FF1B-4283-A5CE-CDDE0D000D21"
    business_udid: str = "D19D3EC7-5494-4B69-AC7B-3AB8AE0B4D1B"


@runs_router.post("/cross-app")
def run_cross_app_suite(
    body: CrossAppRunIn,
    current_user=Depends(get_current_user),
):
    """Run the FULL Consumer + Business scenario across BOTH iOS simulators at
    once. Returns a run_id immediately; results stream into the Scenarios tab as
    each phase completes (bot_type=ios-crossapp)."""
    from automation.scenarios.cross_app_orchestrator import start_cross_app_run

    run_id = start_cross_app_run(body.consumer_udid, body.business_udid)
    return {
        "started": True,
        "run_id": run_id,
        "message": "Cross-app run started on both simulators. "
                   "Open the run's Scenarios tab to watch it.",
    }


@runs_router.get("/{run_id}/scenarios")
def get_run_scenarios(
    run_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Every scenario result for a run + a Consumer/Business breakdown."""
    run = db.query(TestRun).filter(TestRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    rows = (
        db.query(ScenarioResult)
        .filter(ScenarioResult.run_id == run_id)
        .all()
    )
    # Sort numerically when scenario_num is numeric, else lexically.
    def _key(r):
        n = re.sub(r"\D", "", r.scenario_num or "")
        return (int(n) if n else 1_000_000, r.scenario_num or "")
    rows.sort(key=_key)

    scenarios = [{
        "id": r.id,
        "scenario_num": r.scenario_num,
        "scenario_name": r.scenario_name,
        "status": r.status,
        "consumer_status": r.consumer_status,
        "business_status": r.business_status,
        "error": r.error,
        "reasons": r.reasons or [],
        "launch_time": r.launch_time,
        "created_at": utc_iso(r.created_at),
    } for r in rows]

    return {
        "run_id": run_id,
        "bot_type": run.bot_type,
        "total": len(rows),
        "passed": sum(1 for r in rows if r.status == "PASS"),
        "failed": sum(1 for r in rows if r.status == "FAIL"),
        "consumer_passed": sum(1 for r in rows if r.consumer_status == "PASS"),
        "consumer_failed": sum(1 for r in rows if r.consumer_status == "FAIL"),
        "business_passed": sum(1 for r in rows if r.business_status == "PASS"),
        "business_failed": sum(1 for r in rows if r.business_status == "FAIL"),
        "rule": "Both Consumer AND Business must pass",
        "scenarios": scenarios,
    }

# A job in any of these states has already been attempted or is in flight —
# it must NEVER be handed back out to an agent.
UNAVAILABLE_JOB_STATES = {
    "assigned", "downloading", "preparing", "running",
    "collecting_evidence", "completed", "failed", "cancelled", "stopped", "passed",
}

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
    """Agent asks for the next queued job it can run.

    A job is only ever handed out once: it must be strictly ``job_state ==
    "queued"``, and it is flipped to ``"assigned"`` and committed BEFORE the
    response is returned, so a second agent polling concurrently can no longer
    see it. Anything already assigned/running/completed/failed/cancelled is
    never returned.
    """
    # Strictly queued jobs only, FIFO by created_at.
    queued_jobs = (
        db.query(TestRun)
        .filter(TestRun.job_state == "queued")
        .order_by(TestRun.created_at.asc())
        .all()
    )

    for job in queued_jobs:
        # Defensive re-check: never hand out a job that has already been attempted.
        if job.job_state in UNAVAILABLE_JOB_STATES:
            continue

        # If this job asks for a device that this agent has
        if job.device_name in req.connected_devices:
            # Claim the job atomically: mark assigned and COMMIT before returning
            # so no other agent can pick up the same job id.
            claimed = (
                db.query(TestRun)
                .filter(TestRun.id == job.id, TestRun.job_state == "queued")
                .update(
                    {
                        "job_state": "assigned",
                        "agent_id": req.agent_id,
                        "started_at": datetime.utcnow(),
                    },
                    synchronize_session=False,
                )
            )
            if not claimed:
                # Another agent won the race for this job — try the next one.
                continue
            db.commit()

            project = db.query(TestProject).filter(TestProject.id == job.project_id).first()

            return {
                "job_id": job.id,
                "project_id": job.project_id,
                "git_url": project.git_url if project else "",
                "branch": project.default_branch if project else "main",
                "device_id": job.device_name,
                # The agent needs these to build the right artifact — without a
                # platform it cannot know whether to run xcodebuild or gradlew.
                "platform": (project.platform if project else None) or "ios",
                "project_name": project.name if project else job.test_suite,
                # Freshly claimed by this agent. Sent so the agent can assert it
                # was never previously attempted before it starts executing.
                "job_state": "assigned",
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
