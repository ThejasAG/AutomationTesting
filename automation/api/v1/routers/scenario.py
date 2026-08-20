"""Scenario runner API — plain-language steps drive the app and record a script.

POST a list of directions; the engine drives the running app, resolving each step
against the live screen, saves the generated pytest file into the project's e2e/,
and returns a per-step report.
"""

import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, Generator, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from automation.auth.security import get_current_user
from automation.database.config import get_db
from automation.database.models import TestProject
from automation.projects.repository import repository_manager

logger = logging.getLogger("scenario")
router = APIRouter(prefix="/scenario", tags=["Scenario Runner"])


class ScenarioRequest(BaseModel):
    project_id: str                       # the ENVIRONMENT to run against (prod/staging project)
    steps: List[str]
    device_id: str
    bundle_id: Optional[str] = None      # falls back to the project's stored bundle id
    appium_url: str = "http://127.0.0.1:4723"
    name: str = "scenario"
    save: bool = True                     # write the generated .py into e2e/
    # Pull the latest build for this environment and build+install it before
    # running — used for staging, which updates daily. Same steps, fresh build.
    prepare: bool = False


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "scenario"


def _sse(payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# How many recorded scenarios to keep per name before pruning the oldest.
_KEEP_SCENARIOS = 10


def _save_scenario(e2e_dir: str, name: str, source: str) -> str:
    """Write the recorded scenario to a per-run file and prune old ones.

    Every run used to write e2e/test_{name}.py, so the default name "scenario"
    meant each run silently destroyed the previous recording — there was no
    history for any execution. Runs are timestamped instead, keeping the last
    _KEEP_SCENARIOS.
    """
    os.makedirs(e2e_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(e2e_dir, f"test_{name}_{stamp}.py")
    with open(path, "w") as f:
        f.write(source)

    prefix, suffix = f"test_{name}_", ".py"
    mine = sorted(
        (f for f in os.listdir(e2e_dir)
         if f.startswith(prefix) and f.endswith(suffix)),
        reverse=True,
    )
    for stale in mine[_KEEP_SCENARIOS:]:
        try:
            os.remove(os.path.join(e2e_dir, stale))
            logger.info("Pruned old scenario recording %s", stale)
        except OSError as e:
            logger.warning("Could not prune %s: %s", stale, e)
    return path


def _appium_options(device_id: str, bundle_id: str):
    from appium.options.ios import XCUITestOptions

    opts = XCUITestOptions()
    opts.platform_name = "iOS"
    opts.automation_name = "XCUITest"
    opts.udid = device_id
    opts.bundle_id = bundle_id
    opts.no_reset = True
    # WDA config comes from the central resolver — derivedDataPath, wdaLocalPort,
    # wdaLaunchTimeout, usePrebuiltWDA and useNewWDA. This used to hardcode one
    # DerivedData path and set no wdaLocalPort, so it silently landed on Appium's
    # default 8100 — the same port cross-app claims for the consumer sim, and a
    # second session on that port tears the first one down mid-run.
    from automation.appium_service import wda as _wda
    _wda.apply(opts, udid=device_id)
    # ── Speed ────────────────────────────────────────────────────────────────
    # By default XCUITest waits for the app's main thread to be "quiescent" before
    # every single command — the biggest hidden cost on iOS. Turn it off; our own
    # wait_for_idle handles genuine spinners. Compact responses + a bounded UI-tree
    # snapshot make page_source and element lookups much faster too.
    opts.set_capability("waitForQuiescence", False)
    opts.set_capability("shouldUseCompactResponses", True)
    opts.set_capability("maxTypingFrequency", 60)
    return opts


# Runtime session settings that make each command faster. Applied right after the
# session starts (some only take effect via update_settings, not capabilities).
_SPEED_SETTINGS = {
    "waitForIdleTimeout": 0,          # don't block on app-idle between commands
    "shouldWaitForQuiescence": False,
    "shouldUseCompactResponses": True,
    # React Native screens nest deeply — the real tappable rows sit well below
    # depth 40, so a shallow cap made Appium blind to them ("No element matches"
    # even though idb sees them). 60 reaches RN list rows while staying bounded.
    "snapshotMaxDepth": 60,
    "useFirstMatch": True,            # return the first matching element, don't collect all
    "customSnapshotTimeout": 8,       # a deeper snapshot needs a little more time
}


def _apply_speed_settings(driver) -> None:
    try:
        driver.update_settings(_SPEED_SETTINGS)
    except Exception as e:
        logger.debug("Could not apply speed settings: %s", e)


def _resolve_run(req: "ScenarioRequest", db: Session):
    """Validate the request and pull everything needed off the DB.

    Shared by both endpoints, and called BEFORE the streaming generator starts —
    the generator outlives the request handler, so it must never touch the
    session itself.
    """
    project = db.query(TestProject).filter(TestProject.id == req.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    bundle_id = req.bundle_id or project.app_bundle_id
    if not bundle_id:
        raise HTTPException(
            status_code=400,
            detail="No app bundle id — set one on the project or pass bundle_id.",
        )

    steps = [s for s in req.steps if s.strip()]
    if not steps:
        raise HTTPException(status_code=400, detail="No scenario steps provided.")

    repo_path = repository_manager.get_repo_path(req.project_id)
    return bundle_id, steps, repo_path


def _persist_run_start(req: "ScenarioRequest") -> Optional[str]:
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


def _persist_step(run_id: Optional[str], index: int, res, secs: Optional[float] = None) -> None:
    if not run_id:
        return
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


def _collect_crash_reports(run_id: str) -> None:
    """Copy recent iOS-simulator crash reports into the run's evidence dir so the
    RCA has the native stack trace, not just 'app not in foreground'."""
    import glob, shutil, time as _t
    try:
        src_dir = os.path.expanduser("~/Library/Logs/DiagnosticReports")
        dest = os.path.join(os.getcwd(), "reports", run_id, "crash")
        os.makedirs(dest, exist_ok=True)
        cutoff = _t.time() - 300  # crashes from the last 5 minutes
        copied = 0
        for f in glob.glob(os.path.join(src_dir, "*.ips")) + glob.glob(os.path.join(src_dir, "*.crash")):
            try:
                if os.path.getmtime(f) >= cutoff:
                    shutil.copyfile(f, os.path.join(dest, os.path.basename(f)))
                    copied += 1
            except Exception:
                continue
        if copied:
            logger.info("collected %d crash report(s) for run %s", copied, run_id)
    except Exception as e:
        logger.debug("crash-report collection failed: %s", e)


def _persist_run_finish(run_id: Optional[str], started: datetime,
                        out=None, error: Optional[str] = None) -> None:
    if not run_id:
        return
    from automation.database.config import SessionLocal
    from automation.database import database
    try:
        ok = bool(out and out.ok) and not error
        status = "passed" if ok else "failed"
        now = datetime.utcnow()
        # Did the app crash during the run? The runner tags a crashed step's detail.
        crashed = bool(out and any(
            ("APP BUG" in (getattr(r, "detail", "") or "") or "CRASHED" in (getattr(r, "action", "") or "")
             or "red-boxed" in (getattr(r, "detail", "") or "") or "terminated" in (getattr(r, "detail", "") or ""))
            for r in getattr(out, "results", [])))
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
            _collect_crash_reports(run_id)
        # A quick PM-friendly summary (uses the fast non-LLM fallback when Ollama is off).
        try:
            from automation.ai.services.summary import test_summary_generator
            with SessionLocal() as db:
                test_summary_generator.generate_run_summary(run_id, db)
        except Exception:
            pass
    except Exception as e:
        logger.warning("scenario persist-finish failed: %s", e)


def _scenario_events(
    req: ScenarioRequest, bundle_id: str, steps: List[str], repo_path: str
) -> Generator[str, None, None]:
    """Drive the scenario, emitting an SSE event as each step actually happens.

    Same work as ``/run`` — it just narrates instead of going silent for a minute.
    """
    from appium import webdriver
    from automation.intelligence.scenario_runner import ScenarioResult, ScenarioRunner
    from automation.projects.builder import app_builder

    driver = None
    # Persist this run so it appears in Dashboard/Reports like every other run.
    run_started = datetime.utcnow()
    run_id = _persist_run_start(req)
    if run_id:
        yield _sse({"type": "run", "run_id": run_id})
    try:
        shot_dir = os.path.join(repo_path, "reports", "scenario")
        os.makedirs(shot_dir, exist_ok=True)

        # Boot the target simulator ourselves so the run does not silently depend
        # on Appium's implicit boot — and so the device visibly opens.
        yield _sse({"type": "phase", "message": f"Booting simulator {req.device_id[:8]}…"})
        boot_ok, boot_msg = app_builder.ensure_ios_booted(req.device_id)
        if not boot_ok:
            _persist_run_finish(run_id, run_started, error=boot_msg)
            yield _sse({"type": "error", "detail": boot_msg})
            return
        yield _sse({"type": "phase", "message": boot_msg})

        # Staging updates daily — pull the latest build for this environment and
        # build+install it before running (same bundle id replaces what's there).
        if req.prepare:
            yield _sse({"type": "phase", "message": "Preparing environment: pulling latest, building & installing (this can take a few minutes)…"})
            from automation.projects.preparation import preparation_service
            prep = preparation_service.prepare_for_execution(req.project_id, device_id=req.device_id)
            if not prep.ok:
                yield _sse({"type": "error", "detail": f"Environment prepare failed: {prep.error}"})
                return
            yield _sse({"type": "phase", "message": "Environment ready (latest build installed)."})

        yield _sse({"type": "phase", "message": "Starting the JS bundler (Metro)…"})
        metro_ok, metro_msg = app_builder.ensure_metro(repo_path, udid=req.device_id, bundle_id=bundle_id)
        if not metro_ok:
            _persist_run_finish(run_id, run_started, error=metro_msg)
            yield _sse({
                "type": "error",
                "detail": f"{metro_msg} Without it the app opens on the red "
                          f"'No bundle URL present' screen and no step can run.",
            })
            return
        yield _sse({"type": "phase", "message": metro_msg})

        # Appium must be healthy. If it is wedged (stuck WebDriverAgent — what makes
        # "the device stop working"), self-heal by restarting it, and SAY SO so a
        # ~30s recovery never looks like a hang.
        yield _sse({"type": "phase", "message": "Checking the automation engine (Appium)…"})
        appium_ok, appium_msg = app_builder.ensure_appium(req.appium_url)
        yield _sse({"type": "phase", "message": appium_msg})
        if not appium_ok:
            _persist_run_finish(run_id, run_started, error=appium_msg)
            yield _sse({"type": "error", "detail": appium_msg})
            return

        yield _sse({"type": "phase", "message": f"Connecting to {req.device_id} (building WebDriverAgent, ~30s on the first run — this is not a hang)…"})
        driver = webdriver.Remote(req.appium_url, options=_appium_options(req.device_id, bundle_id))
        _apply_speed_settings(driver)

        yield _sse({"type": "phase", "message": "Launching the app…"})
        try:
            driver.terminate_app(bundle_id)
        except Exception:
            pass
        driver.activate_app(bundle_id)
        import time as _t
        _t.sleep(3)   # app-launch settle (was 6s)

        runner = ScenarioRunner(driver, bundle_id, screenshot_dir=shot_dir)
        out = ScenarioResult()
        total = len(steps)

        def emit(res, i: int, total: int):
            return _sse({
                "type": "step",
                "index": i,
                "total": total,
                "step": res.step,
                "ok": res.ok,
                "action": res.action,
                "detail": res.detail,
                "healed": getattr(res, "healed", False),
                "healed_note": getattr(res, "healed_note", ""),
                "screenshot": (
                    os.path.relpath(res.screenshot, repo_path) if res.screenshot else None
                ),
            })

        for i, step in enumerate(steps):
            # Announce BEFORE running: a step can take many seconds, and this is
            # the line the user watches to know what is happening right now.
            yield _sse({"type": "step_start", "index": i, "total": total, "step": step})

            step_started = _t.time()
            res = runner.run_one(step, i)
            out.results.append(res)
            _persist_step(run_id, len(out.results) - 1, res, _t.time() - step_started)
            yield emit(res, i, total)

            # A "book a date" popup can intercept an add-to-cart. Take the
            # booking offer automatically rather than failing the run — but never
            # tap `orderLater`, which discards the item and leaves an empty cart.
            # Skip the auto-take when the user's NEXT step handles the popup itself
            # (e.g. "choose order later"), so their explicit choice wins.
            nxt = steps[i + 1].lower() if i + 1 < len(steps) else ""
            if not re.search(r"order\s*later|\blater\b|book\s*a\s*date", nxt):
                popup = runner.handle_book_popup()
                if popup is not None:
                    out.results.append(popup)
                    _persist_step(run_id, len(out.results) - 1, popup)
                    yield emit(popup, i, total)

        out.script = runner.build_script(out)

        saved_path = None
        if req.save:
            name = _slug(req.name)
            saved_path = _save_scenario(
                os.path.join(repo_path, "e2e"), name,
                runner.build_script(out, test_name=name),
            )

        # The technical-debt report: which elements the framework had to name
        # for itself, how it found them, and how sure it was.
        report = runner.catalog.report()
        yield _sse({"type": "report", "report": report, "text": runner.catalog.render()})

        # Finalize the saved run (status + duration + report) so it shows in
        # Dashboard/Reports with a pass/fail verdict.
        _persist_run_finish(run_id, run_started, out=out)

        yield _sse({
            "type": "done",
            "ok": out.ok,
            "passed": out.passed,
            "total": len(out.results),
            "healed": sum(1 for r in out.results if getattr(r, "healed", False)),
            "saved_to": (os.path.relpath(saved_path, repo_path) if saved_path else None),
            "script": out.script,
            "report": report,
            "run_id": run_id,
        })

    except Exception as e:
        logger.exception("Scenario stream failed")
        _persist_run_finish(run_id, run_started, error=f"Scenario run failed: {e}")
        yield _sse({"type": "error", "detail": f"Scenario run failed: {e}"})
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def run_scenario_headless(req: ScenarioRequest, db: Session) -> Dict[str, Any]:
    """Run a scenario with no HTTP stream and return an outcome dict. Reuses the
    exact same driving logic as the live run (via _scenario_events), so autonomous
    callers (PR auto-test) behave identically to the UI. Never raises."""
    try:
        bundle_id, steps, repo_path = _resolve_run(req, db)
    except HTTPException as e:
        return {"ok": False, "passed": 0, "total": 0, "error": e.detail, "steps": []}

    passed = total = 0
    ok: Optional[bool] = None
    error: Optional[str] = None
    step_results: List[Dict[str, Any]] = []
    for frame in _scenario_events(req, bundle_id, steps, repo_path):
        if not frame.startswith("data: "):
            continue
        try:
            ev = json.loads(frame[6:])
        except Exception:
            continue
        if ev.get("type") == "step":
            step_results.append({"step": ev.get("step"), "ok": ev.get("ok"), "detail": ev.get("detail")})
        elif ev.get("type") == "done":
            ok, passed, total = ev.get("ok"), ev.get("passed", 0), ev.get("total", 0)
        elif ev.get("type") == "error":
            error = ev.get("detail")
    return {"ok": bool(ok), "passed": passed, "total": total, "error": error, "steps": step_results}


class BatchScenario(BaseModel):
    name: str
    steps: List[str]


class BatchRequest(BaseModel):
    project_id: str
    device_id: str
    bundle_id: Optional[str] = None
    appium_url: str = "http://127.0.0.1:4723"
    prepare: bool = False
    scenarios: List[BatchScenario]


def _batch_events(req: BatchRequest, bundle_id: str, repo_path: str) -> Generator[str, None, None]:
    """Run MANY scenarios against ONE Appium session — the session setup (WDA
    connect + app launch, ~10-15s) is paid once instead of per scenario. Between
    scenarios the app is relaunched for a clean start."""
    from appium import webdriver
    from automation.intelligence.scenario_runner import ScenarioResult, ScenarioRunner
    from automation.projects.builder import app_builder

    driver = None
    try:
        shot_dir = os.path.join(repo_path, "reports", "scenario")
        os.makedirs(shot_dir, exist_ok=True)

        yield _sse({"type": "phase", "message": f"Booting simulator {req.device_id[:8]}…"})
        boot_ok, boot_msg = app_builder.ensure_ios_booted(req.device_id)
        if not boot_ok:
            yield _sse({"type": "error", "detail": boot_msg}); return

        if req.prepare:
            yield _sse({"type": "phase", "message": "Preparing environment (pull + build + install)…"})
            from automation.projects.preparation import preparation_service
            prep = preparation_service.prepare_for_execution(req.project_id, device_id=req.device_id)
            if not prep.ok:
                yield _sse({"type": "error", "detail": f"Prepare failed: {prep.error}"}); return

        yield _sse({"type": "phase", "message": "Starting Metro…"})
        metro_ok, metro_msg = app_builder.ensure_metro(repo_path, udid=req.device_id, bundle_id=bundle_id)
        if not metro_ok:
            yield _sse({"type": "error", "detail": metro_msg}); return

        yield _sse({"type": "phase", "message": "Checking the automation engine (Appium)…"})
        appium_ok, appium_msg = app_builder.ensure_appium(req.appium_url)
        yield _sse({"type": "phase", "message": appium_msg})
        if not appium_ok:
            yield _sse({"type": "error", "detail": appium_msg}); return

        yield _sse({"type": "phase", "message": "Connecting once for all scenarios (building WebDriverAgent ~30s on first run — not a hang)…"})
        driver = webdriver.Remote(req.appium_url, options=_appium_options(req.device_id, bundle_id))
        _apply_speed_settings(driver)

        results = []
        total_sc = len(req.scenarios)
        for si, sc in enumerate(req.scenarios):
            yield _sse({"type": "scenario_start", "index": si, "total": total_sc, "name": sc.name})
            # Relaunch the app for a clean start per scenario (same session).
            try:
                driver.terminate_app(bundle_id)
            except Exception:
                pass
            driver.activate_app(bundle_id)
            import time as _t
            _t.sleep(3)

            runner = ScenarioRunner(driver, bundle_id, screenshot_dir=shot_dir)
            out = ScenarioResult()
            steps = [s for s in sc.steps if s.strip()]
            for i, step in enumerate(steps):
                res = runner.run_one(step, i)
                out.results.append(res)
                yield _sse({"type": "step", "scenario_index": si, "index": i, "total": len(steps),
                            "step": res.step, "ok": res.ok, "action": res.action, "detail": res.detail,
                            "healed": getattr(res, "healed", False)})
                popup = runner.handle_book_popup()
                if popup is not None:
                    out.results.append(popup)
            sc_result = {"name": sc.name, "ok": out.ok, "passed": out.passed, "total": len(out.results),
                         "healed": sum(1 for r in out.results if getattr(r, "healed", False))}
            results.append(sc_result)
            yield _sse({"type": "scenario_done", "index": si, **sc_result})

        passed = sum(1 for r in results if r["ok"])
        yield _sse({"type": "done", "scenarios": results, "passed": passed, "total": len(results)})

    except Exception as e:
        logger.exception("Batch scenario run failed")
        yield _sse({"type": "error", "detail": f"Batch run failed: {e}"})
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


@router.post("/run-batch/stream", dependencies=[Depends(get_current_user)])
def run_scenarios_batch(req: BatchRequest, db: Session = Depends(get_db)):
    """Run several scenarios against ONE reused Appium session (much faster than
    N separate runs). Streams scenario_start / step / scenario_done / done."""
    project = db.query(TestProject).filter(TestProject.id == req.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    bundle_id = req.bundle_id or project.app_bundle_id
    if not bundle_id:
        raise HTTPException(status_code=400, detail="No app bundle id for this environment.")
    if not req.scenarios:
        raise HTTPException(status_code=400, detail="No scenarios to run.")
    repo_path = repository_manager.get_repo_path(req.project_id)
    return StreamingResponse(
        _batch_events(req, bundle_id, repo_path),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/run/stream", dependencies=[Depends(get_current_user)])
def run_scenario_stream(req: ScenarioRequest, db: Session = Depends(get_db)):
    """Run a scenario, reporting each step live over SSE."""
    bundle_id, steps, repo_path = _resolve_run(req, db)
    return StreamingResponse(
        _scenario_events(req, bundle_id, steps, repo_path),
        media_type="text/event-stream",
        # Proxies buffer text/event-stream by default, which would defeat the
        # whole point by delivering every event at the end.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/run", dependencies=[Depends(get_current_user)])
def run_scenario(req: ScenarioRequest, db: Session = Depends(get_db)):
    from appium import webdriver
    from automation.intelligence.scenario_runner import ScenarioRunner

    bundle_id, steps, repo_path = _resolve_run(req, db)
    shot_dir = os.path.join(repo_path, "reports", "scenario")
    os.makedirs(shot_dir, exist_ok=True)

    # A Debug React Native build does not embed its JS — it fetches the bundle
    # from Metro when the app launches. With no Metro the app comes up on the red
    # "No bundle URL present" screen and every step below drives a dead app.
    # No-op for native projects and when Metro is already up.
    from automation.projects.builder import app_builder

    metro_ok, metro_msg = app_builder.ensure_metro(repo_path)
    logger.info("Metro: %s", metro_msg)
    if not metro_ok:
        # Unlike the build pipeline, a scenario run has nothing left to do
        # without a working app — fail fast instead of reporting N dead steps.
        raise HTTPException(
            status_code=503,
            detail=f"{metro_msg} Without it the app opens on the red "
                   f"'No bundle URL present' screen and no step can run.",
        )

    driver = None
    try:
        driver = webdriver.Remote(req.appium_url, options=_appium_options(req.device_id, bundle_id))
        _apply_speed_settings(driver)
        # clean start from the app's launch screen
        try:
            driver.terminate_app(bundle_id)
        except Exception:
            pass
        driver.activate_app(bundle_id)
        import time as _t
        _t.sleep(3)   # app-launch settle (was 6s)

        runner = ScenarioRunner(driver, bundle_id, screenshot_dir=shot_dir)
        result = runner.run(steps)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Scenario run failed: {e}")
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    saved_path = None
    if req.save:
        name = _slug(req.name)
        saved_path = _save_scenario(
            os.path.join(repo_path, "e2e"), name,
            runner.build_script(result, test_name=name),
        )

    return {
        "ok": result.ok,
        "passed": result.passed,
        "total": len(result.results),
        "saved_to": (os.path.relpath(saved_path, repo_path) if saved_path else None),
        "script": result.script,
        "steps": [
            {
                "step": r.step, "ok": r.ok, "action": r.action, "detail": r.detail,
                "screenshot": (os.path.relpath(r.screenshot, repo_path) if r.screenshot else None),
            }
            for r in result.results
        ],
    }
