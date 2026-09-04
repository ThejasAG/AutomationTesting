"""Scenario execution — the engine that drives plain-language steps on a device.

Extracted from the scenario router (Phase 4F.4) so the two callers that matter
can share it without sharing a transport:

    agent (no FastAPI, no database)  -+
                                      +-> this module
    backend router (HTTP adapter)    -+

Nothing here imports FastAPI, SQLAlchemy or the database layer. The two things
that DO need a database on the backend - TestRun bookkeeping and ScenarioResult
rows - are injected via set_run_store(); see automation/scenarios/run_records.py.
The agent injects only a result sink (Phase 4F.3) and passes its job_id as the
run id (Phase 4F.4a), so neither hook is ever set in the agent process.
"""

import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, Generator, List, Optional

from pydantic import BaseModel

from automation.projects.repository import repository_manager

logger = logging.getLogger("scenario")


class ScenarioError(Exception):
    """A scenario could not be started. Carries the status the HTTP API has always
    returned for this condition, so the router translates rather than re-decides."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class ScenarioRequest(BaseModel):
    project_id: str                       # the ENVIRONMENT to run against (prod/staging project)
    steps: List[str]
    device_id: str
    bundle_id: Optional[str] = None      # falls back to the project's stored bundle id
    env: Optional[str] = None            # 'staging' | 'prod' — translates bundle_id to that env's build
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


# -- Backend-local persistence, injected -------------------------------------
# TestRun bookkeeping and ScenarioResult rows need a database; execution does
# not. The backend registers a store so this module imports nothing from the
# database layer, and the agent - which runs this exact code on the machine
# holding the simulator - registers nothing and opens no session.
_run_store = None


def set_run_store(store) -> None:
    """Register backend-local run/step persistence (see scenarios/run_records.py)."""
    global _run_store
    _run_store = store


def resolve_run(req: "ScenarioRequest", db=None):
    """Validate the request and resolve what execution needs.

    Shared by both endpoints, and called BEFORE the streaming generator starts -
    the generator outlives the request handler, so it must never touch the
    session itself.

    *db* is optional. The only thing the project row supplies here is
    app_bundle_id, so a caller that already knows it - the agent, from its job
    payload - passes bundle_id and no session, and nothing looks up a project.
    Backend callers keep passing a session and behave exactly as before.
    """
    bundle_id = req.bundle_id
    if db is not None and _run_store is not None:
        bundle_id = bundle_id or _run_store.project_bundle_id(db, req.project_id)
    if bundle_id and req.env:
        from automation.scenarios.cross_app_flows import bundle_for_env
        bundle_id = bundle_for_env(bundle_id, req.env)
    if not bundle_id:
        raise ScenarioError(
            400, "No app bundle id - set one on the project or pass bundle_id.")

    steps = [s for s in req.steps if s.strip()]
    if not steps:
        raise ScenarioError(400, "No scenario steps provided.")

    repo_path = repository_manager.get_repo_path(req.project_id)
    return bundle_id, steps, repo_path


def _app_missing_detail(device_id: str, bundle_id: str) -> Optional[str]:
    """None if the app is on the device, else a message that actually explains it.

    Appium's own error for this is "App with bundle identifier '…' unknown", which
    reads like a corrupt install or a driver fault. The real cause is almost always
    a mismatched pair — the prod Business app only exists on the iPad, the staging
    one on the phone — so name what IS there and what to do about it.
    """
    from automation.projects.builder import app_builder
    if app_builder.is_installed(device_id, bundle_id):
        return None
    # Apple's own apps and Appium's runner are not answers to "which app can I run?"
    present = [b for b in app_builder.installed_bundles(device_id)
               if not b.startswith("com.apple.")
               and "WebDriverAgent" not in b]
    return (f"The app for this environment ({bundle_id}) is not installed on this "
            f"simulator. Installed here: {', '.join(present) or 'no third-party apps'}. "
            f"Pick an environment whose app is on this device, or install it first "
            f"(Latest build → tick the app + this device).")


# Where scenario step results go. The backend leaves this None and writes to the
# database directly. The AGENT sets it, so the same execution — which correctly
# stays on the machine holding the simulator — reports its steps over HTTP instead
# of opening a session. Nothing about how a scenario is driven changes.
_result_sink = None


def set_result_sink(sink) -> None:
    """Route step results somewhere other than this process's database."""
    global _result_sink
    _result_sink = sink


def _persist_step(run_id: Optional[str], index: int, res, secs: Optional[float] = None) -> None:
    if not run_id:
        return
    # The agent's sink wins; the backend falls back to its own database writer.
    sink = _result_sink or (_run_store.step if _run_store is not None else None)
    if sink is None:
        return
    try:
        sink(run_id, index, res, secs)
    except Exception as e:                       # reporting must never fail a run
        logger.warning("scenario step report failed: %s", e)


def _finish(run_id: Optional[str], started: datetime, out=None,
            error: Optional[str] = None) -> None:
    """Finalize a run WE created. No-op for a caller-owned run id (Phase 4F.4a)."""
    if run_id and _run_store is not None:
        _run_store.finish(run_id, started, out=out, error=error)


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


def _crashed(out) -> bool:
    """Did the app crash during the run? The runner tags a crashed step's detail."""
    return bool(out and any(
        ("APP BUG" in (getattr(r, "detail", "") or "") or "CRASHED" in (getattr(r, "action", "") or "")
         or "red-boxed" in (getattr(r, "detail", "") or "") or "terminated" in (getattr(r, "detail", "") or ""))
        for r in getattr(out, "results", [])))


def scenario_events(
    req: ScenarioRequest, bundle_id: str, steps: List[str], repo_path: str,
    run_id: Optional[str] = None,
) -> Generator[str, None, None]:
    """Drive the scenario, emitting an SSE event as each step actually happens.

    Same work as ``/run`` — it just narrates instead of going silent for a minute.
    """
    from appium import webdriver
    from automation.intelligence.scenario_runner import ScenarioResult, ScenarioRunner
    from automation.projects.builder import app_builder

    driver = None
    run_started = datetime.utcnow()
    # A caller that already owns a TestRun (the agent, whose job IS the run) passes
    # its id in. Reuse that identity rather than minting a second one, and leave the
    # lifecycle to that caller — it reports finish over the authenticated status API.
    # With no run_id we are a backend-local caller (Scenarios tab, tickets, workflow)
    # that has no run record of its own, so the backend store makes one for it.
    persist_id: Optional[str] = None
    if run_id is None and _run_store is not None:
        run_id = _run_store.start(req)
        persist_id = run_id
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
            _finish(persist_id, run_started, error=boot_msg)
            yield _sse({"type": "error", "detail": boot_msg})
            return
        yield _sse({"type": "phase", "message": boot_msg})

        # Fail here, not 40s later inside WebDriverAgent. Booting, starting Metro and
        # building WDA all succeed against a device that does not have the app — the
        # run only dies at session-create, by which point the log looks like an Appium
        # problem rather than the wrong app/device pair that it is.
        missing = _app_missing_detail(req.device_id, bundle_id)
        if missing:
            yield _sse({"type": "error", "detail": missing})
            _finish(persist_id, run_started, error=missing)
            return

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
            _finish(persist_id, run_started, error=metro_msg)
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
            _finish(persist_id, run_started, error=appium_msg)
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
        crashed = _crashed(out)
        _finish(persist_id, run_started, out=out)
        if persist_id is None and crashed:
            # The backend store would have done this; it is filesystem-only and
            # belongs on the machine that ran the app, so do it here regardless.
            _collect_crash_reports(run_id)

        yield _sse({
            "type": "done",
            "crash_detected": crashed,
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
        _finish(persist_id, run_started, error=f"Scenario run failed: {e}")
        yield _sse({"type": "error", "detail": f"Scenario run failed: {e}"})
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def run_scenario_headless(req: ScenarioRequest, db=None,
                          run_id: Optional[str] = None) -> Dict[str, Any]:
    """Run a scenario with no HTTP stream and return an outcome dict. Reuses the
    exact same driving logic as the live run (via _scenario_events), so autonomous
    callers (PR auto-test) behave identically to the UI. Never raises."""
    try:
        bundle_id, steps, repo_path = resolve_run(req, db)
    except ScenarioError as e:
        return {"ok": False, "passed": 0, "total": 0, "error": e.detail, "steps": [],
                "crash_detected": False}

    passed = total = 0
    ok: Optional[bool] = None
    error: Optional[str] = None
    step_results: List[Dict[str, Any]] = []
    crashed = False
    for frame in scenario_events(req, bundle_id, steps, repo_path, run_id=run_id):
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
            crashed = bool(ev.get("crash_detected"))
        elif ev.get("type") == "error":
            error = ev.get("detail")
    return {"ok": bool(ok), "passed": passed, "total": total, "error": error,
            "steps": step_results, "crash_detected": crashed}
