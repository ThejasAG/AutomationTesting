"""FastAPI REST API for querying test runs and RCA reports"""

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
from automation.appium_service.router import router as appium_router
from automation.api.v1.routers.intelligence import router as intelligence_router
from automation.api.v1.routers.agents import router as agents_router
from automation.api.v1.routers.jobs import router as jobs_router
from automation.api.v1.routers.ops import router as ops_router
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

    # Validate JWT before accepting the WebSocket handshake.
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
v1_router.include_router(intelligence_router)
v1_router.include_router(agents_router)
v1_router.include_router(jobs_router)
v1_router.include_router(ops_router)

# Mount external routers
app.include_router(appium_router)


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
    """Get RCA report for a run"""
    rca = database.get_rca_report(db, run_id)
    if not rca:
        raise HTTPException(status_code=404, detail="RCA report not found")
    return {"rca": rca}


@v1_router.get("/runs/{run_id}/evidence", dependencies=[Depends(get_current_user)])
def get_evidence(run_id: str, db: Session = Depends(get_db)):
    """Get Evidence bundle for a run"""
    evidence = database.get_evidence(db, run_id)
    if not evidence:
        raise HTTPException(status_code=404, detail="Evidence not found")
    return {"evidence": evidence}


@v1_router.get("/trends", dependencies=[Depends(get_current_user)])
def get_trends(days: int = 30, db: Session = Depends(get_db)):
    """Get failure trends"""
    return database.get_trends(db, days)


@v1_router.post("/runs/{run_id}/analyze")
def trigger_analysis(run_id: str, background_tasks: BackgroundTasks):
    """Trigger RCA analysis for a failed run"""
    return {"status": "analysis_queued"}


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


@app.on_event("shutdown")
async def shutdown_event():
    ops_monitor.stop()
