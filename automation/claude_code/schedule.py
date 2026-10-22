"""The nightly AI Agent run as a macOS launchd job.

launchd, not a thread in the backend: the Mac reboots between days and the
platform is not running then. The job runs scripts/claude_nightly.sh, which starts
the platform if it is down and then the nightly session. launchd also runs a job
that was due while the Mac slept, once it wakes.

Kept in step with the AI Agent settings (nightly_enabled, nightly_time): the API
calls sync() whenever they change, and on backend start.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
from typing import Any, Dict

from automation.claude_code import paths

LABEL = "com.vyapy.ai-agent.nightly"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
SCRIPT = os.path.join(paths.MAIN_ROOT, "scripts", "claude_nightly.sh")


def _domain() -> str:
    return f"gui/{os.getuid()}"


def plist_for(hhmm: str) -> Dict[str, Any]:
    h, m = (int(x) for x in (hhmm or "01:00").split(":")[:2])
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", SCRIPT],
        "StartCalendarInterval": {"Hour": h, "Minute": m},
        "WorkingDirectory": paths.MAIN_ROOT,
        "StandardOutPath": os.path.join(paths.SESSIONS_DIR, "nightly.out"),
        "StandardErrorPath": os.path.join(paths.SESSIONS_DIR, "nightly.out"),
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        "RunAtLoad": False,
    }


def installed() -> Dict[str, Any]:
    if not os.path.exists(PLIST):
        return {"installed": False}
    try:
        with open(PLIST, "rb") as f:
            p = plistlib.load(f)
        cal = p.get("StartCalendarInterval") or {}
        t = f"{int(cal.get('Hour', 0)):02d}:{int(cal.get('Minute', 0)):02d}"
    except (OSError, ValueError, plistlib.InvalidFileException):
        t = ""
    loaded = subprocess.run(["launchctl", "print", f"{_domain()}/{LABEL}"],
                            capture_output=True, timeout=20).returncode == 0
    return {"installed": True, "time": t, "loaded": loaded}


def install(hhmm: str) -> Dict[str, Any]:
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    os.makedirs(paths.SESSIONS_DIR, exist_ok=True)
    with open(PLIST, "wb") as f:
        plistlib.dump(plist_for(hhmm), f)
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True, timeout=20)
    r = subprocess.run(["launchctl", "bootstrap", _domain(), PLIST], capture_output=True, text=True,
                       timeout=20)
    out = installed()
    if r.returncode != 0 and not out.get("loaded"):
        out["error"] = (r.stderr or r.stdout).strip()[:300]
    return out


def uninstall() -> Dict[str, Any]:
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True, timeout=20)
    try:
        os.remove(PLIST)
    except OSError:
        pass
    return installed()


def sync(s: Dict[str, Any]) -> Dict[str, Any]:
    """Make launchd match the settings. Never from the test suite (APP_ENV=test):
    a test must not install or remove the real Mac's job."""
    if os.getenv("APP_ENV") == "test":
        return {"installed": False, "skipped": "test environment"}
    want = bool(s.get("nightly_enabled"))
    cur = installed()
    if not want:
        return uninstall() if cur.get("installed") else cur
    if cur.get("installed") and cur.get("loaded") and cur.get("time") == s.get("nightly_time"):
        return cur
    return install(s.get("nightly_time") or "01:00")
