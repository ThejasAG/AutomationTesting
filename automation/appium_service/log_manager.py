import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

class LogManager:
    def __init__(self, base_log_dir: str = "logs"):
        self.base_log_dir = os.path.abspath(base_log_dir)
        if not os.path.exists(self.base_log_dir):
            os.makedirs(self.base_log_dir)
            
    def get_log_dir(self, run_id: str) -> str:
        """Get the directory for a specific run, creating it if needed."""
        run_dir = os.path.join(self.base_log_dir, f"run_{run_id}")
        if not os.path.exists(run_dir):
            os.makedirs(run_dir)
        return run_dir
        
    def get_appium_log_path(self, run_id: str) -> str:
        """Get the file path for the appium server log for a specific run."""
        return os.path.join(self.get_log_dir(run_id), "appium.log")
        
    def read_appium_logs(self, run_id: str) -> str:
        """Retrieve logs for a specific run."""
        path = self.get_appium_log_path(run_id)
        if not os.path.exists(path):
            return "No Appium logs found for this run."
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

log_manager = LogManager()
