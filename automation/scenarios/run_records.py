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


def resolve_device(selector: str, machine_id: Optional[str] = None):
    """Resolve a scenario's stored device through the machine-aware registry.

    Returns ``(udid, note, ambiguous)``:

      udid       the device to drive, or None when it could not be resolved
      note       why, for the log or the error message
      ambiguous  True when the request is genuinely unanswerable (two candidates,
                 or a device that exists elsewhere but not here) and the run must
                 stop rather than guess. False means "no opinion" -- the caller
                 keeps the selector it already had, which is the pre-P0-2
                 behaviour and must never become a new way for a run to fail.

    The database half of the P0-2 selector. It lives here, not in service.py,
    because Phase 4F.4 keeps the execution service free of SQLAlchemy so the
    agent can import it. There is deliberately no second resolver and no simctl
    call: device_manager.resolve_ios_device_record is the single authority, and
    it answers for every machine rather than only the backend's own.
    """
    from automation.device_manager.service import (
        resolve_ios_device_record, local_machine_id)

    sel = (selector or "").strip()
    if not sel:
        return None, "no device given", False

    try:
        target = machine_id or local_machine_id()
    except Exception as e:
        return None, f"local machine id unavailable ({e})", False

    looks_like_udid = len(sel) == 36 and sel.count("-") == 4

    if looks_like_udid:
        _m, udid, _note = resolve_ios_device_record(sel, machine_id=target)
        if udid:
            return udid, None, False

        # Registered on another machine: carry the NAME across, which is the
        # part that is portable, and re-resolve it here.
        name = _registered_name(sel)
        if not name:
            # Unknown everywhere. Not this function's business to fail the run:
            # the device may be legitimately unregistered on a single-machine
            # install that never ran discovery.
            return None, f"device {sel} is not registered on machine {target}", False
        logger.info("device %s is not on machine %s; resolving by name %r",
                    sel, target, name)
        sel = name

    _m, udid, note = resolve_ios_device_record(None, machine_id=target, name=sel)
    if udid:
        return udid, None, False

    # Several devices answer to this name: unanswerable. Continuing would drive
    # whichever simulator happened to sort first, so the run must stop.
    if _name_is_ambiguous(sel):
        return None, note, True

    # Nothing answers to it. Whether that stops the run depends on what the
    # selector IS, not on the registry being complete:
    #
    #   a DEVICE NAME ("iPhone 16 Pro") is not something Appium can be handed --
    #   it only ever meant "look this up" -- so an unresolvable one is fatal, and
    #   this is the portability case P0-2 exists for: a scenario seeded with a
    #   name that no device on this machine matches.
    #
    #   anything else is an opaque device IDENTIFIER. It was drivable before P0-2
    #   on any install that never ran device discovery, and is passed through
    #   untouched. Nothing is substituted, so no other machine's device can be
    #   reached this way.
    if _looks_like_a_device_name(sel):
        return None, note, True
    return None, note, False


def _looks_like_a_device_name(sel: str) -> bool:
    """True for a human device NAME rather than an opaque device identifier.

    Apple's simulator names are the only names the platform ever seeds
    ('iPhone 16 Pro', 'iPad Pro 11-inch (M4)'), so the test is deliberately
    narrow: matching too widely would turn an unregistered device id into a
    failed run, which is the regression this whole distinction exists to avoid.
    """
    head = sel.split()[0].lower() if sel.split() else ""
    return head in {"iphone", "ipad", "ipod", "apple"}


def _name_is_ambiguous(name: str) -> bool:
    """True when more than one registered device answers to *name*.

    Asked separately from resolution because 'two candidates' and 'no candidate'
    are different answers: the first must stop the run, the second must not.
    """
    from automation.database.config import SessionLocal
    from automation.database.models import DeviceRecord
    db = None
    try:
        db = SessionLocal()
        return db.query(DeviceRecord).filter(DeviceRecord.name == name).count() > 1
    except Exception as e:
        logger.debug("ambiguity check for %r failed: %s", name, e)
        return False
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def _registered_name(udid: str) -> Optional[str]:
    """The name a udid is registered under on ANY machine, when unambiguous.

    The bridge that makes a scenario portable between Macs: the original
    machine's udid means nothing here, but the model name it was registered
    under does.
    """
    from automation.database.config import SessionLocal
    from automation.database.models import DeviceRecord
    db = None
    try:
        db = SessionLocal()
        names = {r.name for r in
                 db.query(DeviceRecord).filter(DeviceRecord.udid == udid).all()
                 if r.name}
        return names.pop() if len(names) == 1 else None
    except Exception as e:
        logger.debug("name lookup for %s failed: %s", udid, e)
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


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
