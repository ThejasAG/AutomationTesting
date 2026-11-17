from fastapi import APIRouter, Depends
from typing import List
from sqlalchemy.orm import Session

from automation.database.config import get_db
from automation.database.models import ExecutionAgent, AuditLog
from automation.auth.security import get_current_user

router = APIRouter(prefix="/ops", tags=["Operations"])

@router.get("/agents")
def get_agents(db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    """List all agents and their metrics"""
    agents = db.query(ExecutionAgent).all()
    return {"agents": agents}

@router.get("/audit")
def get_audit_logs(db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    """Get audit logs"""
    logs = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(100).all()
    return {"logs": logs}


