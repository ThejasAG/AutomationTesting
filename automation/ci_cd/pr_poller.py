"""PR auto-test poller.

A background loop that checks each project's open GitHub PRs and auto-queues a
test run for any PR head commit that hasn't been tested yet — hands-off PR
testing without needing the platform to be publicly reachable for webhooks.

Enabled by default; set PR_POLL_ENABLED=false to turn off. It safely no-ops when
no GitHub token is available. Reuses the same device selection and run shape as
the manual "Test PR" endpoint, and dedups by commit SHA so a commit is tested
once. A per-cycle cap avoids a burst when first pointed at a repo with many PRs.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("pr_poller")

POLL_INTERVAL = int(os.getenv("PR_POLL_INTERVAL_SEC", "300"))
MAX_PER_CYCLE = int(os.getenv("PR_POLL_MAX_PER_CYCLE", "5"))


def _enabled() -> bool:
    return os.getenv("PR_POLL_ENABLED", "true").lower() in ("1", "true", "yes", "on")


def _autotest_enabled() -> bool:
    """When on, the poller runs the full autonomous QA loop (plan → build → run →
    comment) per new PR commit instead of just queuing a generic run."""
    return os.getenv("PR_AUTOTEST_ENABLED", "false").lower() in ("1", "true", "yes", "on")


# Observable state for the status endpoint.
state: Dict[str, Any] = {
    "enabled": _enabled(), "interval_sec": POLL_INTERVAL, "running": False,
    "last_poll": None, "last_error": None, "total_queued": 0, "recent": [],
}


def _owner_repo(git_url: str):
    m = re.search(r"github\.com[:/]+([^/]+)/([^/.]+)", git_url or "")
    if not m:
        raise ValueError(f"Not a GitHub URL: {git_url}")
    return m.group(1), m.group(2)


def _token() -> str:
    tok = os.environ.get("GITHUB_TOKEN", "")
    if tok:
        return tok
    try:  # reuse the git credential helper the manual endpoint uses
        from automation.api.v1.routers.pull_requests import _token_from_git_credentials
        return _token_from_git_credentials() or ""
    except Exception:
        return ""


def _queue_run(db, project, pr: Dict[str, Any]) -> Optional[str]:
    from automation.database import database
    from automation.device_manager.service import device_service
    from automation.projects.builder import app_builder

    online = device_service.get_online_devices()
    device = (next((d for d in online if (d.platform or "").lower() == "ios"), None)
              or next((d for d in online if (d.platform or "").lower() == "android"), None))
    platform = device.platform if device else (project.platform or "iOS")
    device_name = device.id if device else "pending"
    if platform.lower() == "ios":
        # Pin auto PR runs to a specific simulator so they are deterministic and
        # never hijacked by whatever sim happens to be booted. PR_TEST_IOS_DEVICE
        # overrides; otherwise honour a real online device; else pick a sensible sim.
        pinned = os.getenv("PR_TEST_IOS_DEVICE", "").strip()
        # Resolved from the devices REGISTRY, not the backend's own simctl: this
        # decides which machine will execute the job, and the backend's local
        # simulator list cannot answer that for any machine but its own.
        from automation.device_manager.service import resolve_ios_device_record
        resolved_machine, resolved, note = resolve_ios_device_record(
            pinned or (device.id if device else None)
        )
        if note:
            logger.info("PR device resolution: %s", note)
        if resolved:
            device_name = resolved

    owner, repo = _owner_repo(project.git_url)
    now = datetime.utcnow()
    run_id = str(uuid.uuid4())
    # Routing intent comes straight from the resolver: it returned the machine
    # that owns the device, so nothing has to be reconstructed from provider,
    # hostname or the udid string. Unresolved stays NULL, as before.
    machine_id = resolved_machine if resolved else None

    database.insert_test_run(db, {
        "machine_id": machine_id,
        "id": run_id, "project_id": project.id, "test_suite": project.name,
        "test_name": f"PR #{pr['number']}: {pr.get('title') or pr.get('branch')}",
        "status": "queued", "job_state": "queued", "started_at": now, "created_at": now,
        "device_name": device_name,
        "os_version": device.platform_version if device else None,
        "platform": platform,
        "triggered_by": f"pr_auto:{owner}/{repo}#{pr['number']}",
        "branch": pr.get("branch"), "commit_sha": pr.get("commit_sha"),
    })
    logger.info("Auto-queued PR test %s/%s#%s commit=%s run=%s",
                owner, repo, pr["number"], (pr.get("commit_sha") or "")[:8], run_id)
    return run_id


def poll_once() -> List[str]:
    """One poll cycle. Returns the run_ids queued this cycle."""
    from automation.database.config import SessionLocal
    from automation.database.models import TestProject, TestRun
    from automation.integrations.github import GitHubIntegration

    token = _token()
    if not token:
        state["last_error"] = "no GitHub token (set GITHUB_TOKEN or a git credential)"
        return []
    gh = GitHubIntegration(token)
    queued: List[str] = []

    with SessionLocal() as db:
        projects = db.query(TestProject).filter(TestProject.git_url.isnot(None)).all()
        for p in projects:
            if len(queued) >= MAX_PER_CYCLE:
                logger.info("PR poller hit the per-cycle cap (%s); rest next cycle.", MAX_PER_CYCLE)
                break
            try:
                owner, repo = _owner_repo(p.git_url)
            except Exception:
                continue
            try:
                prs = gh.list_open_prs(owner, repo)
            except Exception as e:
                logger.debug("PR poller: list PRs failed for %s/%s: %s", owner, repo, e)
                continue
            for pr in prs:
                if len(queued) >= MAX_PER_CYCLE:
                    break
                sha = pr.get("commit_sha")
                if pr.get("draft") or not sha:
                    continue
                # Dedup: a commit is tested once (any run row with that SHA).
                if db.query(TestRun).filter(TestRun.commit_sha == sha).first():
                    continue
                try:
                    if _autotest_enabled():
                        # Autonomous QA: plan → build PR branch → run scenarios →
                        # comment on the PR. Runs in its own thread (heavy).
                        import threading
                        from automation.intelligence.pr_autotest import run_pr_autotest
                        threading.Thread(target=run_pr_autotest, args=(p.id, pr["number"]),
                                         daemon=True, name=f"pr-autotest-{pr['number']}").start()
                        rid = f"autotest:{pr['number']}"
                    else:
                        rid = _queue_run(db, p, pr)
                    if rid:
                        queued.append(rid)
                        state["recent"] = ([{
                            "repo": f"{owner}/{repo}", "pr": pr["number"],
                            "commit": sha[:8], "run_id": rid,
                        }] + state["recent"])[:10]
                except Exception as e:
                    logger.warning("PR poller: could not process %s#%s: %s", repo, pr.get("number"), e)

    state["total_queued"] += len(queued)
    state["last_error"] = None
    return queued


def _loop():
    logger.info("PR auto-test poller started (interval=%ss, cap=%s/cycle).", POLL_INTERVAL, MAX_PER_CYCLE)
    state["running"] = True
    while True:
        try:
            poll_once()
        except Exception as e:
            state["last_error"] = str(e)
            logger.warning("PR poller cycle error: %s", e)
        state["last_poll"] = datetime.utcnow().isoformat()
        time.sleep(POLL_INTERVAL)


_started = False


def start():
    global _started
    if _started:
        return
    state["enabled"] = _enabled()
    if not _enabled():
        logger.info("PR auto-test poller disabled (PR_POLL_ENABLED=false).")
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="pr-poller").start()
