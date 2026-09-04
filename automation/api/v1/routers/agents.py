import os
import pathlib
import signal
import subprocess

from fastapi import APIRouter, Depends, HTTPException, status
from typing import List, Dict, Any
from pydantic import BaseModel
from sqlalchemy.orm import Session
import time
from datetime import datetime

from automation.database.config import get_db
from automation.database.models import ExecutionAgent
from automation.auth.security import get_current_user, require_agent

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

    return {"id": agent.id, "status": "registered"}

@router.post("/{agent_id}/heartbeat")
def heartbeat(agent_id: str, req: HeartbeatRequest, db: Session = Depends(get_db),
              _agent=Depends(require_agent)):
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
