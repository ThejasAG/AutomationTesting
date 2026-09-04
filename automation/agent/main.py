import os
import re
import sys
import time
import socket
import platform
import threading
import requests
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

from dotenv import load_dotenv

# Load .env before the automation.* imports below — they read os.getenv at
# import time, so a later load would have no effect on them.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# Device tooling (adb for Android, xcrun/simctl for iOS) and Appium are expected
# to be on the agent host's PATH. Configure PATH via the environment/shell rather
# than hardcoding machine-specific SDK locations here.

from automation.device_manager.discovery.android import AndroidDiscoveryProvider
from automation.device_manager.discovery.ios import IOSDiscoveryProvider
from automation.plugins.appium_framework import AppiumFramework
from automation.utils import proctree
from automation.device_manager import reservation
from automation.device_manager.service import device_row_id
from automation.projects.repository import repository_manager
from automation.projects.preparation import preparation_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Agent")

PLATFORM_URL = os.getenv("PLATFORM_URL", "http://localhost:8000/api/v1")

# One session for every platform call, so the agent token is attached in ONE
# place instead of on each of the ~10 call sites (where the next one added
# would silently forget it).
AGENT_TOKEN = os.getenv("AGENT_TOKEN", "").strip()
HTTP = requests.Session()
if AGENT_TOKEN:
    HTTP.headers["X-Agent-Token"] = AGENT_TOKEN
AGENT_ID = None

# Job ids this agent process has already executed — a duplicate dispatch of the
# same id is ignored rather than re-run.
_PROCESSED_JOB_IDS: set = set()

def get_capabilities() -> Dict[str, Any]:
    return {
        "os": platform.system(),
        "python_version": sys.version.split()[0],
        "frameworks": ["appium"],
    }

# Virtual device injected when no real device is connected. This allows the full
# job-dispatch and agent-execution pipeline to be tested without physical hardware
# or an emulator/simulator. The fallback is chosen to match the host OS: a macOS
# host implies an iOS toolchain, everything else defaults to Android.
VIRTUAL_ANDROID_DEVICE = {
    "id": "virtual-android-emulator",
    "name": "Virtual Android Emulator",
    "manufacturer": "Google",
    "model": "Pixel 6 (Virtual)",
    "platform": "Android",
    "platform_version": "13.0"
}

VIRTUAL_IOS_DEVICE = {
    "id": "virtual-ios-simulator",
    "name": "Virtual iOS Simulator",
    "manufacturer": "Apple",
    "model": "iPhone 16 Pro (Virtual)",
    "platform": "iOS",
    "platform_version": "18.3"
}


def agent_slug() -> str:
    """Stable per-machine identifier, used to keep synthetic device ids unique.

    The hostname — not the agent id — because the id is minted fresh by
    /agents/register on every start, and because discover_devices() runs BEFORE
    registration, when no agent id exists yet. The hostname is available at that
    point and is the same across restarts, which is exactly what a device id
    needs to be.
    """
    host = socket.gethostname().split(".")[0]
    slug = re.sub(r"[^a-z0-9-]+", "-", host.lower()).strip("-")
    return slug or "unknown-host"


def get_virtual_device() -> Dict[str, Any]:
    """An OS-appropriate virtual device for pipeline testing, unique to this host.

    The id used to be the constant "virtual-ios-simulator". Every agent with no
    real device reported that same id, so two machines would advertise one
    identity and a job queued for it would go to whichever polled first. The
    host slug makes the id collision-free while staying stable per machine.

    ponytail: keyed on hostname, so two agents on ONE host still collide — they
    would also both claim the same real simulators. Give them explicit ids if
    that ever becomes a real configuration.
    """
    base = VIRTUAL_IOS_DEVICE if platform.system() == "Darwin" else VIRTUAL_ANDROID_DEVICE
    device = dict(base)                      # copy: never mutate the template
    device["id"] = f"{base['id']}-{agent_slug()}"
    return device

def discover_devices() -> List[Dict[str, Any]]:
    providers = [AndroidDiscoveryProvider(), IOSDiscoveryProvider()]
    devices = []
    for p in providers:
        try:
            for d in p.discover_devices():
                devices.append({
                    "id": d.id,
                    "name": d.name,
                    "manufacturer": d.manufacturer,
                    "model": d.model,
                    "platform": d.platform,
                    "platform_version": d.platform_version
                })
        except Exception as e:
            logger.warning(f"Device discovery failed for {p.get_provider_name()}: {e}")

    # Fallback: inject a virtual device so the platform can dispatch jobs
    # even when no physical/emulator device is connected via ADB.
    if not devices:
        logger.info("No real devices found — reporting virtual device for pipeline testing.")
        devices.append(get_virtual_device())

    return devices

def heartbeat_loop():
    global AGENT_ID
    while True:
        try:
            if AGENT_ID:
                devices = discover_devices()
                HTTP.post(
                    f"{PLATFORM_URL}/agents/{AGENT_ID}/heartbeat",
                    json={"status": "online", "connected_devices": devices},
                    timeout=5
                )
        except Exception as e:
            logger.error(f"Heartbeat failed: {e}")
        time.sleep(10)

def report_status(job_id: str, status: str, logs: List[str] = [], timeline_event: str = None, error_message: str = None):
    try:
        HTTP.post(
            f"{PLATFORM_URL}/jobs/{job_id}/status",
            json={
                "status": status,
                "logs": logs,
                "timeline_event": timeline_event,
                "error_message": error_message
            },
            timeout=5
        )
    except Exception as e:
        logger.error(f"Failed to report status: {e}")

def _upload_evidence_with_retry(job_id: str, evidence_data: dict, max_retries: int = 3):
    """Upload evidence with bounded retry on network failure."""
    import time as _time
    for attempt in range(1, max_retries + 1):
        try:
            res = HTTP.post(
                f"{PLATFORM_URL}/jobs/{job_id}/evidence",
                json={"evidence": evidence_data},
                timeout=15
            )
            if res.status_code == 200:
                return
            logger.warning(f"Evidence upload attempt {attempt} failed (HTTP {res.status_code})")
        except Exception as e:
            logger.warning(f"Evidence upload attempt {attempt} error: {e}")
        if attempt < max_retries:
            _time.sleep(2 ** attempt)  # exponential back-off
    logger.error(f"Evidence upload failed after {max_retries} attempts for job {job_id}")


# ── Screen streaming helpers ─────────────────────────────────────────────────

def _post_frame(job_id: str, png_bytes: bytes) -> None:
    """Upload a single PNG frame to the backend streaming endpoint.

    Called from the capture thread.  All exceptions are swallowed so that
    network hiccups never crash the capture loop or the parent job thread.
    """
    try:
        HTTP.post(
            f"{PLATFORM_URL}/jobs/{job_id}/stream/frame",
            data=png_bytes,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Agent-Secret": os.getenv("AGENT_SECRET", ""),
            },
            timeout=4,
        )
    except Exception as e:
        logger.warning(f"Frame upload failed for job {job_id}: {e}")


def _signal_stream_ended(job_id: str) -> None:
    """Send an empty-body POST to tell the backend the stream is finished."""
    try:
        HTTP.post(
            f"{PLATFORM_URL}/jobs/{job_id}/stream/frame",
            data=b"",
            headers={
                "Content-Type": "application/octet-stream",
                "X-Agent-Secret": os.getenv("AGENT_SECRET", ""),
            },
            timeout=4,
        )
    except Exception as e:
        logger.warning(f"Stream end signal failed for job {job_id}: {e}")


def _start_screen_capture(job_id: str, device_id: str) -> tuple[threading.Thread | None, threading.Event]:
    """Start the screen-capture daemon thread.

    Returns ``(thread, stop_event)``.  If the streaming module is unavailable
    or initialisation fails, returns ``(None, stop_event)`` — the job
    continues normally without streaming.
    """
    stop_event = threading.Event()
    try:
        from automation.streaming.screen_capture import ScreenCaptureLoop

        def on_frame(png_bytes: bytes) -> None:
            _post_frame(job_id, png_bytes)

        capture_loop = ScreenCaptureLoop(
            device_id=device_id,
            stop_event=stop_event,
            on_frame=on_frame,
            fps=4.0,
        )
        thread = threading.Thread(
            target=capture_loop.start,
            daemon=True,
            name=f"screen-capture-{job_id[:8]}",
        )
        thread.start()
        logger.info(f"Screen capture thread started for job {job_id}")
        return thread, stop_event

    except Exception as e:
        logger.warning(f"Screen capture unavailable for job {job_id}: {e}")
        return None, stop_event


def _stop_screen_capture(
    job_id: str,
    capture_thread: threading.Thread | None,
    stop_event: threading.Event,
) -> None:
    """Stop the capture thread and send the end-of-stream signal."""
    try:
        stop_event.set()
        if capture_thread and capture_thread.is_alive():
            capture_thread.join(timeout=6)
        _signal_stream_ended(job_id)
    except Exception as e:
        logger.warning(f"Screen capture cleanup error for job {job_id}: {e}")


# ── Job execution ─────────────────────────────────────────────────────────────

# A job in any of these states has already been attempted (or is being run
# elsewhere) and must never be executed again. The backend hands out freshly
# claimed jobs as "queued"/"assigned"; anything else is a replay.
ALREADY_ATTEMPTED_STATES = {
    "downloading", "preparing", "running", "collecting_evidence",
    "completed", "passed", "failed", "cancelled", "stopped",
}


def _has_test_suite(project_id: str, config) -> bool:
    """True when the repository actually contains something to execute.

    A freshly-onboarded app often has no Appium suite yet. The platform can
    still build, install and launch it — that is a successful deployment, not a
    failed test run — so we detect the absence of tests rather than letting the
    runner fail on a missing directory.
    """
    import shlex

    repo_path = repository_manager.get_repo_path(project_id)
    command = (config.execution.command or "").strip()
    if not command:
        return False

    parts = shlex.split(command)

    # pytest <path> — the path must exist and contain test_*.py files.
    if parts and parts[0] in ("pytest", "py.test"):
        targets = [p for p in parts[1:] if not p.startswith("-")] or ["tests"]
        for target in targets:
            full = os.path.join(repo_path, target)
            if os.path.isfile(full):
                return True
            if os.path.isdir(full):
                for _root, _dirs, files in os.walk(full):
                    if any(f.startswith("test_") and f.endswith(".py") for f in files):
                        return True
        return False

    # Any other runner (gradle/xcodebuild/npm/mvn) — assume the repo owner knows
    # what they configured; let it run and report its own result.
    return True


def _run_planned_scenarios(job_id: str, project_id: str, device_id: str,
                           planned: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Run the planner's chosen scenarios through the SCENARIO runner.

    The plan selects saved scenarios (plain-language steps), which pytest cannot run —
    they are executed by the same engine the Scenarios page and the autotest path use.
    Returns the shape execute() returns, so the caller treats both paths identically,
    and writes one scenario_results row per scenario so the run shows its steps.
    """
    from automation.database.config import SessionLocal
    from automation.database.models import ScenarioResult
    from automation.api.v1.routers.scenario import ScenarioRequest, run_scenario_headless

    logs: List[str] = []
    passed = 0
    with SessionLocal() as db:
        for i, sc in enumerate(planned, 1):
            name = sc.get("name") or f"scenario {i}"
            try:
                outcome = run_scenario_headless(
                    ScenarioRequest(project_id=project_id, steps=sc.get("steps") or [],
                                    device_id=device_id, name=name, save=False,
                                    prepare=False),
                    db,
                )
            except Exception as e:
                outcome = {"ok": False, "steps": [], "error": str(e)[:300]}
            ok = bool(outcome.get("ok"))
            passed += ok
            reasons = [f"{s.get('step')} — {'ok' if s.get('ok') else 'FAIL'}"
                       for s in (outcome.get("steps") or [])]
            if outcome.get("error"):
                reasons.append(f"[FAIL] {outcome['error']}")
            logs.append(f"{'PASS' if ok else 'FAIL'} {name}")

            row = (db.query(ScenarioResult)
                   .filter_by(run_id=job_id, scenario_num=str(i)).first())
            if row is None:
                row = ScenarioResult(run_id=job_id, scenario_num=str(i))
                db.add(row)
            row.scenario_name = name[:500]
            row.status = "PASS" if ok else "FAIL"
            row.consumer_status = "N/A"
            row.business_status = "N/A"
            row.reasons = reasons
            row.error = None if ok else (outcome.get("error")
                                         or "; ".join(r for r in reasons if "FAIL" in r))[:500]
        db.commit()

    total = len(planned)
    failed = total - passed
    return {
        "status": "passed" if total and not failed else "failed",
        "detail": (f"{passed}/{total} planned scenario(s) passed." if total
                   else "The plan selected no runnable scenarios."),
        "logs": logs, "exit_code": 0 if not failed else 1,
        "tests": total, "failures": failed, "errors": 0, "skipped": 0,
        "attempts": 1, "flaky_detected": False,
        "evidence_dir": None,
    }


# Wipe the app before AND after each PR job, so one PR's run cannot inherit the previous
# one's login, cache or half-finished screen. OFF by default on purpose: a wiped app
# comes back at first-run (onboarding carousel, signed out) and NO consumer scenario
# handles that yet — turning this on today makes every consumer PR test fail the way a
# fresh simulator did. Turn it on once a first-run preamble exists.
FRESH_INSTALL_PER_JOB = os.getenv("FRESH_INSTALL_PER_JOB", "").lower() in ("1", "true", "yes")


def _bundle_id_for(project_id: str, config) -> Optional[str]:
    """The app's bundle id, from automation.yaml's built artifact."""
    try:
        app = getattr(getattr(config, "environment", None), "app", None)
        if app and os.path.exists(app):
            from automation.projects.builder import app_builder
            return app_builder._bundle_id(app)
    except Exception as e:
        logger.warning(f"Could not resolve bundle id: {e}")
    return None


def _wipe_app(device_id: str, bundle_id: Optional[str], platform: str, when: str) -> None:
    """Best-effort uninstall — never fails a job over cleanup."""
    if not bundle_id:
        return
    try:
        from automation.projects.builder import app_builder
        ok, msg = app_builder.uninstall(device_id, bundle_id, platform)
        logger.info(f"Fresh-install ({when}): {msg}")
    except Exception as e:
        logger.warning(f"Uninstall ({when}) failed: {e}")


def run_job(job: Dict[str, Any], connected_devices: List[str]):

    job_id = job["job_id"]

    # Guard against re-processing the same job. This stops the agent from
    # picking up the same job id (e.g. b3e4d933) over and over after it has
    # already run or failed. The backend's atomic claim in /jobs/poll is the
    # primary defence; this is the client-side backstop.
    job_state = job.get("job_state", "queued")
    if job_state in ALREADY_ATTEMPTED_STATES:
        logger.warning(f"Skipping job {job_id} — already attempted (job_state={job_state}).")
        return

    # Track jobs this process has already run, so a duplicate dispatch of the
    # same id within one agent lifetime is never executed twice.
    if job_id in _PROCESSED_JOB_IDS:
        logger.warning(f"Skipping job {job_id} — already processed by this agent.")
        return
    _PROCESSED_JOB_IDS.add(job_id)

    project_id = job["project_id"]
    git_url = job["git_url"]
    branch = job["branch"]
    device_id = job["device_id"]

    if device_id not in connected_devices:
        logger.error(f"Job assigned for device {device_id} but it is no longer connected.")
        report_status(job_id, "failed", ["Device disconnected before execution."], "Failed", "Device disconnected")
        return

    # ── Reserve the simulator BEFORE anything can drive it ───────────────────
    # Claiming the TestRun stops two agents taking the same JOB; it says nothing
    # about two different jobs targeting the same simulator. This does.
    #
    # The row is resolved against THIS agent's machine, which is what makes a
    # legacy job (machine_id NULL) safe: the executing agent supplies the machine,
    # so the same UDID on another Mac is never reserved and nothing is backfilled.
    reserved_device_id = None
    device_row = device_row_id(AGENT_ID, device_id)
    if device_row:
        if not reservation.reserve_device(device_row, job_id):
            # Another run holds this simulator. That is contention, not a test
            # failure — put the job back on the queue and let it be claimed again
            # once the device frees, using the existing status mechanism.
            holder = reservation.get_reservation(device_row)
            owner = holder.reserved_by if holder else "another run"
            logger.warning("Device %s is reserved by %s — requeueing job %s",
                           device_id, owner, job_id)
            report_status(job_id, "queued",
                          [f"Device {device_id} is in use by run {owner}; requeued."],
                          "Waiting for device")
            # Never executed, so this agent may legitimately pick it up again.
            _PROCESSED_JOB_IDS.discard(job_id)
            return
        reserved_device_id = device_row
    else:
        # No registry row for this machine + device. Reservation must never block
        # work that runs today, so proceed unreserved exactly as before 4E.2.
        logger.info("No device record for %s on this machine — running unreserved.", device_id)

    # Screen capture state — declared here so finally block can always reference it.
    capture_thread: threading.Thread | None = None
    stop_capture_event = threading.Event()
    perf_collector = None  # declared here so finally can always stop it
        
    try:
        # 1. Prepare the project. This ALWAYS runs before validation:
        #    clone (if missing) → checkout branch → pull → detect type →
        #    type-aware validation → install dependencies.
        #    The agent never validates a repository that does not exist locally.
        report_status(
            job_id, "downloading",
            [f"Preparing repository {git_url} (branch: {branch})..."],
            "Git Sync",
        )

        # Clean slate for this PR, when enabled — see FRESH_INSTALL_PER_JOB.
        if FRESH_INSTALL_PER_JOB and job.get("is_pr"):
            _cfg = repository_manager.validate_yaml(project_id)
            _wipe_app(device_id, _bundle_id_for(project_id, _cfg) if _cfg else None,
                      (job.get("platform") or "ios"), "before")

        prep = preparation_service.prepare_for_execution(
            project_id,
            device_id=device_id,
            # A missing automation.yaml is scaffolded from the detected type
            # rather than hard-failing the job.
            auto_generate_yaml=True,
            on_step=lambda msg: report_status(job_id, "preparing", [msg]),
            git_url=git_url,
            branch=branch,
            platform=job.get("platform"),
            project_name=job.get("project_name"),
        )

        if not prep.ok:
            details = []
            if prep.validation:
                details = [
                    f"PRE-FLIGHT FAILED: {i.check} — {i.problem}"
                    for i in prep.validation.issues
                ]
            report_status(
                job_id, "failed",
                details or [f"Preparation failed: {prep.error}"],
                "Pre-flight Failed",
                prep.error or "Project preparation failed.",
            )
            return

        logger.info(f"Project {project_id} prepared as type={prep.project_type}")

        config = repository_manager.validate_yaml(project_id)
        if not config:
            raise Exception(
                "automation.yaml is present but invalid. Fix the schema in the repository root."
            )

        framework = AppiumFramework()

        # 2a. Start performance collection (CPU/mem/API sampling) for iOS runs.
        perf_collector = None
        try:
            from automation.performance.collector import PerformanceCollector
            _bundle = None
            try:
                _bundle = getattr(getattr(config, "execution", None), "bundle_id", None) \
                    or getattr(config, "bundle_id", None)
            except Exception:
                _bundle = None
            _metro_log = os.getenv("METRO_LOG_PATH")
            perf_collector = PerformanceCollector(
                device_id=device_id, run_id=job_id,
                bundle_id=_bundle, metro_log_path=_metro_log)
            perf_collector.start()
            if _bundle:
                lt = perf_collector.measure_app_launch_time(_bundle)
                if lt is not None:
                    report_status(job_id, "running", [f"App launch time: {lt:.2f}s"])
        except Exception as _pe:
            logger.warning(f"perf collector start failed: {_pe}")
            perf_collector = None

        # 2. Start screen capture BEFORE executing tests so the browser can
        #    watch from the very first moment the suite launches. By this point
        #    the app is already built, installed and launched, so the stream
        #    shows the running app even if there is no test suite yet.
        capture_thread, stop_capture_event = _start_screen_capture(job_id, device_id)

        # 3. Execute — but only if the repository actually has a test suite.
        #    A repo with no Appium tests is not a failure: the app was deployed
        #    and is running on the device. Reporting that as "failed" makes a
        #    perfectly healthy platform look broken.
        # A PLAN does not need a repo test suite — planned scenarios are platform-side
        # (plain-language steps run by the scenario runner), not pytest files. This check
        # sat BEFORE the plan branch, so a job carrying 10 planned scenarios still
        # short-circuited to "no tests to run" and reported PASSED without executing one.
        if not (job.get("planned_scenarios") or []) and not _has_test_suite(project_id, config):
            report_status(
                job_id, "running",
                ["App built, installed and launched on the device.",
                 "No test suite found in the repository, and no plan for this job — "
                 "nothing to execute.",
                 f"Add an Appium suite and set execution.command (currently: "
                 f"{config.execution.command!r}) to run automated tests."],
                "App Deployed",
            )
            # Let the stream show the running app for a few seconds before we cut it.
            time.sleep(8)
            _stop_screen_capture(job_id, capture_thread, stop_capture_event)
            capture_thread = None
            # NOT "passed". Nothing was verified about the change — the app merely
            # built and launched. Reporting green here means a PR with no runnable
            # tests looks identical to a PR whose tests all passed, which is exactly
            # how empty runs kept showing up as green ticks.
            report_status(
                job_id, "no_tests",
                ["Deployment verified — the app built, installed and launched.",
                 "NO TESTS RAN, so this is not a pass: nothing was verified."],
                "Deployed (not tested)",
                "No test suite and no plan — nothing was executed.",
            )
            return

        # RUN THE PLAN when the job carries one. The planner selects scenarios via the
        # dependency graph and the linked ticket, but the agent used to ignore that
        # entirely and run the project's fixed execution.command — so "smart selection"
        # never reached execution and every PR ran the same suite.
        planned = job.get("planned_scenarios") or []
        # Execution proper starts here: the repo is prepared, the app is built and
        # installed, and the next call brings up Appium/WDA and runs the tests.
        if reserved_device_id:
            reservation.mark_active(reserved_device_id, job_id)

        if planned:
            report_status(job_id, "running",
                          [f"Executing {len(planned)} planned scenario(s) for this change: "
                           + ", ".join(s.get("name", "?") for s in planned)],
                          "Executing")
            exec_res = _run_planned_scenarios(job_id, project_id, device_id, planned)
        else:
            report_status(job_id, "running",
                          ["No plan for this job — running the project's default command."],
                          "Executing")
            # Flaky auto-retry: retries on failure and flags the run if it only passed on a retry.
            exec_res = framework.execute_with_retry(
                project_id, device_id, {"command": config.execution.command}, job_id,
                max_retries=2)

        status = exec_res.get("status", "failed")
        logs = exec_res.get("logs", [])
        attempts = exec_res.get("attempts", 1)
        flaky = bool(exec_res.get("flaky_detected"))
        if attempts > 1:
            logs = list(logs) + [f"Ran {attempts} attempt(s)" + (" — FLAKY (passed on retry)" if flaky else "")]
        # Persist attempts/flaky onto the run so the dashboard can badge it.
        try:
            HTTP.post(
                f"{PLATFORM_URL}/jobs/{job_id}/status",
                json={"status": "running", "attempts": attempts, "flaky_detected": flaky},
                timeout=10,
            )
        except Exception:
            pass

        # 5. Stop capture as soon as pytest exits.
        _stop_screen_capture(job_id, capture_thread, stop_capture_event)
        capture_thread = None  # prevent double-stop in finally
        
        report_status(job_id, "collecting_evidence", logs, f"Execution {status}")

        # 5b. Stop performance collection, persist summary, attach to evidence.
        perf_summary = None
        if perf_collector is not None:
            try:
                perf_collector.stop()          # halt sampling thread
                perf_summary = perf_collector.save_to_db()
                report_status(job_id, "collecting_evidence",
                              [f"Performance: score {perf_summary.get('performance_score')} "
                               f"(grade {perf_summary.get('grade')})"])
            except Exception as _pe:
                logger.warning(f"perf stop/save failed: {_pe}")

        # 6. Upload Evidence
        evidence_data = framework.collect_evidence(project_id, exec_res)
        evidence_data["run_id"] = job_id
        if perf_summary is not None:
            evidence_data["performance"] = perf_summary
        _upload_evidence_with_retry(job_id, evidence_data)

        final_status = "passed" if status == "passed" else "failed"
        # Carry the REASON through. This used to report only "Execution Complete." with no
        # error_message, so a failed job showed a blank cause in the dashboard — a 48-minute
        # run that failed told you nothing about why.
        detail = exec_res.get("detail") or ""
        counts = (f"{exec_res.get('tests', 0)} test(s), "
                  f"{exec_res.get('failures', 0)} failed, "
                  f"{exec_res.get('errors', 0)} errored, "
                  f"{exec_res.get('skipped', 0)} skipped")
        report_status(
            job_id, final_status,
            [f"Execution complete — {counts}." + (f" {detail}" if detail else "")],
            "Done",
            None if final_status == "passed" else (detail or f"Tests failed ({counts})"),
        )
        
    except Exception as e:
        logger.error(f"Job Execution Error: {e}")
        report_status(job_id, "failed", [f"Execution Error: {str(e)}"], "Failed", str(e))
    finally:
        # Always stop capture if it wasn't already stopped cleanly above.
        if capture_thread is not None:
            _stop_screen_capture(job_id, capture_thread, stop_capture_event)
        # Never leak the perf-collector thread on an error path.
        if perf_collector is not None and getattr(perf_collector, "is_collecting", False):
            try:
                perf_collector.stop()
                perf_collector.save_to_db()
            except Exception:
                pass
        try:
            framework.cleanup(project_id)
        except Exception:
            pass
        # Reap anything this job spawned into a tracked process group. The happy
        # path already released the Appium session; this is the backstop for the
        # error paths, where release_instance() is never reached and Appium plus
        # its xcodebuild/WDA tree used to survive the job. Only pids recorded for
        # THIS job_id are touched — see automation/utils/proctree.py.
        try:
            reaped = proctree.reap_job(job_id)
            if reaped:
                logger.info(f"Reaped {reaped} tracked process group(s) for job {job_id}")
        except Exception as e:
            logger.warning(f"Process reap failed for job {job_id}: {e}")
        # Release the simulator LAST — only once Appium, WDA and the whole process
        # tree are gone. Freeing it earlier would let the next job start while this
        # job's processes could still be driving the device.
        if reserved_device_id:
            try:
                reservation.release_device(reserved_device_id, job_id)
            except Exception as e:
                logger.warning(f"Device release failed for job {job_id}: {e}")
        # Remove the app so the NEXT job starts clean (opt-in — see FRESH_INSTALL_PER_JOB).
        if FRESH_INSTALL_PER_JOB and job.get("is_pr"):
            try:
                _cfg = repository_manager.validate_yaml(project_id)
                _wipe_app(device_id, _bundle_id_for(project_id, _cfg) if _cfg else None,
                          (job.get("platform") or "ios"), "after")
            except Exception:
                pass

def poll_loop():
    global AGENT_ID
    while True:
        try:
            devices = [d["id"] for d in discover_devices()]
            # devices always has at least the virtual fallback — no early bail-out needed
            res = HTTP.post(
                f"{PLATFORM_URL}/jobs/poll",
                json={"agent_id": AGENT_ID, "connected_devices": devices},
                timeout=5
            )
            if res.status_code == 200:
                job = res.json()
                if job and job.get("job_id"):
                    logger.info(f"Accepted Job: {job['job_id']}")
                    run_job(job, devices)
        except Exception as e:
            logger.error(f"Poll failed: {e}")
        time.sleep(3)

def _startup_sweep() -> None:
    """Startup orphan sweep — classification always, reaping only if armed.

    Reaping requires PROC_SWEEP_ENABLED and PROC_SWEEP_ALLOW_REAP both true with
    PROC_SWEEP_DRY_RUN false; every default is safe, so out of the box this only
    classifies and logs. Called after registration because deciding what is an
    orphan needs the backend's job state, and it swallows every exception: an
    agent that cannot sweep must still be able to run jobs.
    """
    try:
        proctree.log_sweep_report(proctree.sweep_orphans())
    except Exception as e:
        logger.warning(f"Orphan sweep (dry-run) skipped: {e}")


def main():
    global AGENT_ID
    hostname = socket.gethostname()
    os_name = platform.system()
    capabilities = get_capabilities()
    devices = discover_devices()
    
    logger.info(f"Registering agent {hostname}...")
    res = HTTP.post(
        f"{PLATFORM_URL}/agents/register",
        json={
            "hostname": hostname,
            "os": os_name,
            "capabilities": capabilities,
            "connected_devices": devices
        }
    )
    if res.status_code != 200:
        logger.error(f"Registration failed: {res.text}")
        sys.exit(1)
        
    AGENT_ID = res.json()["id"]
    logger.info(f"Registered successfully. Agent ID: {AGENT_ID}")

    _startup_sweep()
    
    hb_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    hb_thread.start()
    
    logger.info("Polling for jobs...")
    poll_loop()

if __name__ == "__main__":
    main()
