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
    opts.set_capability("wdaLaunchTimeout", 180000)
    opts.set_capability("usePrebuiltWDA", True)
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
    "snapshotMaxDepth": 40,           # bound the UI-tree walk (default 50) → faster page_source
    "useFirstMatch": True,            # return the first matching element, don't collect all
    "customSnapshotTimeout": 3,       # cap how long a snapshot may take
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
    try:
        shot_dir = os.path.join(repo_path, "reports", "scenario")
        os.makedirs(shot_dir, exist_ok=True)

        # Boot the target simulator ourselves so the run does not silently depend
        # on Appium's implicit boot — and so the device visibly opens.
        yield _sse({"type": "phase", "message": f"Booting simulator {req.device_id[:8]}…"})
        boot_ok, boot_msg = app_builder.ensure_ios_booted(req.device_id)
        if not boot_ok:
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
            yield _sse({
                "type": "error",
                "detail": f"{metro_msg} Without it the app opens on the red "
                          f"'No bundle URL present' screen and no step can run.",
            })
            return
        yield _sse({"type": "phase", "message": metro_msg})

        # Appium must be running — the session creation below connects to it. A
        # clear message here beats a cryptic connection-refused stack trace.
        try:
            import urllib.request
            with urllib.request.urlopen(f"{req.appium_url.rstrip('/')}/status", timeout=4):
                pass
        except Exception:
            yield _sse({
                "type": "error",
                "detail": f"Appium server is not reachable at {req.appium_url}. "
                          f"Start it in a terminal with 'appium' and run again.",
            })
            return

        yield _sse({"type": "phase", "message": f"Connecting to {req.device_id} (building WDA can take a minute on first run)…"})
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

            res = runner.run_one(step, i)
            out.results.append(res)
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
            "healed": sum(1 for r in out.results if getattr(r, "healed", False)),
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

        try:
            import urllib.request
            with urllib.request.urlopen(f"{req.appium_url.rstrip('/')}/status", timeout=4):
                pass
        except Exception:
            yield _sse({"type": "error", "detail": f"Appium not reachable at {req.appium_url}. Start it with 'appium'."}); return

        yield _sse({"type": "phase", "message": "Connecting (one session for all scenarios)…"})
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
