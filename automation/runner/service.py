import os
import threading
import uuid
import time
import json
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

from automation.database import database
from automation.database.config import SessionLocal
from automation.ai.service import RCAService
from automation.evidence.collector import EvidenceCollector, EvidenceCollectorConfig
from automation.reports.generator import ReportGenerator

from automation.projects.repository import repository_manager
from automation.plugins.appium_framework import AppiumFramework
from automation.ai.provider import default_config

logger = logging.getLogger(__name__)

class RunnerService:
    def __init__(self):
        self.active_runs = {} # type: Dict[str, Dict[str, Any]]
        self.ai_service = RCAService(default_config)
        self.evidence_collector = EvidenceCollector(EvidenceCollectorConfig(repo_path=os.getcwd()))
        self.report_generator = ReportGenerator(output_dir="reports")
        
        # Plugins registry
        self.frameworks = {
            "appium": AppiumFramework()
        }
        
    def execute_project(self, project_id: str, device_id: str, run_id: Optional[str] = None, triggered_by: Optional[str] = None) -> str:
        """New orchestrated execution flow."""
        from automation.device_manager.service import device_service
        from automation.device_manager.models import DeviceStatus
        
        # 1. Device Safety Check
        device = device_service.get_device(device_id)
        if not device:
            raise ValueError(f"Device {device_id} not found.")
            
        health = device_service.get_device_health(device_id)
        if health and health.battery is not None and health.battery < 15:
            raise ValueError(f"Device battery is too low ({health.battery}%). Minimum required is 15%.")
            
        # 2. Get Project DB info
        with SessionLocal() as db:
            project_db = database.get_test_project(db, project_id)
            if not project_db:
                raise ValueError(f"Project {project_id} not found.")
            git_url = project_db.git_url
            branch = project_db.default_branch
            project_name = project_db.name
        
        if not run_id:
            run_id = str(uuid.uuid4())
            
        self.active_runs[run_id] = {
            "id": run_id,
            "status": "Queued",
            "logs": ["Job queued for execution. Waiting for an available Execution Agent..."],
            "project_id": project_id,
            "device_id": device_id,
            "start_time": time.time(),
            "end_time": None,
            "duration_ms": 0,
            "triggered_by": triggered_by,
            "timeline": []
        }
        
        self._add_timeline_event(run_id, "Run Queued")
        
        # Create run record in the database
        # NOTE: SQLAlchemy DateTime columns require actual datetime objects,
        # not ISO-format strings — do NOT call .isoformat() here.
        now = datetime.utcnow()
        run_record = {
            "id": run_id,
            "project_id": project_id,
            "test_suite": project_name,
            "test_name": "Execution",
            "status": "queued",
            "started_at": now,
            "completed_at": None,
            "duration_ms": None,
            "device_name": device_id,
            "os_version": device.platform_version,
            "platform": device.platform,
            "created_at": now,
            "triggered_by": triggered_by
        }
        
        with SessionLocal() as db:
            database.insert_test_run(db, run_record)
            
        return run_id
        
    def append_agent_logs(self, run_id: str, logs: List[str], status: str):
        if run_id in self.active_runs:
            self.active_runs[run_id]["logs"].extend(logs)
            if status.lower() in ["passed", "failed", "stopped"]:
                self.active_runs[run_id]["status"] = "Completed" if status.lower() == "passed" else "Failed"
                if not self.active_runs[run_id]["end_time"]:
                    self.active_runs[run_id]["end_time"] = time.time()
                    self.active_runs[run_id]["duration_ms"] = int((self.active_runs[run_id]["end_time"] - self.active_runs[run_id]["start_time"]) * 1000)
            else:
                self.active_runs[run_id]["status"] = "Running"

    def _add_timeline_event(self, run_id: str, event_name: str):
        if run_id in self.active_runs:
            self.active_runs[run_id]["timeline"].append({
                "time": datetime.utcnow().strftime("%H:%M:%S"),
                "event": event_name
            })
            
    def get_run_status(self, run_id: str) -> Dict[str, Any]:
        if run_id in self.active_runs:
            run = self.active_runs[run_id]
            return {
                "id": run_id,
                "status": run["status"],
                "logs": list(run["logs"]),
                "duration_ms": run["duration_ms"] if run["end_time"] else (int((time.time() - run["start_time"]) * 1000) if run["start_time"] else 0)
            }
        return {"status": "Not Found", "logs": []}
        
    def stop_run(self, run_id: str):
        if run_id in self.active_runs:
            run = self.active_runs[run_id]
            run["status"] = "Stopped"
            run["logs"].append("Execution stopped by user.")
            run["end_time"] = time.time()
            run["duration_ms"] = int((run["end_time"] - run["start_time"]) * 1000)
                
            with SessionLocal() as db:
                db_run = database.get_test_run(db, run_id)
                if db_run:
                    db_run = dict(db_run)
                    db_run["status"] = "stopped"
                    db_run["job_state"] = "cancelled"
                    db_run["completed_at"] = datetime.utcnow().isoformat()
                    database.insert_test_run(db, db_run)

runner_service = RunnerService()
