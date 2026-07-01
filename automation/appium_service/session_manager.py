import uuid
import time
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

class AppiumSession:
    def __init__(self, run_id: str, device_id: str, port: int, provider: str):
        self.session_id = str(uuid.uuid4())
        self.run_id = run_id
        self.device_id = device_id
        self.port = port
        self.provider = provider
        self.status = "Active"
        self.started_time = time.time()
        self.finished_time: Optional[float] = None
        self.duration: int = 0

class SessionManager:
    def __init__(self):
        self.sessions: Dict[str, AppiumSession] = {}
        
    def create_session(self, run_id: str, device_id: str, port: int, provider: str) -> AppiumSession:
        session = AppiumSession(run_id, device_id, port, provider)
        self.sessions[session.session_id] = session
        logger.info(f"SessionManager: Created session {session.session_id} for run {run_id}")
        return session
        
    def end_session(self, session_id: str) -> None:
        if session_id in self.sessions:
            session = self.sessions[session_id]
            session.status = "Ended"
            session.finished_time = time.time()
            session.duration = int((session.finished_time - session.started_time) * 1000)
            logger.info(f"SessionManager: Ended session {session_id}")
            
    def get_session(self, session_id: str) -> Optional[AppiumSession]:
        return self.sessions.get(session_id)
        
    def get_all_active_sessions(self) -> list:
        return [s for s in self.sessions.values() if s.status == "Active"]

session_manager = SessionManager()
