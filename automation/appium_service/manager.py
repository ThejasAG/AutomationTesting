import subprocess
import socket
import glob
import json
import logging
import time
import os
import urllib.request
from typing import Dict, Any, List

APPIUM_LOG_DIR = os.path.abspath(os.path.join("logs", "appium"))


def _prebuilt_wda_derived_data() -> str:
    """DerivedData dir holding an already-compiled WebDriverAgent, or "".

    Xcode suffixes the dir with a hash, so the path cannot be hardcoded — any dir
    containing a built WebDriverAgentRunner-Runner.app will do. Override with
    WDA_DERIVED_DATA.
    """
    override = os.getenv("WDA_DERIVED_DATA")
    if override:
        return override

    hits = glob.glob(
        os.path.expanduser(
            "~/Library/Developer/Xcode/DerivedData/WebDriverAgent-*/Build/Products/"
            "Debug-iphonesimulator/WebDriverAgentRunner-Runner.app"
        )
    )
    if not hits:
        return ""
    newest = max(hits, key=os.path.getmtime)
    # .../<derived-data>/Build/Products/Debug-iphonesimulator/<app> -> <derived-data>
    return os.path.abspath(os.path.join(newest, *[os.pardir] * 4))

logger = logging.getLogger(__name__)

def get_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('', 0))
    s.listen(1)
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for_appium_ready(port: int, process: subprocess.Popen, timeout: int = 30) -> None:
    """Poll GET http://127.0.0.1:{port}/status until Appium is ready.

    Polls every 0.5s for up to *timeout* seconds. Returns once /status
    answers HTTP 200. Raises RuntimeError if the process dies or the
    timeout elapses without a 200.
    """
    status_url = f"http://127.0.0.1:{port}/status"
    logger.info(f"Waiting for Appium on port {port}...")
    start = time.time()
    deadline = start + timeout
    while time.time() < deadline:
        # If the process exited, there's no point in polling further.
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            logger.error(f"Appium failed to start: {stderr}")
            raise RuntimeError(f"Appium failed to start on port {port}")
        try:
            with urllib.request.urlopen(status_url, timeout=2) as resp:
                if resp.status == 200:
                    elapsed = time.time() - start
                    logger.info(f"Appium ready on port {port} after {elapsed:.1f}s")
                    return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Appium failed to start on port {port}")


class AppiumSession:
    def __init__(self, session_id: str, device_id: str, port: int, process: subprocess.Popen):
        self.session_id = session_id
        self.device_id = device_id
        self.port = port
        self.process = process
        self.status = "running"
        self.created_at = time.time()

class AppiumProcessManager:
    def __init__(self):
        self.active_sessions: Dict[str, AppiumSession] = {}

    def allocate_instance(self, run_id: str, device_id: str) -> AppiumSession:
        """Spawns a real Appium process on a dynamic port"""
        port = get_free_port()
        logger.info(f"Starting real Appium server for run {run_id} on port {port}")

        # Use npx to launch appium to ensure it runs even if not globally installed
        npx_cmd = "npx.cmd" if os.name == "nt" else "npx"
        cmd = [npx_cmd, "appium", "-p", str(port), "--log-level", "error"]

        # Reuse an already-compiled WebDriverAgent instead of rebuilding it.
        #
        # WDA is Appium's own UI-test runner. By default Appium compiles it from
        # source into a fresh DerivedData dir for EVERY session — a full Xcode
        # build that dwarfs the tests themselves. Set here rather than in the
        # project's conftest so it survives the git reset between runs and applies
        # to every project, not just the one whose conftest happens to set it.
        wda = _prebuilt_wda_derived_data()
        if wda:
            cmd += [
                "--default-capabilities",
                json.dumps(
                    {"appium:derivedDataPath": wda, "appium:usePrebuiltWDA": True}
                ),
            ]
            logger.info(f"Reusing prebuilt WebDriverAgent from {wda}")
        else:
            logger.warning(
                "No prebuilt WebDriverAgent found — Appium will compile it from "
                "source, which takes many minutes. Build it once with: "
                "appium driver run xcuitest build-wda"
            )

        # Appium's output goes to a FILE, never to an unread pipe.
        #
        # subprocess.PIPE with nobody draining it is a deadlock: Appium is chatty,
        # and once the 64KB pipe buffer fills it blocks forever on write — taking
        # any xcodebuild it spawned down with it (hangs at 0% CPU, indefinitely).
        os.makedirs(APPIUM_LOG_DIR, exist_ok=True)
        log_path = os.path.join(APPIUM_LOG_DIR, f"appium-{run_id}.log")
        log_file = open(log_path, "wb")
        logger.info(f"Appium log: {log_path}")

        process = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        process._log_file = log_file  # closed in release_instance

        # Block until Appium answers /status with 200 (or fail after 30s).
        # Only after this do we hand the port back to the caller, so tests
        # never connect before the server is listening.
        wait_for_appium_ready(port, process, timeout=30)

        session = AppiumSession(
            session_id=run_id,
            device_id=device_id,
            port=port,
            process=process
        )
        self.active_sessions[run_id] = session
        return session

    def release_instance(self, session_id: str) -> bool:
        """Terminates the Appium process"""
        if session_id in self.active_sessions:
            session = self.active_sessions[session_id]
            logger.info(f"Terminating Appium server for session {session_id} on port {session.port}")
            try:
                session.process.terminate()
                session.process.wait(timeout=5)
            except Exception as e:
                logger.warning(f"Failed to cleanly terminate Appium: {e}")
                session.process.kill()

            log_file = getattr(session.process, "_log_file", None)
            if log_file is not None:
                log_file.close()

            del self.active_sessions[session_id]
            return True
        return False

    def get_session(self, session_id: str) -> AppiumSession:
        return self.active_sessions.get(session_id)

appium_manager = AppiumProcessManager()
