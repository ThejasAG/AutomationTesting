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
from automation.auth.security import require_agent
from automation.database.config import get_db
from automation.reports.step_stats import step_stats
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

    # Real-time failure alert — fire the moment a scenario fails, not at run end.
    try:
        from automation.notifications.realtime_alerts import realtime_alert_service
        realtime_alert_service.check_and_alert(run_id, {
            "status": row.status,
            "scenario_name": row.scenario_name or f"#{body.scenario_num}",
            "error": row.error,
        })
    except Exception as _e:
        logger.warning(f"realtime alert (scenario-result) failed: {_e}")

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


class RoleCreds(BaseModel):
    email: Optional[str] = None
    password: Optional[str] = None


class CrossAppRunIn(BaseModel):
    # Which simulator plays each role. Waiter == kitchen device means the run
    # switches accounts on one device; different devices means two instances.
    devices: Optional[Dict[str, str]] = None       # {consumer, waiter, kitchen}
    credentials: Optional[Dict[str, RoleCreds]] = None
    save: bool = True                              # persist devices/creds for next time


@runs_router.get("/cross-app/config")
def get_cross_app_config(current_user=Depends(get_current_user)):
    """Simulators + saved device assignments + saved emails (passwords masked)."""
    from automation.scenarios.cross_app_config import public_config
    return public_config()


@runs_router.post("/cross-app")
def run_cross_app_suite(
    body: CrossAppRunIn,
    current_user=Depends(get_current_user),
):
    """Run the FULL Consumer + Business scenario across the chosen iOS simulators.

    Accepts per-role device assignments and credentials; saves them for reuse
    when save=True. Returns a run_id immediately; results stream into the
    Scenarios tab as each phase completes (bot_type=ios-crossapp).
    """
    from automation.scenarios import cross_app_config as cfgmod
    from automation.scenarios.cross_app_orchestrator import start_cross_app_run

    creds_in = None
    if body.credentials:
        creds_in = {r: c.dict() for r, c in body.credentials.items()}

    if body.save:
        cfgmod.apply_update(body.devices, creds_in)
        cfg = cfgmod.load_config()
    else:
        cfg = cfgmod.load_config()
        if body.devices:
            cfg["devices"].update({k: v for k, v in body.devices.items() if v})

    run_id = start_cross_app_run(
        consumer_udid=cfg["devices"]["consumer"],
        waiter_udid=cfg["devices"]["waiter"],
        kitchen_udid=cfg["devices"]["kitchen"],
        credentials=cfg["credentials"],
    )
    return {
        "started": True,
        "run_id": run_id,
        "devices": cfg["devices"],
        "message": "Cross-app run started. Open the run's Scenarios tab to watch it.",
    }


@runs_router.get("/cross-app-flows")
def list_cross_app_flows(current_user=Depends(get_current_user)):
    """The four major end-to-end cross-app flows (id, name, segments, steps)."""
    from automation.scenarios.cross_app_flows import list_flows
    return {"flows": list_flows()}


class FlowRunIn(BaseModel):
    flow_id: str
    env: str = "prod"                     # "prod" (old Vya) | "staging" (STG-* apps)
    business_device: str = "tablet"       # "tablet" (iPad) | "phone" (iPhone 16 Pro)


@runs_router.post("/cross-app-flow")
def run_cross_app_flow(body: FlowRunIn, current_user=Depends(get_current_user)):
    """Run one cross-app flow (consumer → waiter → kitchen → waiter) against the
    chosen environment. env='staging' drives the STG-* apps on vya.xorstack.com;
    env='prod' drives the old live Vya apps. business_device picks which device
    runs the B-app roles (waiter+kitchen): 'tablet' (iPad) or 'phone' (iPhone).
    Returns a run_id."""
    from automation.scenarios.cross_app_flows import start_flow_run
    try:
        run_id = start_flow_run(body.flow_id, env=body.env,
                                business_device=body.business_device)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"started": True, "run_id": run_id, "flow_id": body.flow_id, "env": body.env,
            "business_device": body.business_device,
            "message": "Flow started. Open the run's Scenarios tab / rich report to watch it."}


# ── Cross-app flow EDITING ───────────────────────────────────────────────────
# Built-ins live in code; an edit is stored as a row keyed by flow id and wins at run
# time. Deleting the row reverts to the built-in, so editing can't destroy a working
# flow permanently.
class FlowSegmentIn(BaseModel):
    num: str = "1"
    name: str = ""
    role: str = "consumer"                # consumer | waiter | kitchen
    steps: List[str] = []


class FlowEditIn(BaseModel):
    name: str
    description: str = ""
    segments: List[FlowSegmentIn]


@runs_router.get("/cross-app-flows/step-catalog")
def cross_app_step_catalog(current_user=Depends(get_current_user)):
    """Step tokens the runner understands, for the editor's picker. Free text is still
    accepted (it falls through to the fuzzy click/type resolver) — this is a menu."""
    from automation.scenarios.cross_app_flows import STEP_CATALOG
    return {"catalog": STEP_CATALOG, "roles": ["consumer", "waiter", "kitchen"]}


def _validate_segments(segments: List[FlowSegmentIn]) -> None:
    if not segments:
        raise HTTPException(400, "A flow needs at least one segment.")
    for i, s in enumerate(segments, 1):
        if s.role not in ("consumer", "waiter", "kitchen"):
            raise HTTPException(400, f"Segment {i}: role must be consumer, waiter or kitchen.")
        if not [x for x in s.steps if x.strip()]:
            raise HTTPException(400, f"Segment {i} ('{s.name or s.role}') has no steps.")


@runs_router.put("/cross-app-flows/{flow_id}")
def save_cross_app_flow(flow_id: str, body: FlowEditIn,
                        db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Create or update a flow. Editing a built-in id overrides it for future runs."""
    from automation.database.models import CrossAppFlowEdit
    from automation.scenarios.cross_app_flows import FLOWS

    _validate_segments(body.segments)
    segs = [{"num": s.num or str(i + 1), "name": s.name, "role": s.role,
             "steps": [x.strip() for x in s.steps if x.strip()]}
            for i, s in enumerate(body.segments)]

    row = db.query(CrossAppFlowEdit).filter(CrossAppFlowEdit.id == flow_id).first()
    if row:
        row.name, row.description, row.segments = body.name, body.description, segs
    else:
        row = CrossAppFlowEdit(id=flow_id, name=body.name, description=body.description,
                               segments=segs,
                               based_on=flow_id if flow_id in FLOWS else None)
        db.add(row)
    db.commit()
    return {"saved": True, "flow_id": flow_id, "overrides_builtin": flow_id in FLOWS}


@runs_router.delete("/cross-app-flows/{flow_id}", status_code=204)
def delete_cross_app_flow(flow_id: str, db: Session = Depends(get_db),
                          current_user=Depends(get_current_user)):
    """Delete a custom flow, or revert an edited built-in to its shipped definition."""
    from automation.database.models import CrossAppFlowEdit
    row = db.query(CrossAppFlowEdit).filter(CrossAppFlowEdit.id == flow_id).first()
    if not row:
        raise HTTPException(404, f"No stored edit for '{flow_id}'")
    db.delete(row)
    db.commit()
    return None


class FlowRunAllIn(BaseModel):
    env: str = "prod"


@runs_router.post("/cross-app-flows/run-all")
def run_all_cross_app_flows(body: FlowRunAllIn, current_user=Depends(get_current_user)):
    """Run ALL cross-app flows sequentially against the chosen environment.
    Returns the list of run_ids (one per flow), kicked off back-to-back."""
    from automation.scenarios.cross_app_flows import FLOWS, start_flow_run
    import threading, time as _t

    def _sequential(flow_ids, env):
        from automation.database.config import SessionLocal
        from automation.database.models import TestRun
        for fid in flow_ids:
            rid = start_flow_run(fid, env=env)
            # wait for this run to finish before starting the next (shared sims)
            for _ in range(600):                      # up to ~50 min per flow
                with SessionLocal() as db:
                    r = db.query(TestRun).filter_by(id=rid).first()
                    if r and r.status in ("passed", "failed"):
                        break
                _t.sleep(5)

    flow_ids = list(FLOWS.keys())
    threading.Thread(target=_sequential, args=(flow_ids, body.env), daemon=True).start()
    return {"started": True, "env": body.env, "flows": flow_ids,
            "message": f"Running all {len(flow_ids)} flows on {body.env} sequentially."}


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

    _stats = {r.id: step_stats(r.reasons) for r in rows}
    scenarios = [{
        "id": r.id,
        "scenario_num": r.scenario_num,
        "scenario_name": r.scenario_name,
        "status": r.status,
        "consumer_status": r.consumer_status,
        "business_status": r.business_status,
        "error": r.error,
        "reasons": r.reasons or [],
        # Step-level accounting, derived from `reasons` (which already tags every
        # step [ok]/[FAIL]). "failed at step 7 of 8" and "failed at step 1" both
        # showed as just "FAIL" before this. None pct = no countable steps ran,
        # which is NOT the same as 0%.
        "steps_passed": _stats[r.id].passed,
        "steps_failed": _stats[r.id].failed,
        "steps_total": _stats[r.id].total,
        "steps_pass_pct": _stats[r.id].pass_pct,
        "steps_summary": _stats[r.id].summary(),
        "launch_time": r.launch_time,
        "screenshot": r.screenshot,          # failure screenshot (data-URI) for the Scenarios tab
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

@runs_router.get("/{run_id}/visual-regression")
def get_visual_regression(
    run_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Visual-regression results for a run, with baseline/current/diff images as base64."""
    import base64
    from automation.database.models import VisualRegressionResult

    rows = (
        db.query(VisualRegressionResult)
        .filter(VisualRegressionResult.run_id == run_id)
        .order_by(VisualRegressionResult.diff_percentage.desc())
        .all()
    )

    def _b64(path: Optional[str]) -> Optional[str]:
        try:
            if path and os.path.exists(path):
                with open(path, "rb") as f:
                    return "data:image/png;base64," + base64.b64encode(f.read()).decode()
        except Exception:
            pass
        return None

    results = [{
        "id": r.id,
        "screen_name": r.screen_name,
        "diff_percentage": r.diff_percentage,
        "severity": r.severity,
        "passed": r.passed,
        "baseline_image": _b64(r.baseline_path),
        "current_image": _b64(r.current_path),
        "diff_image": _b64(r.diff_path),
        "created_at": utc_iso(r.created_at),
    } for r in rows]

    return {
        "run_id": run_id,
        "total": len(results),
        "regressions": sum(1 for r in rows if not r.passed),
        "results": results,
    }


class UpdateBaselineIn(BaseModel):
    project_id: Optional[str] = None


@runs_router.post("/{run_id}/visual-regression/update-baseline")
def update_visual_baseline(
    run_id: str,
    body: UpdateBaselineIn = UpdateBaselineIn(),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Accept this run's screenshots as the new baseline (allowed on passed runs)."""
    from automation.intelligence.visual_regression import visual_regression_analyzer
    run = db.query(TestRun).filter(TestRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if (run.status or "").lower() != "passed":
        raise HTTPException(status_code=400, detail="Baseline can only be updated from a passed run")
    result = visual_regression_analyzer.update_baseline(run.project_id, run_id)
    return {"updated": True, **result}


@runs_router.get("/{run_id}/risk-predictions")
def get_risk_predictions(
    run_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Per-test failure-risk predictions for this run's project."""
    from automation.intelligence.impact_predictor import test_impact_predictor
    run = db.query(TestRun).filter(TestRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    test_names = [
        n[0] for n in db.query(TestRun.test_name)
        .filter(TestRun.project_id == run.project_id, TestRun.test_name.isnot(None))
        .distinct().all()
    ]
    if not test_names and run.test_name:
        test_names = [run.test_name]
    changed = [f.strip() for f in (run.commit_sha and [] or [])]  # no diff at read time
    predictions = test_impact_predictor.predict_failure_risk(test_names, run.project_id, changed)
    return {"run_id": run_id, "project_id": run.project_id, "predictions": predictions}


class CreateBotRunIn(BaseModel):
    selection: str = "all"
    bot_type: str = "android"
    device_name: str = "vya-bot"
    platform: str = "Android"


@runs_router.post("/vya-bot")
def create_vya_bot_run(
    body: CreateBotRunIn,
    request: Request,
    db: Session = Depends(get_db),
    x_bot_secret: Optional[str] = Header(default=None),
):
    """Create a run for the Vya bot and return its id + scenario-result callback.

    Lets the bot (running on any machine) self-register a run over HTTP, so it
    does not need the platform's local DB/venv. Auth mirrors scenario-result:
    X-Bot-Secret is required only when BOT_SECRET is set on the platform.
    """
    import uuid as _uuid
    expected = os.getenv("BOT_SECRET", "")
    if expected and x_bot_secret != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Bot-Secret")

    run_id = str(_uuid.uuid4())
    now = datetime.utcnow()
    db.add(TestRun(
        id=run_id,
        test_suite="Vya-agentic-BOT (cross-app)",
        test_name=f"Scenarios: {body.selection}",
        status="running", job_state="running",
        started_at=now, created_at=now,
        device_name=body.device_name, platform=body.platform,
        bot_type=body.bot_type, triggered_by="vya-bot",
    ))
    db.commit()

    base = str(request.base_url).rstrip("/")
    return {
        "run_id": run_id,
        "callback_url": f"{base}/api/v1/runs/{run_id}/scenario-result",
        "dashboard_url": f"http://localhost:5173/run/{run_id}",
    }


@runs_router.get("/{run_id}/performance")
def get_run_performance(
    run_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Performance summary + time-series + API breakdown + vs-previous comparison."""
    from automation.database.models import PerformanceSummary, PerformanceMetric

    ps = db.query(PerformanceSummary).filter(PerformanceSummary.run_id == run_id).first()
    if not ps:
        raise HTTPException(status_code=404, detail="No performance data for this run")

    summary = {c.name: getattr(ps, c.name) for c in ps.__table__.columns}

    metrics = (
        db.query(PerformanceMetric)
        .filter(PerformanceMetric.run_id == run_id)
        .order_by(PerformanceMetric.timestamp.asc())
        .all()
    )
    metrics_over_time = [{
        "timestamp": utc_iso(m.timestamp),
        "cpu": m.cpu_percent, "memory": m.memory_mb, "fps": m.fps,
    } for m in metrics]

    # Rebuild the API breakdown from stored issues is lossy; expose what we have.
    api_calls = {
        "total_calls": ps.api_calls,
        "avg_ms": ps.avg_api_response_ms,
        "slowest": {"url": ps.slowest_api_endpoint, "ms": ps.slowest_api_ms},
    }

    # Compare with the previous run of the same project.
    comparison = None
    run = db.query(TestRun).filter(TestRun.id == run_id).first()
    if run is not None and run.project_id:
        prev = (
            db.query(PerformanceSummary)
            .join(TestRun, TestRun.id == PerformanceSummary.run_id)
            .filter(TestRun.project_id == run.project_id,
                    PerformanceSummary.run_id != run_id,
                    PerformanceSummary.created_at < ps.created_at)
            .order_by(PerformanceSummary.created_at.desc())
            .first()
        )
        if prev is not None:
            def _delta(a, b):
                if a is None or b is None:
                    return None
                return round(a - b, 2)
            comparison = {
                "vs_previous_run": {
                    "prev_run_id": prev.run_id,
                    "prev_score": prev.performance_score,
                    "launch_time_change": _delta(ps.app_launch_time_s, prev.app_launch_time_s),
                    "cpu_change": _delta(ps.avg_cpu_percent, prev.avg_cpu_percent),
                    "score_change": _delta(ps.performance_score, prev.performance_score),
                    "better": (ps.performance_score or 0) >= (prev.performance_score or 0),
                }
            }

    return {
        "summary": summary,
        "grade": ps.grade,
        "score": ps.performance_score,
        "issues": ps.issues or [],
        "metrics_over_time": metrics_over_time,
        "api_calls": api_calls,
        "comparison": comparison,
    }


@runs_router.get("/{run_id}/summary")
def get_run_summary(
    run_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """AI-generated, PM-friendly summary of a run (cached on the run)."""
    from automation.ai.services.summary import test_summary_generator
    run = db.query(TestRun).filter(TestRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return test_summary_generator.generate_run_summary(run_id, db)


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
    logs: List[str] = []
    timeline_event: Optional[str] = None
    error_message: Optional[str] = None
    attempts: Optional[int] = None
    flaky_detected: Optional[bool] = None

class EvidenceUploadRequest(BaseModel):
    evidence: Dict[str, Any]

class StreamRequest(BaseModel):
    logs: str
    screen_state: str

@router.post("/poll")
def poll_job(req: PollJobRequest, db: Session = Depends(get_db),
              _agent=Depends(require_agent)):
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
                # THE JOB'S branch, not the project default. A PR test stores the PR's
                # branch on the run, but this used to hand the agent default_branch — so
                # "Test merge" cloned and built `main` and tested code that had nothing to
                # do with the pull request, while reporting a verdict against the PR.
                # Fall back to the project default only when the job names no branch.
                "branch": (job.branch
                           or (project.default_branch if project else None)
                           or "main"),
                "device_id": job.device_name,
                # The agent needs these to build the right artifact — without a
                # platform it cannot know whether to run xcodebuild or gradlew.
                "platform": (project.platform if project else None) or "ios",
                "project_name": project.name if project else job.test_suite,
                # The scenarios the planner chose for THIS change. Empty/absent means the
                # agent falls back to the project's execution.command.
                "planned_scenarios": job.planned_scenarios or [],
                # PR jobs get the clean-install treatment; scheduled/manual runs on a
                # prepared device do not, because wiping signs the app out.
                "is_pr": str(job.triggered_by or "").startswith("pr_test:"),
                # Freshly claimed by this agent. Sent so the agent can assert it
                # was never previously attempted before it starts executing.
                "job_state": "assigned",
            }

    # No jobs found for this agent
    return {"job_id": None}

@router.post("/{job_id}/status")
def update_job_status(job_id: str, req: JobStatusRequest, db: Session = Depends(get_db),
                      _agent=Depends(require_agent)):
    """Agent updates the status, logs, and timeline of a running job."""
    job = db.query(TestRun).filter(TestRun.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
        
    job.job_state = req.status
    # "no_tests" is TERMINAL but deliberately not "passed": the app deployed and nothing
    # was verified. It used to be reported as passed, so a PR with no runnable tests
    # looked exactly like one whose tests all passed.
    if req.status in ["passed", "failed", "stopped", "no_tests"]:
        job.status = req.status
        job.completed_at = datetime.utcnow()
        if job.started_at:
            job.duration_ms = int((job.completed_at - job.started_at).total_seconds() * 1000)
    elif req.status == "running":
        job.status = "running"
        
    if req.error_message:
        job.error_message = req.error_message

    if req.attempts is not None:
        job.attempts = req.attempts
    if req.flaky_detected is not None:
        job.flaky_detected = req.flaky_detected
        if req.flaky_detected:
            job.is_flaky = True

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
def stream_logs(job_id: str, req: StreamRequest, db: Session = Depends(get_db),
                _agent=Depends(require_agent)):
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
def upload_evidence(job_id: str, req: EvidenceUploadRequest, db: Session = Depends(get_db),
                    _agent=Depends(require_agent)):
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
