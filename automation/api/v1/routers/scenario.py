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
    project_id: str
    steps: List[str]
    device_id: str
    bundle_id: Optional[str] = None      # falls back to the project's stored bundle id
    appium_url: str = "http://127.0.0.1:4723"
    name: str = "scenario"
    save: bool = True                     # write the generated .py into e2e/


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
    opts.set_capability("wdaLaunchTimeout", 180000)
    opts.set_capability("usePrebuiltWDA", True)
    return opts


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


def _scenario_events(
    req: ScenarioRequest, bundle_id: str, steps: List[str], repo_path: str
) -> Generator[str, None, None]:
    """Drive the scenario, emitting an SSE event as each step actually happens.

    Same work as ``/run`` — it just narrates instead of going silent for a minute.
    """
    from appium import webdriver
    from automation.intelligence.scenario_runner import ScenarioResult, ScenarioRunner
    from automation.projects.builder import app_builder

    shot_dir = os.path.join(repo_path, "reports", "scenario")
    os.makedirs(shot_dir, exist_ok=True)

    driver = None
    try:
        yield _sse({"type": "phase", "message": "Starting the JS bundler (Metro)…"})
        metro_ok, metro_msg = app_builder.ensure_metro(repo_path)
        if not metro_ok:
            yield _sse({
                "type": "error",
                "detail": f"{metro_msg} Without it the app opens on the red "
                          f"'No bundle URL present' screen and no step can run.",
            })
            return
        yield _sse({"type": "phase", "message": metro_msg})

        yield _sse({"type": "phase", "message": f"Connecting to {req.device_id}…"})
        driver = webdriver.Remote(req.appium_url, options=_appium_options(req.device_id, bundle_id))

        yield _sse({"type": "phase", "message": "Launching the app…"})
        try:
            driver.terminate_app(bundle_id)
        except Exception:
            pass
        driver.activate_app(bundle_id)
        import time as _t
        _t.sleep(6)

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
                "screenshot": (
                    os.path.relpath(res.screenshot, repo_path) if res.screenshot else None
                ),
            })

        for i, step in enumerate(steps):
            # Announce BEFORE running: a step can take many seconds, and this is
            # the line the user watches to know what is happening right now.
            yield _sse({"type": "step_start", "index": i, "total": total, "step": step})

            res = runner.run_one(step, i)
            out.results.append(res)
            yield emit(res, i, total)

            # A "book a date" popup can intercept an add-to-cart. Take the
            # booking offer automatically rather than failing the run — but never
            # tap `orderLater`, which discards the item and leaves an empty cart.
            popup = runner.handle_book_popup()
            if popup is not None:
                out.results.append(popup)
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

        yield _sse({
            "type": "done",
            "ok": out.ok,
            "passed": out.passed,
            "total": len(out.results),
            "saved_to": (os.path.relpath(saved_path, repo_path) if saved_path else None),
            "script": out.script,
            "report": report,
        })

    except Exception as e:
        logger.exception("Scenario stream failed")
        yield _sse({"type": "error", "detail": f"Scenario run failed: {e}"})
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


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
        # clean start from the app's launch screen
        try:
            driver.terminate_app(bundle_id)
        except Exception:
            pass
        driver.activate_app(bundle_id)
        import time as _t
        _t.sleep(6)

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
