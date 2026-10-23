"""File-backed job queue shared between the adapter and Tanish's Android bot.

The platform POSTs a job to the adapter (/run); the adapter appends it here. The
bot polls the adapter (/status), sees a pending job, runs it, and POSTs each
scenario result back to the job's callback_url. A plain JSON file is deliberate:
the bot runs as a separate process (often on a separate machine sharing this
directory), so a DB connection would couple them — a file does not.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Overridable so tests / a separate bot host can point at a shared location.
QUEUE_PATH = Path(os.getenv("BOT_QUEUE_PATH",
                            Path(__file__).resolve().parent / "bot_queue.json"))

_lock = threading.Lock()

# Job lifecycle: pending -> started -> done
PENDING, STARTED, DONE = "pending", "started", "done"


def _load() -> List[Dict[str, Any]]:
    if not QUEUE_PATH.exists():
        return []
    try:
        return json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save(jobs: List[Dict[str, Any]]) -> None:
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Keep the file from growing forever — drop the oldest done jobs.
    if len(jobs) > 200:
        jobs = jobs[-200:]
    QUEUE_PATH.write_text(json.dumps(jobs, indent=2), encoding="utf-8")


def add_job(run_id: str, scenarios: List[Any], callback_url: str,
            consumer_device: Optional[str] = None,
            business_device: Optional[str] = None) -> Dict[str, Any]:
    """Queue a new job. Replaces any existing entry for the same run_id."""
    with _lock:
        jobs = [j for j in _load() if j.get("run_id") != run_id]
        job = {
            "run_id": run_id,
            "scenarios": scenarios,
            "callback_url": callback_url,
            "consumer_device": consumer_device,
            "business_device": business_device,
            "state": PENDING,
            "scenarios_completed": 0,
            "queued_at": datetime.utcnow().isoformat() + "Z",
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
        jobs.append(job)
        _save(jobs)
        return job


def get_pending_job() -> Optional[Dict[str, Any]]:
    """The oldest job the bot has not started yet, or None."""
    with _lock:
        for job in _load():
            if job.get("state") == PENDING:
                return job
    return None


def _update(run_id: str, **fields) -> Optional[Dict[str, Any]]:
    with _lock:
        jobs = _load()
        found = None
        for job in jobs:
            if job.get("run_id") == run_id:
                job.update(fields)
                job["updated_at"] = datetime.utcnow().isoformat() + "Z"
                found = job
        _save(jobs)
        return found


def mark_job_started(run_id: str) -> Optional[Dict[str, Any]]:
    return _update(run_id, state=STARTED)


def mark_job_done(run_id: str) -> Optional[Dict[str, Any]]:
    return _update(run_id, state=DONE)


def increment_completed(run_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        jobs = _load()
        found = None
        for job in jobs:
            if job.get("run_id") == run_id:
                job["scenarios_completed"] = job.get("scenarios_completed", 0) + 1
                job["updated_at"] = datetime.utcnow().isoformat() + "Z"
                found = job
        _save(jobs)
        return found


def current_status() -> Dict[str, Any]:
    """What GET /status reports: the job the bot should be (or is) running."""
    jobs = _load()
    started = [j for j in jobs if j.get("state") == STARTED]
    active = started[-1] if started else get_pending_job()
    return {
        "running": bool(started),
        "current_run_id": active.get("run_id") if active else None,
        "scenarios_completed": active.get("scenarios_completed", 0) if active else 0,
        "pending_jobs": sum(1 for j in jobs if j.get("state") == PENDING),
        "last_updated": active.get("updated_at") if active else None,
    }
