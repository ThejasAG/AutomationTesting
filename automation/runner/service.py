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

logger = logging.getLogger(__name__)

class RunnerService:
    def __init__(self):
        self.active_runs = {} # type: Dict[str, Dict[str, Any]]
        # No config passed → RCAService uses the env-driven Ollama default.
        self.ai_service = RCAService()
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

            # Surface "Last Execution" on the project card in the dashboard.
            from automation.database.models import TestProject
            project_row = db.query(TestProject).filter(TestProject.id == project_id).first()
            if project_row:
                project_row.last_execution_at = now
                db.commit()

        return run_id
        
    def append_agent_logs(self, run_id: str, logs: List[str], status: str):
        if run_id in self.active_runs:
            self.active_runs[run_id]["logs"].extend(logs)
            normalized = status.lower()
            if normalized in ["passed", "failed", "stopped"]:
                self.active_runs[run_id]["status"] = "Completed" if normalized == "passed" else "Failed"
                if not self.active_runs[run_id]["end_time"]:
                    self.active_runs[run_id]["end_time"] = time.time()
                    self.active_runs[run_id]["duration_ms"] = int((self.active_runs[run_id]["end_time"] - self.active_runs[run_id]["start_time"]) * 1000)

                # Persist the terminal status, then generate an RCA report.
                if normalized in ("passed", "failed"):
                    self._persist_final_status(run_id, normalized)
                    self._trigger_rca(run_id, normalized)
            else:
                self.active_runs[run_id]["status"] = "Running"

    def _persist_final_status(self, run_id: str, status: str):
        """Write the final run status/duration back to the test_runs table."""
        try:
            run = self.active_runs.get(run_id, {})
            error_message = None
            if status == "failed":
                logs = run.get("logs", [])
                error_message = logs[-1] if logs else "Test failed"
            with SessionLocal() as db:
                db_run = database.get_test_run(db, run_id)
                if db_run:
                    db_run = dict(db_run)
                    db_run["status"] = status
                    db_run["job_state"] = "completed" if status == "passed" else "failed"
                    db_run["completed_at"] = datetime.utcnow()
                    db_run["duration_ms"] = run.get("duration_ms")
                    if error_message:
                        db_run["error_message"] = error_message
                    database.insert_test_run(db, db_run)
        except Exception as e:
            logger.error(f"Failed to persist final status for run {run_id}: {e}")

    def _trigger_rca(self, run_id: str, status: str):
        """Generate and store an RCA report for a completed run.

        Failed runs get a full LLM analysis via the RCA service; passed runs get
        a lightweight "tests passed successfully" report (no LLM call needed).
        """
        try:
            with SessionLocal() as db:
                db_run = database.get_test_run(db, run_id)
                evidence = database.get_evidence(db, run_id) or {}

            if status == "failed":
                run = self.active_runs.get(run_id, {})
                evidence_bundle = {
                    "test_name": (db_run or {}).get("test_name", "unknown"),
                    "error_message": (db_run or {}).get("error_message", "")
                        or (run.get("logs", [])[-1] if run.get("logs") else ""),
                    "failed_locator": evidence.get("failed_locator", "unknown"),
                    "git_commit": evidence.get("git_commit", (db_run or {}).get("commit_sha", "unknown")),
                    "git_author": evidence.get("git_author", "unknown"),
                    "git_branch": evidence.get("git_branch", (db_run or {}).get("branch", "unknown")),
                    "git_diff": evidence.get("git_diff", "No changes detected"),
                    "appium_logs": run.get("logs", []),
                    "device_logs": evidence.get("device_logs", "") or "",
                    "xml_page_source": evidence.get("xml_page_source", ""),
                }
                analysis = self.ai_service.analyze(evidence_bundle)
                report = {
                    "run_id": run_id,
                    "root_cause": analysis.root_cause,
                    "failure_category": analysis.failure_category,
                    "affected_modules": analysis.affected_modules or [],
                    "confidence": analysis.confidence,
                    "possible_reason": analysis.possible_reason,
                    "impact": analysis.impact,
                    "suggested_fix": analysis.suggested_fix,
                    "priority": analysis.priority,
                    "severity": analysis.severity,
                    "responsible_module": analysis.responsible_module,
                    "summary": analysis.summary,
                    "llm_provider": self.ai_service.provider.name,
                    "llm_model": os.getenv("LLM_MODEL_NAME", "llama3.2"),
                }
            else:  # passed
                report = {
                    "run_id": run_id,
                    "root_cause": "Tests passed successfully",
                    "failure_category": "None",
                    "affected_modules": [],
                    "confidence": 1.0,
                    "possible_reason": "All assertions passed; no failure was detected in this run.",
                    "impact": "No impact — the test run completed successfully.",
                    "suggested_fix": "No action required.",
                    "priority": "Low",
                    "severity": "Minor",
                    "responsible_module": "N/A",
                    "summary": "Tests passed successfully.",
                    "llm_provider": "system",
                    "llm_model": "n/a",
                }

            with SessionLocal() as db:
                database.insert_rca_report(db, report)
            logger.info(f"RCA report generated for run {run_id} (status={status})")
        except Exception as e:
            logger.error(f"Failed to generate RCA for run {run_id}: {e}")

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
        
    def stop_run(self, run_id: str) -> bool:
        """Stop a run whether it is executing in THIS process or queued for the
        distributed agent.

        Returns False if the run is unknown or already finished. A queued run is
        not in active_runs — only marking the DB job_state 'cancelled' keeps the
        agent from picking it up (or tells a running agent to abort), so the DB
        update must happen regardless of active_runs.
        """
        if run_id in self.active_runs:
            run = self.active_runs[run_id]
            run["status"] = "Stopped"
            run["logs"].append("Execution stopped by user.")
            run["end_time"] = time.time()
            run["duration_ms"] = int((run["end_time"] - run["start_time"]) * 1000)

        with SessionLocal() as db:
            db_run = database.get_test_run(db, run_id)
            if not db_run:
                return False
            db_run = dict(db_run)
            if db_run.get("status") in ("passed", "failed", "completed", "stopped", "cancelled"):
                return False  # already terminal — nothing to stop
            db_run["status"] = "stopped"
            db_run["job_state"] = "cancelled"
            # DateTime columns require real datetime objects, not ISO strings.
            db_run["completed_at"] = datetime.utcnow()
            database.insert_test_run(db, db_run)
            return True

runner_service = RunnerService()
