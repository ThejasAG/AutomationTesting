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
        from automation.device_manager.service import (device_service,
                                                       machine_for_local_device as _machine_for)
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
            "triggered_by": triggered_by,
            # Routing intent. The caller resolved and registered this simulator on
            # THIS host, so the registry can confirm which machine owns it.
            "machine_id": _machine_for(device_id),
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
                    # Visual regression check (never flips a pass to a fail; sets a warning flag).
                    self._trigger_visual_regression(run_id, normalized)
                    # Fire an immediate Slack alert on failure.
                    if normalized == "failed":
                        try:
                            from automation.notifications.realtime_alerts import realtime_alert_service
                            run = self.active_runs.get(run_id, {})
                            err = (run.get("logs") or ["Test failed"])[-1]
                            realtime_alert_service.alert_ios_failure(
                                run_id, run.get("test_name") or run_id, str(err))
                        except Exception as e:
                            logger.warning(f"realtime alert failed for {run_id}: {e}")
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
        # If this run came from a GitHub PR, comment the verdict back onto the PR.
        self._maybe_comment_on_pr(run_id, status)

    def _maybe_comment_on_pr(self, run_id: str, status: str):
        """Post a QA-verdict comment + commit status on the originating PR."""
        try:
            from automation.database.models import TestRun, RCAReport, VisualRegressionResult
            with SessionLocal() as db:
                run = db.query(TestRun).filter(TestRun.id == run_id).first()
                if not run or not (run.triggered_by or "").startswith("github_webhook:"):
                    return
                # triggered_by = "github_webhook:owner/repo#<pr>"
                meta = run.triggered_by.split("github_webhook:", 1)[1]
                repo_full, _, pr_str = meta.partition("#")
                if "/" not in repo_full or not pr_str:
                    return
                owner, short_repo = repo_full.split("/", 1)
                pr_number = int(pr_str)
                commit_sha = run.commit_sha

                rca = db.query(RCAReport).filter(RCAReport.run_id == run_id).first()
                vrs = db.query(VisualRegressionResult).filter(
                    VisualRegressionResult.run_id == run_id).all()

            from automation.ai.services.summary import test_summary_generator
            with SessionLocal() as db:
                summary = test_summary_generator.generate_run_summary(run_id, db).get("summary")

            run_results = {
                "verdict": status,
                "status": status,
                "run_id": run_id,
                "total": 1,
                "passed": 1 if status == "passed" else 0,
                "failed": 1 if status == "failed" else 0,
                "rca": getattr(rca, "root_cause", None) if rca else None,
                "summary": summary,
                "visual_regression": {
                    "regressions": [
                        {"screen": v.screen_name, "diff_percentage": v.diff_percentage,
                         "severity": v.severity}
                        for v in vrs if not v.passed
                    ]
                },
            }
            from automation.integrations.pr_comment import pr_comment_bot
            pr_comment_bot.post_pr_comment(pr_number, short_repo, run_results, owner=owner)
            gh_state = "success" if status == "passed" else "failure"
            pr_comment_bot.update_pr_status(
                short_repo, commit_sha, gh_state,
                f"QA {status}", owner=owner)
        except Exception as e:
            logger.warning(f"PR comment for run {run_id} failed: {e}")

    def _trigger_visual_regression(self, run_id: str, status: str):
        """Compare this run's screenshots to the project baseline and store results.

        A regression on a passing run does NOT flip it to failed — it sets
        `visual_warning` so the UI can surface it while keeping the honest verdict.
        """
        try:
            from automation.intelligence.visual_regression import visual_regression_analyzer
            from automation.database.models import TestRun, VisualRegressionResult

            with SessionLocal() as db:
                run = db.query(TestRun).filter(TestRun.id == run_id).first()
                if not run or not run.project_id:
                    return
                project_id = run.project_id
                screenshots_dir = visual_regression_analyzer._screenshots_dir(run_id)

            result = visual_regression_analyzer.compare_with_baseline(
                run_id, project_id, [screenshots_dir])

            with SessionLocal() as db:
                for reg in result.get("regressions", []):
                    db.add(VisualRegressionResult(
                        run_id=run_id,
                        screen_name=reg["screen"],
                        diff_percentage=reg["diff_percentage"],
                        severity=reg["severity"],
                        baseline_path=reg["baseline_path"],
                        current_path=reg["current_path"],
                        diff_path=reg["diff_path"],
                        passed=False,
                    ))
                if result.get("regressions"):
                    run = db.query(TestRun).filter(TestRun.id == run_id).first()
                    if run:
                        run.visual_warning = True
                db.commit()

            # No baseline yet + a clean pass → capture this run as the baseline.
            if status == "passed" and result.get("total_screens", 0) == 0:
                visual_regression_analyzer.capture_baseline(run_id, screenshots_dir)
        except Exception as e:
            logger.warning(f"visual regression check failed for {run_id}: {e}")

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

        # Cross-app flows run in a daemon THREAD in this process, and marking the
        # database never reached it: Stop turned the row 'stopped' while the thread
        # kept driving the simulators to the end of the flow, then overwrote the row
        # with passed/failed on its way out. Ask it to stop for real. Cooperative —
        # it unwinds through its own finally and quits its Appium sessions, which is
        # what actually frees the devices.
        try:
            from automation.scenarios.cross_app_flows import request_flow_stop
            request_flow_stop(run_id)
        except Exception:
            # An in-process flow runner is one of several ways a run can execute;
            # a run dispatched to a distributed agent has none, and the database
            # update below is what stops that one. Never fail the stop over this.
            logger.exception("could not signal in-process flow run %s", run_id)

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
