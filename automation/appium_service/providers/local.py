import subprocess
import requests
import time
import logging
from typing import Dict, Any, Optional

from automation.appium_service.providers.base import AppiumProvider

logger = logging.getLogger(__name__)

class LocalAppiumProvider(AppiumProvider):
    def __init__(self):
        self.processes: Dict[int, subprocess.Popen] = {}
        self.log_files = {}
        
    def start_instance(self, port: int, log_path: str) -> bool:
        if port in self.processes:
            return True
            
        logger.info(f"LocalAppiumProvider: Starting Appium on port {port}, logging to {log_path}")
        try:
            log_handle = open(log_path, "w", encoding="utf-8")
            self.log_files[port] = log_handle
            
            process = subprocess.Popen(
                ["appium", "-p", str(port)],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True
            )
            self.processes[port] = process
            
            for _ in range(15):
                if self.is_healthy(port):
                    logger.info(f"LocalAppiumProvider: Appium ready on port {port}.")
                    return True
                time.sleep(1)
                
            logger.error(f"LocalAppiumProvider: Appium timeout on port {port}")
            self.stop_instance(port)
            return False
            
        except Exception as e:
            logger.error(f"LocalAppiumProvider: Failed to start Appium: {e}")
            return False

    def stop_instance(self, port: int) -> None:
        if port in self.processes:
            process = self.processes[port]
            process.terminate()
            process.wait()
            del self.processes[port]
            logger.info(f"LocalAppiumProvider: Stopped Appium on port {port}")
            
        if port in self.log_files:
            self.log_files[port].close()
            del self.log_files[port]
            
    def is_healthy(self, port: int) -> bool:
        try:
            res = requests.get(f"http://localhost:{port}/status", timeout=2)
            return res.status_code == 200
        except:
            return False
            
    def get_remote_url(self, port: int) -> str:
        return f"http://localhost:{port}/"
