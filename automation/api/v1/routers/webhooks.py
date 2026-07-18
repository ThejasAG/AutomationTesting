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
from automation.database.models import TestProject, ApplicationGroup, TestRun
from automation.device_manager.service import device_service
from automation.device_manager.models import DeviceStatus
from automation.intelligence.git_analyzer import git_analyzer
from automation.intelligence.hybrid_impact_analyzer import HybridImpactAnalyzer
from automation.projects.repository import repository_manager
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

    base_branch = None
    if is_pr:
        pr_number = pr.get("number")
        head = pr.get("head", {}) or {}
        branch = head.get("ref")
        commit_sha = head.get("sha")
        base_branch = (pr.get("base", {}) or {}).get("ref")
    else:  # push to main
        pr_number = None
        branch = payload.get("ref", "").split("/")[-1] or None
        commit_sha = payload.get("after") or (payload.get("head_commit", {}) or {}).get("id")

    repo = payload.get("repository", {}) or {}
    repo_name = repo.get("full_name") or repo.get("name") or "unknown"
    clone_url = repo.get("clone_url", "")
    ssh_url = repo.get("ssh_url", "")

    # A PR payload only carries files when GitHub inlines them, and the git
    # fallback below only ever looked at the LAST COMMIT. Diff the whole PR
    # against its merge base instead, so a payment change in commit 1 of 12 is
    # still seen when commit 12 only touched the README.
    if is_pr and base_branch and commit_sha:
        with SessionLocal() as _db:
            _p = _find_project(_db, clone_url, ssh_url, repo_name)
            if _p is not None and repository_manager.is_cloned(_p.id):
                pr_files = git_analyzer.extract_pr_changed_files(
                    repository_manager.get_repo_path(_p.id), commit_sha, base_branch
                )
                if pr_files:
                    changed_files = pr_files

    # ── 4b. Cross-app impact (Graphify + endpoint graph) ─────────────────────
    # For a project inside an AppGroup, run the hybrid analyzer FIRST: it walks
    # the changed file's blast radius, finds the endpoints it touches, and lists
    # every OTHER app in the group that calls those endpoints. This is what turns
    # "Business changed the order endpoint" into "→ also re-test Consumer".
    cross_app_impact: list = []
    affected_endpoints: list = []
    graphify_nodes = 0
    with SessionLocal() as _db:
        _p = _find_project(_db, clone_url, ssh_url, repo_name)
        if _p is not None and _p.group_id:
            group = (
                _db.query(ApplicationGroup)
                .filter(ApplicationGroup.id == _p.group_id)
                .first()
            )
            if group is not None:
                app_configs = [
                    {
                        "name": m.name,
                        "repo_path": repository_manager.get_repo_path(m.id),
                        "app_role": m.project_type or m.platform,
                        "test_suite": m.id,
                        "project_id": m.id,
                    }
                    for m in group.projects
                ]
                try:
                    impact = HybridImpactAnalyzer().analyze(changed_files, app_configs)
                    cross_app_impact = impact.get("cross_app_impact", [])
                    affected_endpoints = impact.get("affected_endpoints", [])
                    graphify_nodes = impact.get("graphify_nodes_affected", 0)
                except Exception as exc:
                    logger.warning("Cross-app impact analysis failed: %s", exc)

    # ── 5. Impact analysis + test selection ──────────────────────────────────
    affected_modules = git_analyzer.identify_modules(changed_files)

    # Recommend only tests that EXIST in the clone. The old path selected
    # test_checkout.py / test_cart.py / test_smoke.py from a hardcoded map —
    # none of which are on disk — so a payment PR "ran" a suite of phantom files
    # and reported success without executing anything.
    selected_tests: list = []
    missing_tests: list = []
    no_coverage = False
    with SessionLocal() as _db:
        _project = _find_project(_db, clone_url, ssh_url, repo_name)
        if _project is not None:
            _rec = recommendation_engine.recommend_for_files(
                changed_files, repository_manager.get_repo_path(_project.id)
            )
            selected_tests = _rec["recommended_tests"]
            missing_tests = _rec["missing_tests"]
            no_coverage = _rec["no_coverage"]
            affected_modules = _rec["affected_modules"] or affected_modules

    if missing_tests:
        logger.warning(
            "%s: %d recommended test(s) do not exist and were dropped: %s",
            repo_name, len(missing_tests), ", ".join(missing_tests),
        )
    if no_coverage:
        # Loud on purpose: a changed module with no test is the case that let the
        # payment regression through, and it must not read as a clean run.
        logger.error(
            "%s: modules %s changed but NO test file covers them — this PR is "
            "NOT verified by any suite.", repo_name, affected_modules,
        )

    # ── 6. Match the project and queue a single TestRun ──────────────────────
    run_ids = []
    android_run_id = None
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
            "bot_type": "ios",
        })
        run_ids.append(run_id)

        # ── 6b. Android cross-app run (Consumer + Business) ──────────────────
        # If this project's group has the Android bot configured, spin up a
        # SECOND run (bot_type="android") and hand it to the bot adapter.
        if project.group_id:
            group = (
                db.query(ApplicationGroup)
                .filter(ApplicationGroup.id == project.group_id)
                .first()
            )
            if group is not None and group.android_bot_url:
                from automation.api.v1.routers.jobs import trigger_android_bot

                android_run_id = str(uuid.uuid4())
                android_now = datetime.utcnow()
                database.insert_test_run(db, {
                    "id": android_run_id,
                    "project_id": project.id,
                    "test_suite": f"{group.name} (Android cross-app)",
                    "test_name": "Consumer + Business scenarios",
                    "status": "queued",
                    "job_state": "queued",
                    "started_at": android_now,
                    "created_at": android_now,
                    "device_name": "android-bot",
                    "platform": "Android",
                    "triggered_by": f"github_webhook:{repo_name}",
                    "branch": branch,
                    "commit_sha": commit_sha,
                    "bot_type": "android",
                })
                android_run = db.query(TestRun).filter_by(id=android_run_id).first()
                try:
                    trigger_android_bot(
                        db, android_run, scenarios=[],
                        consumer_device=None, business_device=None,
                        bot_url=group.android_bot_url,
                    )
                except Exception as exc:
                    logger.warning("Android bot trigger failed: %s", exc)
                run_ids.append(android_run_id)

    module_count = len(affected_modules)
    message = (
        f"Running {len(selected_tests)} test{'s' if len(selected_tests) != 1 else ''} "
        f"for {module_count} affected module{'s' if module_count != 1 else ''}"
    )

    logger.info(
        "GitHub webhook triggered: repo=%s pr=%s modules=%s tests=%s runs=%d",
        repo_name, pr_number, affected_modules, selected_tests, len(run_ids),
    )

    ios_run_id = run_ids[0] if run_ids else None
    both = message + (" | iOS + Android tests triggered" if android_run_id else "")

    return {
        "status": "triggered",
        "pr_number": pr_number,
        "affected_modules": affected_modules,
        "selected_tests": selected_tests,
        # Surfaced, not swallowed: a recommended file that is not on disk is a
        # coverage hole, and "no_coverage" means this PR ran nothing at all.
        "missing_tests": missing_tests,
        "no_coverage": no_coverage,
        "ios_run_id": ios_run_id,
        "android_run_id": android_run_id,
        "run_ids": run_ids,
        # Cross-app impact (empty unless the project belongs to an AppGroup).
        "cross_app_impact": cross_app_impact,
        "affected_endpoints": affected_endpoints,
        "graphify_nodes": graphify_nodes,
        "message": both,
    }
