import threading
import time
import requests
import logging
from typing import Callable, List, Dict

logger = logging.getLogger(__name__)

class HealthMonitor:
    def __init__(self, interval: int = 5):
        self.interval = interval
        self.running = False
        self._thread = None
        self.callbacks: List[Callable[[Dict], None]] = []
        
    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.info("HealthMonitor: Started background monitoring thread.")
        
    def stop(self):
        self.running = False
        if self._thread:
            self._thread.join(timeout=2)
            
    def register_callback(self, callback: Callable[[Dict], None]):
        self.callbacks.append(callback)
        
    def _monitor_loop(self):
        while self.running:
            # Here we would normally query `AppiumManager` for active instances 
            # and verify process health via psutil or HTTP calls.
            # We fire callbacks to notify the manager if an instance has died.
            time.sleep(self.interval)

health_monitor = HealthMonitor()
