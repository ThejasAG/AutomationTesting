"""The nightly AI Agent session, started by the backend at the configured time.

Not launchd: the project lives in ~/Desktop, which macOS privacy protection (TCC)
closes to background jobs. MEASURED 2026-10-23..26: the launchd job fired every
day and died at once with "/bin/bash: …/scripts/claude_nightly.sh: Operation not
permitted" — no nightly ever ran. The backend, started from a terminal, can read
the project, so a thread in it starts the session instead.

A plain thread checking every 30 s (no extra dependency). Local time, because
"13:15" means 13:15 on this Mac. The window is 3 hours wide, so a backend started
at 13:40 still runs that day's session; it never starts twice in one day, and it
waits (retrying every 5 minutes inside the window) while another session runs.
The session itself waits for a person's runs to finish before using the rig.

Settings: nightly_enabled, nightly_time, nightly_mode (AI Agent page).
`scripts/claude_nightly.sh` remains for starting one by hand from a terminal.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger("claude_code.schedule")

WINDOW = timedelta(hours=3)
CHECK_S = 30
RETRY_S = 300
# The launchd job of 2026-10-22, removed on start (it cannot work from ~/Desktop).
LEGACY_LABEL = "com.vyapy.ai-agent.nightly"
LEGACY_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LEGACY_LABEL}.plist")

_state: Dict[str, Any] = {"thread": None, "last_try": 0.0, "last_error": "", "last_started": ""}


def _in_tests() -> bool:
    """Under pytest, whatever APP_ENV says. MEASURED 2026-10-26: .env sets APP_ENV=dev,
    an agent session's pytest inherited it, conftest's setdefault kept it, and the
    APP_ENV-only guard let a test start the real nightly clock."""
    return os.getenv("APP_ENV") == "test" or "pytest" in sys.modules


def _at(day: datetime, hhmm: str) -> datetime:
    h, m = (int(x) for x in (hhmm or "01:00").split(":")[:2])
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def _started_since(local_since: datetime) -> bool:
    """Has a nightly session been created since that local time (any outcome)?"""
    from automation.database.config import SessionLocal
    from automation.database.models import AgentSession
    since_utc = datetime.utcnow() - (datetime.now() - local_since)
    with SessionLocal() as db:
        return db.query(AgentSession).filter(AgentSession.trigger == "nightly",
                                             AgentSession.created_at >= since_utc).first() is not None


def due(s: Dict[str, Any], now: Optional[datetime] = None) -> Optional[datetime]:
    """Today's start time when the session should start now, else None."""
    if not s.get("nightly_enabled"):
        return None
    now = now or datetime.now()
    start = _at(now, s.get("nightly_time") or "01:00")
    if not (start <= now < start + WINDOW):
        return None
    return None if _started_since(start) else start


def next_run(s: Dict[str, Any], now: Optional[datetime] = None) -> str:
    if not s.get("nightly_enabled"):
        return ""
    now = now or datetime.now()
    start = _at(now, s.get("nightly_time") or "01:00")
    if now >= start + WINDOW or (now >= start and _started_since(start)):
        start += timedelta(days=1)
    return start.isoformat(timespec="minutes")


def tick(now: Optional[datetime] = None) -> Optional[str]:
    """One check: start the nightly session if it is due. Returns its id."""
    from automation.agentic import settings
    from automation.claude_code import session
    s = settings.get()
    if due(s, now) is None:
        return None
    if time.time() - _state["last_try"] < RETRY_S:
        return None
    _state["last_try"] = time.time()
    try:
        sid = session.create("nightly", trigger="nightly",
                             options={"mode": s.get("nightly_mode") or "auto"}, created_by="schedule")
    except (session.Busy, session.Refused, ValueError) as e:
        if str(e)[:300] != _state["last_error"]:             # once per reason, not every retry
            session.note_hook("nightly", "waiting" if isinstance(e, session.Busy) else "refused", str(e))
        _state["last_error"] = str(e)[:300]
        logger.warning("nightly AI Agent session not started yet: %s", e)
        return None
    _state.update(last_error="", last_started=datetime.now().isoformat(timespec="minutes"))
    session.note_hook("nightly", "started", f"nightly session {sid[:8]}", session_id=sid)
    logger.info("nightly AI Agent session %s started", sid[:8])
    return sid


def _loop() -> None:
    while True:
        try:
            tick()
        except Exception as e:  # noqa: BLE001 — the clock must survive a bad tick
            _state["last_error"] = f"{type(e).__name__}: {e}"[:300]
            logger.warning("nightly scheduler tick failed: %s", e)
        time.sleep(CHECK_S)


def start() -> None:
    """Start the clock (once per process) and remove the dead launchd job."""
    if _in_tests():
        return
    remove_legacy_launchd()
    if _state["thread"] is None:
        t = threading.Thread(target=_loop, name="ai-agent-nightly", daemon=True)
        t.start()
        _state["thread"] = t


def remove_legacy_launchd() -> bool:
    if not os.path.exists(LEGACY_PLIST):
        return False
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LEGACY_LABEL}"],
                   capture_output=True, timeout=20)
    try:
        os.remove(LEGACY_PLIST)
    except OSError:
        pass
    logger.info("removed the launchd nightly job (macOS blocks it from ~/Desktop)")
    return True


def installed() -> Dict[str, Any]:
    """What the AI Agent page shows: is the nightly clock running, and when next."""
    from automation.agentic import settings
    s = settings.get()
    alive = bool(_state["thread"] and _state["thread"].is_alive())
    out: Dict[str, Any] = {"installed": alive and bool(s.get("nightly_enabled")),
                           "time": s.get("nightly_time"), "by": "backend",
                           "next": next_run(s), "last_started": _state["last_started"]}
    if s.get("nightly_enabled") and not alive:
        out["error"] = "the backend's nightly clock is not running (restart the backend)"
    elif _state["last_error"]:
        out["error"] = _state["last_error"]
    return out


def sync(s: Dict[str, Any]) -> Dict[str, Any]:
    """Settings changed: the clock reads them on every tick, so just report."""
    if _in_tests():
        return {"installed": False, "skipped": "test environment"}
    return installed()

