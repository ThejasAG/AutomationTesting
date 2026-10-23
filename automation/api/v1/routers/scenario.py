"""Scenario runner API — plain-language steps drive the app and record a script.

POST a list of directions; the engine drives the running app, resolving each step
against the live screen, saves the generated pytest file into the project's e2e/,
and returns a per-step report.

HTTP only. The execution itself lives in automation/scenarios/service.py so the
agent can run the same code without importing FastAPI (Phase 4F.4); importing
run_records here is what gives a backend-local run its TestRun rows.
"""

import logging
import os
from typing import Any, Dict, Generator, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from automation.auth.security import get_current_user
from automation.database.config import get_db
from automation.database.models import TestProject
from automation.projects.repository import repository_manager
from automation.scenarios import run_records  # noqa: F401 — registers run persistence
from automation.scenarios.service import (ScenarioError, ScenarioRequest, _app_missing_detail,
                                          _appium_options, _apply_speed_settings, _save_scenario,
                                          _slug, _sse, resolve_run, scenario_events)

logger = logging.getLogger("scenario")
router = APIRouter(prefix="/scenario", tags=["Scenario Runner"])


def _resolve_run(req: ScenarioRequest, db: Optional[Session] = None):
    """HTTP translation of the service's start-up validation. The status codes and
    messages are the service's, unchanged — this only changes the exception type."""
    try:
        return resolve_run(req, db)
    except ScenarioError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


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

        missing = _app_missing_detail(req.device_id, bundle_id)
        if missing:
            yield _sse({"type": "error", "detail": missing}); return

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
        scenario_events(req, bundle_id, steps, repo_path),
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
