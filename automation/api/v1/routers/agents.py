import os
import pathlib
import signal
import subprocess

from fastapi import APIRouter, Depends, HTTPException, status
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from sqlalchemy.orm import Session
import time
from datetime import datetime

from automation.database.config import get_db
from automation.database.models import ExecutionAgent, TestRun
from automation.auth.security import (get_current_user, require_agent,
                                      authenticated_agent, assert_agent_identity,
                                      issue_agent_credential, require_authenticated_agent)

router = APIRouter(prefix="/agents", tags=["Agents"])

class RegisterAgentRequest(BaseModel):
    hostname: str
    os: str
    capabilities: Dict[str, Any]
    connected_devices: List[Dict[str, Any]]

class HeartbeatRequest(BaseModel):
    status: str
    connected_devices: List[Dict[str, Any]]

@router.post("/register")
def register_agent(req: RegisterAgentRequest, db: Session = Depends(get_db),
                   _agent=Depends(require_agent)):
    # Re-registering the SAME machine reuses its row instead of inserting a new one.
    #
    # Every restart used to mint a fresh ExecutionAgent with a fresh uuid: this
    # database holds 39 agent rows for 4 machines, 32 of them one Mac restarting.
    # That made execution_agents.id unusable as a machine identity — the devices
    # table keys on it, so a per-restart id would create a new set of device rows
    # every time the agent came back. Keyed on (hostname, os): the same physical
    # machine keeps one stable id across restarts.
    #
    # Existing rows are never deleted or merged; the newest row for a hostname is
    # adopted and the older ones are left exactly as they are, still referenced by
    # the historic test_runs that point at them.
    # Matched on NORMALIZED hostname + os, so the agent's "Admins-MacBook-Pro.local"
    # and the backend's "Admins-MacBook-Pro" resolve to the same machine — the two
    # spellings that made a co-located backend and agent look like two Macs.
    from automation.device_manager.service import _find_machine

    agent = _find_machine(db, req.hostname, req.os)
    if agent is None:
        agent = ExecutionAgent(hostname=req.hostname, os=req.os)
        db.add(agent)
    # A real agent now polls for this machine, so the row is no longer
    # backend-only — whether the backend created it or an agent did.
    agent.is_backend = False
    # Issue this machine a credential of its own. AGENT_TOKEN proved the caller
    # may register; this proves WHICH machine it is on every later request, so no
    # agent can act as another by naming a different id.
    credential, credential_hash = issue_agent_credential()
    agent.agent_credential_hash = credential_hash
    agent.agent_credential_issued_at = datetime.utcnow()
    agent.status = "online"
    agent.capabilities = req.capabilities
    agent.connected_devices = req.connected_devices
    agent.last_heartbeat = datetime.utcnow()
    db.commit()
    db.refresh(agent)

    # Sync devices into the in-memory cache immediately on registration so the
    # device is available right away — not only after the first heartbeat (10s).
    from automation.device_manager.service import device_service
    device_service.sync_agent_devices(agent.id, req.connected_devices, agent.hostname, db=db)

    # The secret is returned exactly once — only its hash is stored.
    return {"id": agent.id, "status": "registered", "agent_credential": credential}

@router.post("/{agent_id}/heartbeat")
def heartbeat(agent_id: str, req: HeartbeatRequest, db: Session = Depends(get_db),
              _agent=Depends(require_agent),
              authenticated=Depends(authenticated_agent)):
    # The agent id is still a path parameter for compatibility, but a credentialed
    # caller may only heartbeat as itself.
    assert_agent_identity(authenticated, agent_id)
    agent = db.query(ExecutionAgent).filter(ExecutionAgent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
        
    agent.status = req.status
    agent.connected_devices = req.connected_devices
    agent.last_heartbeat = datetime.utcnow()
    
    db.commit()
    
    # Also forward devices to DeviceDiscoveryService so UI sees them
    from automation.device_manager.service import device_service
    device_service.sync_agent_devices(agent_id, req.connected_devices, agent.hostname, db=db)
    
    return {"status": "ok"}

@router.get("")
def list_agents(db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    agents = db.query(ExecutionAgent).order_by(ExecutionAgent.created_at.desc()).all()
    
    result = []
    for a in agents:
        result.append({
            "id": a.id,
            "hostname": a.hostname,
            "os": a.os,
            "status": a.status,
            "last_heartbeat": a.last_heartbeat.isoformat() if a.last_heartbeat else None,
            "capabilities": a.capabilities,
            "connected_devices": a.connected_devices
        })
    return result


# ── Runner control: start/stop the execution agent process from the dashboard ──
#
# Without this the agent is only startable from a terminal, so queued jobs sat
# for days with nobody noticing that nothing was draining them. The shell
# scripts stay the single source of truth for HOW the agent starts (node on
# PATH, backend health check, pid file, registration wait) — these endpoints
# just run them.

# routers -> v1 -> api -> automation -> repo root
_PROJECT_ROOT = str(pathlib.Path(__file__).resolve().parents[4])
_AGENT_PID_FILE = os.path.join(_PROJECT_ROOT, "logs", "agent.pid")
_AGENT_LOG = os.path.join(_PROJECT_ROOT, "logs", "agent.log")


def _agent_pid() -> int | None:
    """Pid of the running agent from its pid file, or None if it is not alive."""
    try:
        with open(_AGENT_PID_FILE) as fh:
            pid = int(fh.read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None
    try:
        os.kill(pid, 0)          # signal 0 = liveness probe, sends nothing
        return pid
    except OSError:
        return None


def _runner_state() -> Dict[str, Any]:
    pid = _agent_pid()
    tail = ""
    try:
        with open(_AGENT_LOG, "rb") as fh:            # last line, for the UI
            fh.seek(0, os.SEEK_END)
            back = min(fh.tell(), 4096)
            fh.seek(-back, os.SEEK_END)
            lines = [l for l in fh.read().decode("utf-8", "replace").splitlines() if l.strip()]
            tail = lines[-1] if lines else ""
    except OSError:
        pass
    return {"running": pid is not None, "pid": pid, "last_log": tail}


@router.get("/runner")
def runner_status(current_user=Depends(get_current_user)):
    """Is the execution agent process up?"""
    return _runner_state()


@router.post("/runner/start")
def runner_start(current_user=Depends(get_current_user)):
    """Start the execution agent. Idempotent — start-agent.sh no-ops if it is up."""
    if _agent_pid() is not None:
        return {**_runner_state(), "message": "already running"}
    script = os.path.join(_PROJECT_ROOT, "start-agent.sh")
    try:
        proc = subprocess.run([script], cwd=_PROJECT_ROOT, capture_output=True,
                              text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Agent did not start within 60s")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not run start-agent.sh: {e}")

    state = _runner_state()
    if not state["running"]:
        # The script prints why (backend down is the usual one).
        detail = (proc.stdout or proc.stderr or "").strip().splitlines()
        raise HTTPException(status_code=500,
                            detail=detail[-1] if detail else "Agent failed to start")
    return {**state, "message": "started"}


@router.post("/runner/stop")
def runner_stop(current_user=Depends(get_current_user)):
    """Stop the execution agent. Queued jobs then wait for it to come back."""
    pid = _agent_pid()
    if pid is None:
        return {**_runner_state(), "message": "not running"}
    # Signal the pid we recorded — never a name match, so nothing else can be hit.
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not stop agent: {e}")
    for _ in range(40):
        if _agent_pid() is None:
            break
        time.sleep(0.25)
    if _agent_pid() is not None:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        os.remove(_AGENT_PID_FILE)
    except OSError:
        pass
    return {**_runner_state(), "message": "stopped"}


# ── /agents/me/... — the agent's device operations, owned by the backend ─────
#
# These exist so an execution agent never needs a database session: it holds no
# DATABASE_URL, no credentials, and cannot reach the file at all from another Mac.
# Every route derives the machine from the AUTHENTICATED agent — `me` is the
# server's answer, never a client claim — so agent A can never touch agent B's
# device even by guessing its ids.
#
# They are business operations, not database CRUD: reserve this device for my run,
# release my reservation, allocate my WDA port. The underlying database functions
# are unchanged and still do the atomic work.


class ReservationRequest(BaseModel):
    run_id: str


class WdaPortRequest(BaseModel):
    preferred: Optional[int] = None


def _own_device(agent, device_id_or_udid: str, db: Session, by_udid: bool = False):
    """The DeviceRecord on the AUTHENTICATED agent's machine, or 404.

    Scoping every lookup to the caller's own machine is what makes a shared UDID
    safe: 26 udids in this database exist on two machines, and resolving one
    globally would hand an agent a device it does not own.
    """
    from automation.database.models import DeviceRecord

    q = db.query(DeviceRecord).filter(DeviceRecord.machine_id == agent.id)
    q = q.filter(DeviceRecord.udid == device_id_or_udid) if by_udid \
        else q.filter(DeviceRecord.id == device_id_or_udid)
    row = q.first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No such device on this machine: {device_id_or_udid}")
    return row


def _own_run(agent, run_id: str, db: Session):
    """The TestRun this agent may act for, or 403/404.

    A run claimed by another agent is not this agent's to reserve devices for —
    otherwise a credentialed agent could hold devices against someone else's job.
    A legacy run with no agent_id yet is allowed: the agent claiming it is the one
    executing it, and TestRun.machine_id stays NULL exactly as before.
    """
    from automation.database.models import TestRun

    run = db.query(TestRun).filter(TestRun.id == run_id).first()
    if run is None:
        raise HTTPException(status_code=404, detail=f"No such run: {run_id}")
    if run.agent_id and run.agent_id != agent.id:
        raise HTTPException(status_code=403,
                            detail="That run is claimed by a different agent.")
    return run


@router.get("/me/active-jobs")
def my_active_jobs(db: Session = Depends(get_db),
                   agent=Depends(require_authenticated_agent)):
    """Job ids the platform still considers in flight. Replaces the agent's own query.

    GLOBAL on purpose. This returns every agent's active jobs, exactly as the
    direct query it replaces did, because the orphan reaper uses it only to avoid
    killing a live process — and on a shared machine a per-agent list would let
    one agent reap another's work. Narrowing it is Phase 4F.7B, deliberately
    separate because it is a behaviour change rather than a boundary move.

    ACTIVE_JOB_STATES comes from proctree so the two sides cannot drift; that
    module imports neither the database nor FastAPI, so the direction is safe.
    """
    from automation.utils.proctree import ACTIVE_JOB_STATES
    rows = (db.query(TestRun.id)
            .filter(TestRun.job_state.in_(tuple(ACTIVE_JOB_STATES))).all())
    return {"job_ids": [str(r[0]) for r in rows]}


@router.get("/me/devices/{udid}")
def get_my_device(udid: str, db: Session = Depends(get_db),
                  agent=Depends(require_authenticated_agent)):
    """Resolve one of MY simulators by udid. Replaces the agent's device_row_id()."""
    row = _own_device(agent, udid, db, by_udid=True)
    return {"device_id": row.id, "udid": row.udid, "name": row.name,
            "platform": row.platform, "wda_port": row.wda_port}


@router.post("/me/devices/{device_id}/reserve", status_code=201)
def reserve_my_device(device_id: str, req: ReservationRequest,
                      db: Session = Depends(get_db),
                      agent=Depends(require_authenticated_agent)):
    """Take exclusive use of one of my simulators for one of my runs.

    409 means the device is BUSY — contention, not a test failure. The caller
    requeues its job rather than reporting a failed run.
    """
    from automation.device_manager.reservation import reserve_device, get_reservation

    row = _own_device(agent, device_id, db)
    _own_run(agent, req.run_id, db)

    # The unchanged atomic conditional UPDATE — no SELECT-then-UPDATE here.
    if not reserve_device(row.id, req.run_id, db=db):
        held = get_reservation(row.id, db=db)
        raise HTTPException(
            status_code=409,
            detail=f"Device is reserved by run {held.reserved_by if held else 'another run'}.")
    held = get_reservation(row.id, db=db)
    return {"device_id": row.id, "reserved_by": held.reserved_by,
            "reserved_at": held.reserved_at, "state": held.reserved_state}


@router.post("/me/devices/{device_id}/activate")
def activate_my_reservation(device_id: str, req: ReservationRequest,
                            db: Session = Depends(get_db),
                            agent=Depends(require_authenticated_agent)):
    """Mark my reservation as executing. reserved -> active, no new state machine."""
    from automation.device_manager.reservation import mark_active, get_reservation

    row = _own_device(agent, device_id, db)
    if not mark_active(row.id, req.run_id, db=db):
        raise HTTPException(status_code=409,
                            detail="That reservation belongs to a different run.")
    held = get_reservation(row.id, db=db)
    return {"device_id": row.id, "state": held.reserved_state if held else None}


@router.delete("/me/devices/{device_id}/reservation")
def release_my_reservation(device_id: str, req: ReservationRequest,
                           db: Session = Depends(get_db),
                           agent=Depends(require_authenticated_agent)):
    """Release MY reservation. Owner-only: a run can never free another run's device."""
    from automation.device_manager.reservation import release_device, get_reservation

    row = _own_device(agent, device_id, db)
    if not release_device(row.id, req.run_id, db=db):
        held = get_reservation(row.id, db=db)
        if held and held.reserved_by and held.reserved_by != req.run_id:
            raise HTTPException(status_code=409,
                                detail="That reservation belongs to a different run.")
        # Already free: releasing twice is harmless, as it was before.
        return {"device_id": row.id, "released": False}
    return {"device_id": row.id, "released": True}


@router.post("/me/devices/{device_id}/wda-port")
def allocate_my_wda_port(device_id: str, req: WdaPortRequest = None,
                         db: Session = Depends(get_db),
                         agent=Depends(require_authenticated_agent)):
    """The machine-scoped WDA port for one of my devices.

    An explicit `preferred` still wins outright, so the cross-app orchestrator's
    pinned 8100/8101 is unaffected. Otherwise the existing atomic lowest-free-port
    allocation runs, scoped to this machine.
    """
    from automation.device_manager.reservation import ensure_wda_port

    row = _own_device(agent, device_id, db)
    preferred = (req.preferred if req else None)
    if preferred:
        return {"device_id": row.id, "port": preferred, "preferred": True}
    port = ensure_wda_port(row.id, db=db)
    if port is None:
        raise HTTPException(status_code=503,
                            detail="No WDA port could be allocated on this machine.")
    return {"device_id": row.id, "port": port, "preferred": False}
