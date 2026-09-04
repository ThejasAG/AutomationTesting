"""Backend-local run bookkeeping for scenario execution.

The database half of the scenario path, and the ONLY half that needs a Session.
It registers itself with the execution service on import, so that service - which
the agent runs too - imports nothing from the database layer.

The agent never imports this module: it passes its job_id as the run id
(Phase 4F.4a) and reports steps over the ScenarioResult API (Phase 4F.3), so
nothing here is reached and the agent process opens no session.
"""

import logging
import sys
from datetime import datetime
from typing import Optional

from automation.scenarios import service
from automation.scenarios.service import ScenarioError

logger = logging.getLogger("scenario")


def project_bundle_id(db, project_id: str) -> Optional[str]:
    """The project's stored app bundle id. 404s exactly as the endpoint always has."""
    from automation.database.models import TestProject
    project = db.query(TestProject).filter(TestProject.id == project_id).first()
    if not project:
        raise ScenarioError(404, "Project not found")
    return project.app_bundle_id


def start(req) -> Optional[str]:
    """Create a TestRun so this Scenarios-tab execution shows in Dashboard/Reports."""
    import uuid
    from automation.database.config import SessionLocal
    from automation.database import database
    try:
        run_id = str(uuid.uuid4())
        now = datetime.utcnow()
        with SessionLocal() as db:
            database.insert_test_run(db, {
                "id": run_id, "project_id": req.project_id,
                "test_suite": "Scenario (iOS)", "test_name": req.name or "scenario",
                "status": "running", "job_state": "running",
                "started_at": now, "created_at": now,
                "device_name": req.device_id, "platform": "iOS", "bot_type": "ios",
                "triggered_by": "scenarios-tab",
            })
        return run_id
    except Exception as e:
        logger.warning("scenario persist-start failed: %s", e)
        return None


def step(run_id: Optional[str], index: int, res, secs: Optional[float] = None) -> None:
    from automation.database.config import SessionLocal
    from automation.database.models import ScenarioResult
    try:
        status = "PASS" if getattr(res, "ok", False) else "FAIL"
        with SessionLocal() as db:
            row = (db.query(ScenarioResult)
                   .filter_by(run_id=run_id, scenario_num=str(index)).first())
            if row is None:
                row = ScenarioResult(run_id=run_id, scenario_num=str(index))
                db.add(row)
            row.scenario_name = getattr(res, "step", f"step {index}")
            row.status = status
            row.consumer_status = status
            row.error = (getattr(res, "detail", "") or None) if status == "FAIL" else None
            # Real wall-clock for THIS step. Left unset before, so every scenario-runner
            # row reached the report with a blank duration.
            if secs is not None:
                row.launch_time = round(secs, 1)
            db.commit()
    except Exception as e:
        logger.warning("scenario persist-step failed: %s", e)


def finish(run_id: Optional[str], started: datetime,
           out=None, error: Optional[str] = None) -> None:
    if not run_id:
        return
    from automation.database.config import SessionLocal
    from automation.database import database
    try:
        ok = bool(out and out.ok) and not error
        status = "passed" if ok else "failed"
        now = datetime.utcnow()
        crashed = service._crashed(out)
        with SessionLocal() as db:
            db_run = database.get_test_run(db, run_id)
            if db_run:
                db_run = dict(db_run)
                db_run["status"] = status
                db_run["job_state"] = status
                db_run["completed_at"] = now
                db_run["duration_ms"] = int((now - started).total_seconds() * 1000)
                db_run["crash_detected"] = crashed
                if error:
                    db_run["error_message"] = error
                database.insert_test_run(db, db_run)
        if crashed:
            service._collect_crash_reports(run_id)
        # A quick PM-friendly summary (uses the fast non-LLM fallback when Ollama is off).
        try:
            from automation.ai.services.summary import test_summary_generator
            with SessionLocal() as db:
                test_summary_generator.generate_run_summary(run_id, db)
        except Exception:
            pass
    except Exception as e:
        logger.warning("scenario persist-finish failed: %s", e)


# Wiring. Importing this module is what gives a backend-local scenario run its
# TestRun and its ScenarioResult rows.
service.set_run_store(sys.modules[__name__])
