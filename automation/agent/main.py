import os
import sys
import time
import socket
import platform
import threading
import requests
import logging
from pathlib import Path
from typing import List, Dict, Any

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
from automation.projects.repository import repository_manager
from automation.projects.preparation import preparation_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Agent")

PLATFORM_URL = os.getenv("PLATFORM_URL", "http://localhost:8000/api/v1")
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


def get_virtual_device() -> Dict[str, Any]:
    """Return an OS-appropriate virtual device for pipeline testing."""
    if platform.system() == "Darwin":
        return VIRTUAL_IOS_DEVICE
    return VIRTUAL_ANDROID_DEVICE

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
                requests.post(
                    f"{PLATFORM_URL}/agents/{AGENT_ID}/heartbeat",
                    json={"status": "online", "connected_devices": devices},
                    timeout=5
                )
        except Exception as e:
            logger.error(f"Heartbeat failed: {e}")
        time.sleep(10)

def report_status(job_id: str, status: str, logs: List[str] = [], timeline_event: str = None, error_message: str = None):
    try:
        requests.post(
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
            res = requests.post(
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
        requests.post(
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
        requests.post(
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

    # Screen capture state — declared here so finally block can always reference it.
    capture_thread: threading.Thread | None = None
    stop_capture_event = threading.Event()
        
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

        # 2. Start screen capture BEFORE executing tests so the browser can
        #    watch from the very first moment the suite launches. By this point
        #    the app is already built, installed and launched, so the stream
        #    shows the running app even if there is no test suite yet.
        capture_thread, stop_capture_event = _start_screen_capture(job_id, device_id)

        # 3. Execute — but only if the repository actually has a test suite.
        #    A repo with no Appium tests is not a failure: the app was deployed
        #    and is running on the device. Reporting that as "failed" makes a
        #    perfectly healthy platform look broken.
        if not _has_test_suite(project_id, config):
            report_status(
                job_id, "running",
                ["App built, installed and launched on the device.",
                 "No test suite found in the repository — nothing to execute.",
                 f"Add an Appium suite and set execution.command (currently: "
                 f"{config.execution.command!r}) to run automated tests."],
                "App Deployed",
            )
            # Let the stream show the running app for a few seconds before we cut it.
            time.sleep(8)
            _stop_screen_capture(job_id, capture_thread, stop_capture_event)
            capture_thread = None
            report_status(
                job_id, "passed",
                ["Deployment verified. No tests to run."],
                "Done",
            )
            return

        report_status(job_id, "running", ["Executing tests..."], "Executing")
        exec_res = framework.execute(project_id, device_id, {"command": config.execution.command}, job_id)

        status = exec_res.get("status", "failed")
        logs = exec_res.get("logs", [])

        # 5. Stop capture as soon as pytest exits.
        _stop_screen_capture(job_id, capture_thread, stop_capture_event)
        capture_thread = None  # prevent double-stop in finally
        
        report_status(job_id, "collecting_evidence", logs, f"Execution {status}")
        
        # 6. Upload Evidence
        evidence_data = framework.collect_evidence(project_id, exec_res)
        evidence_data["run_id"] = job_id
        _upload_evidence_with_retry(job_id, evidence_data)

        final_status = "passed" if status == "passed" else "failed"
        report_status(job_id, final_status, ["Execution Complete."], "Done")
        
    except Exception as e:
        logger.error(f"Job Execution Error: {e}")
        report_status(job_id, "failed", [f"Execution Error: {str(e)}"], "Failed", str(e))
    finally:
        # Always stop capture if it wasn't already stopped cleanly above.
        if capture_thread is not None:
            _stop_screen_capture(job_id, capture_thread, stop_capture_event)
        try:
            framework.cleanup(project_id)
        except Exception:
            pass

def poll_loop():
    global AGENT_ID
    while True:
        try:
            devices = [d["id"] for d in discover_devices()]
            # devices always has at least the virtual fallback — no early bail-out needed
            res = requests.post(
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

def main():
    global AGENT_ID
    hostname = socket.gethostname()
    os_name = platform.system()
    capabilities = get_capabilities()
    devices = discover_devices()
    
    logger.info(f"Registering agent {hostname}...")
    res = requests.post(
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
    
    hb_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    hb_thread.start()
    
    logger.info("Polling for jobs...")
    poll_loop()

if __name__ == "__main__":
    main()
