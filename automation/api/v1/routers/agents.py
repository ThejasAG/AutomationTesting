from fastapi import APIRouter, Depends, HTTPException, status
from typing import List, Dict, Any
from pydantic import BaseModel
from sqlalchemy.orm import Session
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
    # Simple registration, ideally protected by an API key or auth token, but for now we'll leave it open for agents
    agent = ExecutionAgent(
        hostname=req.hostname,
        os=req.os,
        status="online",
        capabilities=req.capabilities,
        connected_devices=req.connected_devices
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)

    # Sync devices into the in-memory cache immediately on registration so the
    # device is available right away — not only after the first heartbeat (10s).
    from automation.device_manager.service import device_service
    device_service.sync_agent_devices(agent.id, req.connected_devices)

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
    device_service.sync_agent_devices(agent_id, req.connected_devices)
    
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
