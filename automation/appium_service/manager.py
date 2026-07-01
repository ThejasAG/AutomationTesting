import subprocess
import socket
import logging
import time
import os
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

def get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('', 0))
    s.listen(1)
    port = s.getsockname()[1]
    s.close()
    return port

class AppiumSession:
    def __init__(self, session_id: str, device_id: str, port: int, process: subprocess.Popen):
        self.session_id = session_id
        self.device_id = device_id
        self.port = port
        self.process = process
        self.status = "running"
        self.created_at = time.time()

class AppiumProcessManager:
    def __init__(self):
        self.active_sessions: Dict[str, AppiumSession] = {}

    def allocate_instance(self, run_id: str, device_id: str) -> AppiumSession:
        """Spawns a real Appium process on a dynamic port"""
        port = get_free_port()
        logger.info(f"Starting real Appium server for run {run_id} on port {port}")
        
        # Use npx to launch appium to ensure it runs even if not globally installed
        npx_cmd = "npx.cmd" if os.name == "nt" else "npx"
        cmd = [npx_cmd, "appium", "-p", str(port), "--log-level", "error"]
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        # Wait a moment for appium to start
        time.sleep(3)
        
        # Check if it crashed immediately
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            logger.error(f"Appium failed to start: {stderr}")
            raise Exception("Appium process crashed on startup.")
            
        session = AppiumSession(
            session_id=run_id,
            device_id=device_id,
            port=port,
            process=process
        )
        self.active_sessions[run_id] = session
        return session

    def release_instance(self, session_id: str) -> bool:
        """Terminates the Appium process"""
        if session_id in self.active_sessions:
            session = self.active_sessions[session_id]
            logger.info(f"Terminating Appium server for session {session_id} on port {session.port}")
            try:
                session.process.terminate()
                session.process.wait(timeout=5)
            except Exception as e:
                logger.warning(f"Failed to cleanly terminate Appium: {e}")
                session.process.kill()
                
            del self.active_sessions[session_id]
            return True
        return False
        
    def get_session(self, session_id: str) -> AppiumSession:
        return self.active_sessions.get(session_id)

appium_manager = AppiumProcessManager()
