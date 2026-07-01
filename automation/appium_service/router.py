from fastapi import APIRouter, HTTPException, Depends
from typing import List, Dict, Any

from automation.auth.security import get_current_user
from automation.appium_service.manager import appium_manager
from automation.appium_service.session_manager import session_manager
from automation.appium_service.log_manager import log_manager

router = APIRouter(prefix="/appium", tags=["appium"])

@router.get("/status", dependencies=[Depends(get_current_user)])
def get_appium_status():
    """Overall status of the Appium infrastructure."""
    active = session_manager.get_all_active_sessions()
    return {
        "status": "healthy",
        "active_sessions_count": len(active),
        "total_sessions_tracked": len(session_manager.sessions)
    }
    
@router.get("/sessions", dependencies=[Depends(get_current_user)])
def list_sessions():
    """List all active appium sessions."""
    active = session_manager.get_all_active_sessions()
    return {"sessions": [
        {
            "session_id": s.session_id,
            "run_id": s.run_id,
            "device_id": s.device_id,
            "port": s.port,
            "provider": s.provider,
            "status": s.status
        } for s in active
    ]}

@router.get("/logs/{run_id}", dependencies=[Depends(get_current_user)])
def get_logs(run_id: str):
    """Retrieve raw Appium logs for a specific execution run."""
    logs = log_manager.read_appium_logs(run_id)
    return {"logs": logs}
