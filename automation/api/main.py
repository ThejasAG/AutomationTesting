"""FastAPI REST API for querying test runs and RCA reports"""

# Load .env BEFORE any automation.* import. Several modules (e.g. ai/provider.py)
# read os.getenv at import time, so loading later would leave them holding the
# defaults and the .env values would be silently ignored.
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from fastapi import (
    FastAPI,
    HTTPException,
    BackgroundTasks,
    Depends,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from typing import Optional
import asyncio
import logging
import os
import hmac

from automation.database import database
from automation.database.config import get_db, initialize_database
from automation.auth.security import get_current_user, SECRET_KEY, ALGORITHM
from automation.ops.monitor import ops_monitor
from automation.api.v1.routers.automation import router as automation_router
from automation.api.v1.routers.analytics import router as analytics_router
from automation.api.v1.routers.auth import router as auth_router
from automation.api.v1.routers.projects import router as projects_router
from automation.api.v1.routers.groups import router as groups_router
from automation.api.v1.routers.dependency import router as dependency_router
from automation.api.v1.routers.pull_requests import router as pull_requests_router
from automation.api.v1.routers.scenario import router as scenario_router
from automation.api.v1.routers.scenarios import router as scenarios_router
from automation.api.v1.routers.recorder import router as recorder_router
from automation.api.v1.routers.reports import router as reports_router
from automation.api.v1.routers.pr_poller_api import router as pr_poller_router
from automation.appium_service.router import router as appium_router
from automation.api.v1.routers.intelligence import router as intelligence_router
from automation.api.v1.routers.agents import router as agents_router
from automation.api.v1.routers.jobs import router as jobs_router, runs_router
from automation.api.v1.routers.ops import router as ops_router
from automation.api.v1.routers import webhooks
from automation.utils.security import install_secret_filter
from automation.streaming.ws_manager import stream_manager
from fastapi import APIRouter
from jose import jwt as jose_jwt, JWTError

app = FastAPI(
    title="AI-Powered Mobile Test Orchestration Platform",
    version="1.0.0-rc",
    description="Enterprise-grade mobile test orchestration with AI-powered analysis.",
)

# Setup CORS for dashboard
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Global Structured Error Handler ────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger = logging.getLogger("api")
    logger.exception(f"Unhandled error on {request.url.path}: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "problem": "An unexpected server error occurred.",
                "cause": type(exc).__name__,
                "impact": "The requested operation could not be completed.",
                "resolution": "Check the server logs for the full stack trace and contact the platform administrator.",
                "doc_link": "https://github.com/your-org/automation-platform/docs/troubleshooting",
                "log_reference": str(request.url.path),
            }
        },
    )


# ── WebSocket: Live Emulator Stream ─────────────────────────────────────────
@app.websocket("/ws/stream/{run_id}")
async def stream_run(websocket: WebSocket, run_id: str):
    """Real-time emulator screen stream.

    Browsers connect with: ws://host/ws/stream/{run_id}?token=<jwt>

    The JWT is validated server-side.  Binary WebSocket messages carry raw
    PNG bytes.  A final JSON message ``{"type": "ended"}`` signals that the
    agent has stopped streaming.
    """
    token = websocket.query_params.get("token", "")

    # Debug/test streams (run_id prefixed "test-"/"debug-") bypass JWT so the
    # standalone stream-test page and the /debug/stream-test endpoint can be
    # viewed without a dashboard login. Real run streams still require a token.
    is_debug_stream = run_id.startswith(("test-", "debug-"))

    # Validate JWT before accepting the WebSocket handshake.
    if not is_debug_stream:
        try:
            if not token:
                raise JWTError("missing token")
            jose_jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        except JWTError:
            await websocket.close(code=4001)
            return

    await websocket.accept()

    logger = logging.getLogger("ws_stream")
    logger.info("Browser connected to stream for run %s", run_id)

    try:
        while True:
            if stream_manager.is_ended(run_id):
                try:
                    await websocket.send_json({"type": "ended"})
                except Exception:
                    pass
                break

            frame = stream_manager.get_frame(run_id)
            if frame:
                await websocket.send_bytes(frame)

            # ~4 FPS poll rate — gives the browser time to render each frame.
            await asyncio.sleep(0.25)

    except WebSocketDisconnect:
        logger.info("Browser disconnected from stream %s", run_id)
    except Exception as err:
        logger.warning("WebSocket stream error for %s: %s", run_id, err)
    finally:
        stream_manager.cleanup(run_id)
        logger.info("Stream resources cleaned up for run %s", run_id)


# ── API v1 Router ────────────────────────────────────────────────────────────
v1_router = APIRouter(prefix="/api/v1")

# Include Routers in v1
v1_router.include_router(auth_router)
v1_router.include_router(automation_router, dependencies=[Depends(get_current_user)])
v1_router.include_router(analytics_router, dependencies=[Depends(get_current_user)])
v1_router.include_router(projects_router, dependencies=[Depends(get_current_user)])
v1_router.include_router(groups_router, dependencies=[Depends(get_current_user)])
v1_router.include_router(dependency_router, dependencies=[Depends(get_current_user)])
v1_router.include_router(pull_requests_router)
v1_router.include_router(scenario_router)
v1_router.include_router(scenarios_router)
v1_router.include_router(recorder_router)
v1_router.include_router(reports_router)
v1_router.include_router(pr_poller_router)
v1_router.include_router(intelligence_router)
v1_router.include_router(agents_router)
v1_router.include_router(jobs_router)
# Mounted WITHOUT a blanket JWT dep: /runs/{id}/scenario-result is called by the
# Android bot (X-Bot-Secret), while the other two routes enforce JWT per-route.
v1_router.include_router(runs_router)
v1_router.include_router(ops_router)

# Mount external routers
app.include_router(appium_router)

# GitHub webhooks — mounted directly (NO JWT dependency; GitHub calls these).
app.include_router(webhooks.router, prefix="/api/v1")


# ── Agent Frame Upload ──────────────────────────────────────────────────────
@v1_router.post("/jobs/{run_id}/stream/frame")
async def upload_stream_frame(run_id: str, request: Request):
    """Receives raw PNG frames (or an empty body end-signal) from the agent.

    Authentication: optional shared secret via ``X-Agent-Secret`` header.
    When the environment variable ``AGENT_SECRET`` is set, the header must
    match exactly.  In dev mode (no env var), the check is skipped.
    """
    agent_secret = os.getenv("AGENT_SECRET", "")
    if agent_secret:
        provided = request.headers.get("X-Agent-Secret", "")
        # Use constant-time comparison to resist timing attacks.
        if not hmac.compare_digest(provided, agent_secret):
            raise HTTPException(status_code=403, detail="Invalid agent secret")

    body: bytes = await request.body()

    if not body:
        # Empty body is the agent's end-of-stream signal.
        stream_manager.mark_ended(run_id)
    else:
        stream_manager.push_frame(run_id, body)

    return {"status": "ok"}


# ── Debug: Standalone Stream Test ───────────────────────────────────────────
@v1_router.post("/debug/stream-test/{run_id}")
async def debug_stream_test(run_id: str):
    """Stream the booted iOS simulator for 30 seconds without running a test job.

    Captures the iPhone 16 Pro simulator screen via ``xcrun simctl`` and pushes
    frames into the stream manager. View live at
    ``ws://localhost:8000/ws/stream/{run_id}`` (use a "test-"/"debug-" prefixed
    run_id to skip the JWT check).
    """
    import threading
    from automation.streaming.screen_capture import IOSScreenCapture

    def _run_capture() -> None:
        stop_event = threading.Event()
        capture = IOSScreenCapture(
            device_id="booted",
            stop_event=stop_event,
            on_frame=lambda png: stream_manager.push_frame(run_id, png),
            fps=4.0,
        )
        cap_thread = threading.Thread(
            target=capture.start, daemon=True, name=f"debug-stream-{run_id[:8]}"
        )
        cap_thread.start()
        # Stream for 30 seconds, then finalise the stream.
        stop_event.wait(timeout=30)
        stop_event.set()
        cap_thread.join(timeout=6)
        stream_manager.mark_ended(run_id)

    threading.Thread(target=_run_capture, daemon=True).start()

    return {
        "status": "streaming",
        "run_id": run_id,
        "websocket": f"ws://localhost:8000/ws/stream/{run_id}",
    }


# ── Standard v1 Endpoints ───────────────────────────────────────────────────
@v1_router.get("/runs", dependencies=[Depends(get_current_user)])
def list_runs(
    suite: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """List test runs with optional filtering"""
    runs = database.get_test_runs(db, limit, suite, status)
    return {"runs": runs, "total": len(runs)}


@v1_router.get("/runs/{run_id}", dependencies=[Depends(get_current_user)])
def get_run(run_id: str, db: Session = Depends(get_db)):
    """Get specific test run details"""
    run = database.get_test_run(db, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"run": run}


@v1_router.get("/runs/{run_id}/rca", dependencies=[Depends(get_current_user)])
def get_rca(run_id: str, db: Session = Depends(get_db)):
    """Get RCA report for a run. Absent RCA is normal (a passing run has none)."""
    return {"rca": database.get_rca_report(db, run_id)}


@v1_router.get("/runs/{run_id}/evidence", dependencies=[Depends(get_current_user)])
def get_evidence(run_id: str, db: Session = Depends(get_db)):
    """Get Evidence bundle for a run. Absent evidence is normal (nothing collected yet)."""
    return {"evidence": database.get_evidence(db, run_id)}


@v1_router.get("/trends", dependencies=[Depends(get_current_user)])
def get_trends(days: int = 30, db: Session = Depends(get_db)):
    """Get failure trends"""
    return database.get_trends(db, days)


@v1_router.post("/runs/{run_id}/analyze", dependencies=[Depends(get_current_user)])
def trigger_analysis(run_id: str, db: Session = Depends(get_db)):
    """Generate an RCA report for a failed run (was a no-op stub).

    Builds evidence from the run's error and its failed scenarios, runs the
    RCAService (local Ollama), persists it, and returns it — so the dashboard's
    'Trigger Analysis' button actually produces a report.
    """
    from automation.database.models import TestRun, ScenarioResult
    from automation.ai.service import RCAService

    run = db.query(TestRun).filter(TestRun.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    existing = database.get_rca_report(db, run_id)
    if existing:
        return {"status": "exists", "rca": existing}

    # Assemble evidence: the run's own error plus every failed scenario's reason.
    scenarios = db.query(ScenarioResult).filter(ScenarioResult.run_id == run_id).all()
    failed = [s for s in scenarios if s.status == "FAIL"]
    evidence = {
        "test_name": run.test_name,
        "suite": run.test_suite,
        "platform": run.platform,
        "error_message": run.error_message or "",
        "failed_scenarios": [
            {
                "scenario": f"{s.scenario_num} {s.scenario_name}",
                "consumer": s.consumer_status,
                "business": s.business_status,
                "reason": s.error or "; ".join(s.reasons or []),
            }
            for s in failed
        ],
        "total_scenarios": len(scenarios),
        "failed_count": len(failed),
    }

    try:
        rca = RCAService().analyze(evidence)
    except Exception as e:
        logging.getLogger("api").exception("RCA generation failed")
        raise HTTPException(status_code=502, detail=f"RCA generation failed: {e}")

    from automation.ai.service import default_rca_config
    from datetime import datetime as _dt
    _cfg = default_rca_config()

    def _text(v):
        # Models sometimes return a list of bullet strings for a text field; the
        # column is TEXT, so join them. Leaves plain strings/None untouched.
        if isinstance(v, (list, tuple)):
            return "\n".join(str(x) for x in v)
        return v if v is None else str(v)

    rca_data = {
        "run_id": run_id,
        "root_cause": _text(rca.root_cause),
        "failure_category": _text(rca.failure_category),
        "affected_modules": rca.affected_modules if isinstance(rca.affected_modules, list) else [],
        "confidence": rca.confidence,
        "possible_reason": _text(rca.possible_reason),
        "impact": _text(rca.impact),
        "suggested_fix": _text(rca.suggested_fix),
        "priority": _text(rca.priority),
        "severity": _text(rca.severity),
        "responsible_module": _text(rca.responsible_module),
        "summary": _text(rca.summary),
        "llm_provider": _cfg.get("provider", "ollama"),
        "llm_model": _cfg.get("model", "llama3.2"),
        "generated_at": _dt.utcnow(),
    }
    database.insert_rca_report(db, rca_data)
    return {"status": "generated", "rca": database.get_rca_report(db, run_id)}


@v1_router.get("/health")
def health_check():
    """Health check endpoint"""
    return {"status": "ok"}


# Mount v1 router at the very end so all routes are included
app.include_router(v1_router)


@app.on_event("startup")
async def startup_event():
    install_secret_filter()  # Redact tokens/keys from all logs
    initialize_database()
    ops_monitor.start()
    from automation.ci_cd import pr_poller
    pr_poller.start()  # auto-queue runs for new PR commits (PR_POLL_ENABLED)


@app.on_event("shutdown")
async def shutdown_event():
    ops_monitor.stop()
