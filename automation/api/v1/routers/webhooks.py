"""GitHub webhook receiver — auto-triggers regression tests on PR/push events.

This router is intentionally mounted WITHOUT the ``get_current_user`` JWT
dependency: GitHub (not a logged-in browser) calls these endpoints. Requests
are instead authenticated with the ``X-Hub-Signature-256`` HMAC header when
``GITHUB_WEBHOOK_SECRET`` is configured. If that env var is unset, signature
validation is skipped (dev mode only).
"""

import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from automation.database import database
from automation.database.config import SessionLocal
from automation.database.models import TestProject
from automation.device_manager.service import device_service
from automation.device_manager.models import DeviceStatus
from automation.intelligence.git_analyzer import git_analyzer
from automation.intelligence.recommendation import recommendation_engine

logger = logging.getLogger("webhooks")

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])

# PR actions that should trigger a regression run.
_TRIGGER_PR_ACTIONS = {"opened", "synchronize", "reopened"}
_MAIN_REFS = {"refs/heads/main", "refs/heads/master"}


def _verify_signature(secret: str, body: bytes, signature_header: str) -> bool:
    """Constant-time HMAC-SHA256 validation of the GitHub payload signature."""
    if not signature_header:
        return False
    sha_name, _, provided = signature_header.partition("=")
    if sha_name != "sha256" or not provided:
        return False
    digest = hmac.new(secret.encode("utf-8"), msg=body, digestmod=hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, provided)


def _find_project(db, repo_clone_url: str, repo_ssh_url: str, repo_full_name: str):
    """Match a TestProject by clone_url / ssh_url, falling back to name substring."""
    candidates = [u for u in (repo_clone_url, repo_ssh_url) if u]
    for url in candidates:
        project = db.query(TestProject).filter(TestProject.git_url == url).first()
        if project:
            return project

    # Tolerant fallback: match on the "owner/repo" slug appearing in git_url.
    if repo_full_name:
        return (
            db.query(TestProject)
            .filter(TestProject.git_url.ilike(f"%{repo_full_name}%"))
            .first()
        )
    return None


@router.post("/github/test")
async def github_webhook_test():
    """Unauthenticated reachability probe — used to verify the endpoint is live."""
    return {"status": "ok", "message": "Webhook endpoint working"}


@router.post("/github")
async def github_webhook(request: Request):
    """Receive GitHub ``pull_request`` / ``push`` events and trigger tests."""
    body: bytes = await request.body()

    # ── 1. Signature validation (skipped in dev when no secret set) ──────────
    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    if secret:
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not _verify_signature(secret, body, signature):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
    else:
        logger.warning("GITHUB_WEBHOOK_SECRET not set — skipping signature validation (dev mode)")

    # ── 2. Parse payload ─────────────────────────────────────────────────────
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event = request.headers.get("X-GitHub-Event", "")
    action = payload.get("action", "")

    # ── 3. Event filtering ───────────────────────────────────────────────────
    pr = payload.get("pull_request", {}) or {}
    is_pr = event == "pull_request" and action in _TRIGGER_PR_ACTIONS
    is_main_push = event == "push" and payload.get("ref", "") in _MAIN_REFS

    if not (is_pr or is_main_push):
        return {
            "status": "ignored",
            "message": f"Event '{event}' (action='{action}') is not a trigger event",
        }

    # ── 4. Extract metadata from the payload ─────────────────────────────────
    changed_files = git_analyzer.extract_files_from_webhook(payload)

    if is_pr:
        pr_number = pr.get("number")
        head = pr.get("head", {}) or {}
        branch = head.get("ref")
        commit_sha = head.get("sha")
    else:  # push to main
        pr_number = None
        branch = payload.get("ref", "").split("/")[-1] or None
        commit_sha = payload.get("after") or (payload.get("head_commit", {}) or {}).get("id")

    repo = payload.get("repository", {}) or {}
    repo_name = repo.get("full_name") or repo.get("name") or "unknown"
    clone_url = repo.get("clone_url", "")
    ssh_url = repo.get("ssh_url", "")

    # ── 5. Impact analysis + test selection ──────────────────────────────────
    affected_modules = git_analyzer.identify_modules(changed_files)

    selected_tests = set()
    for module in affected_modules:
        selected_tests.update(recommendation_engine.default_test_map.get(module, []))
    # Fall back to a smoke test when files changed but no module mapped.
    if not selected_tests and changed_files:
        selected_tests.add("test_smoke.py")
    selected_tests = sorted(selected_tests)

    # ── 6. Match the project and queue a single TestRun ──────────────────────
    run_ids = []
    with SessionLocal() as db:
        project = _find_project(db, clone_url, ssh_url, repo_name)
        if not project:
            raise HTTPException(
                status_code=404,
                detail=f"No TestProject matches repository '{repo_name}' ({clone_url or ssh_url})",
            )

        online_devices = [
            d for d in device_service.get_all_devices() if d.status == DeviceStatus.ONLINE
        ]

        # Device selection priority:
        #   1. first ONLINE iOS device
        #   2. first ONLINE Android device
        #   3. none → queue a "pending" run with no device attached
        device = (
            next((d for d in online_devices if (d.platform or "").lower() == "ios"), None)
            or next((d for d in online_devices if (d.platform or "").lower() == "android"), None)
        )

        now = datetime.utcnow()
        run_id = str(uuid.uuid4())
        database.insert_test_run(db, {
            "id": run_id,
            "project_id": project.id,
            "test_suite": project.name,
            "test_name": ", ".join(selected_tests) or "Regression",
            "status": "queued",
            "job_state": "queued",
            "started_at": now,
            "created_at": now,
            "device_name": device.id if device else "pending",
            "os_version": device.platform_version if device else None,
            "platform": device.platform if device else "iOS",
            "triggered_by": f"github_webhook:{repo_name}",
            "branch": branch,
            "commit_sha": commit_sha,
        })
        run_ids.append(run_id)

    module_count = len(affected_modules)
    message = (
        f"Running {len(selected_tests)} test{'s' if len(selected_tests) != 1 else ''} "
        f"for {module_count} affected module{'s' if module_count != 1 else ''}"
    )

    logger.info(
        "GitHub webhook triggered: repo=%s pr=%s modules=%s tests=%s runs=%d",
        repo_name, pr_number, affected_modules, selected_tests, len(run_ids),
    )

    return {
        "status": "triggered",
        "pr_number": pr_number,
        "affected_modules": affected_modules,
        "selected_tests": selected_tests,
        "run_ids": run_ids,
        "message": message,
    }
