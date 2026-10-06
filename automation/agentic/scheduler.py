"""Fires the nightly batch, and the weekly scenario generator after it.

A plain thread checking every 30 s (no extra dependency). Local time, because
"01:00" means 01:00 on this Mac. The window is 3 hours wide so a backend that was
restarted at 01:10 still runs that night, and a batch is never started twice in
one night.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from automation.agentic import orchestrator, settings
from automation.database.config import SessionLocal
from automation.database.models import AgentAction, AgentBatch

logger = logging.getLogger("agentic.scheduler")

_WINDOW = timedelta(hours=3)
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_started = False


def _today_at(hhmm: str) -> datetime:
    h, m = (int(x) for x in (hhmm or "01:00").split(":")[:2])
    return datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)


def _ran_since(trigger: str, since_local: datetime) -> bool:
    since_utc = datetime.utcnow() - (datetime.now() - since_local)
    with SessionLocal() as db:
        return db.query(AgentBatch).filter(AgentBatch.trigger == trigger,
                                           AgentBatch.started_at >= since_utc).first() is not None


def _generated_since(days: int) -> bool:
    since = datetime.utcnow() - timedelta(days=days)
    with SessionLocal() as db:
        return db.query(AgentAction).filter(AgentAction.kind.in_(("app_map", "app_analysis")),
                                            AgentAction.created_at >= since).first() is not None


def next_run() -> str:
    s = settings.get()
    if not s["enabled"]:
        return ""
    t = _today_at(s["nightly_time"])
    if datetime.now() >= t + _WINDOW or _ran_since("scheduled", t):
        t += timedelta(days=1)
    return t.isoformat(timespec="minutes")


def _tick() -> None:
    s = settings.get()
    if not s["enabled"] or orchestrator.status()["running"]:
        return
    start = _today_at(s["nightly_time"])
    now = datetime.now()
    if start <= now < start + _WINDOW and not _ran_since("scheduled", start):
        logger.info("agent: starting the nightly batch")
        orchestrator.start_batch("scheduled")
        return
    # Weekly scenario generation, once the night's batch is done.
    if (s["generator_enabled"] and _DAYS[now.weekday()] == s["generator_day"]
            and _ran_since("scheduled", start) and not _generated_since(6)):
        logger.info("agent: weekly scenario generation")
        orchestrator.generate_scenarios()


def _loop() -> None:
    while True:
        try:
            _tick()
        except orchestrator.Busy:
            pass
        except Exception:
            logger.exception("agent scheduler tick failed")
        time.sleep(30)


def start() -> None:
    """Backend startup: continue an interrupted batch, then start the clock."""
    global _started
    if _started:
        return
    _started = True
    try:
        orchestrator.continue_after_restart()
    except Exception:
        logger.exception("agent: could not continue the interrupted batch")
    threading.Thread(target=_loop, name="agent-scheduler", daemon=True).start()
