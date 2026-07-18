"""Pull-request testing.

List a project's open PRs and queue a test run against a PR's branch — so a
change can be verified BEFORE it merges, instead of waiting for the post-merge
webhook. Reuses the same run-creation shape as the GitHub webhook.
"""

import logging
import os
import re
import subprocess
import uuid
from datetime import datetime

import requests
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from automation.auth.security import get_current_user
from automation.database import database
from automation.database.config import get_db
from automation.database.models import TestProject
from automation.device_manager.models import DeviceStatus
from automation.device_manager.service import device_service
from automation.integrations.github import GitHubIntegration

logger = logging.getLogger("pull_requests")

router = APIRouter(prefix="/projects", tags=["Pull Requests"])

_GITHUB_URL_RE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")


def _owner_repo(git_url: str):
    m = _GITHUB_URL_RE.search(git_url or "")
    if not m:
        raise HTTPException(
            status_code=400,
            detail=f"Project git_url is not a GitHub URL: {git_url!r}",
        )
    return m.group(1), m.group(2)


def _token_from_git_credentials() -> str:
    """The token git already uses for github.com.

    The private repos were cloned over HTTPS, so git has a working credential in
    the OS keychain (a gho_/ghp_ token with repo scope). Reusing it means the user
    needs NO personal access token and NO org 'settings' permission — the same
    credential that clones the repo can read its pull requests.
    """
    try:
        out = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return ""
    for line in out.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    return ""


def _github() -> GitHubIntegration:
    token = os.environ.get("GITHUB_TOKEN", "") or _token_from_git_credentials()
    if not token:
        raise HTTPException(
            status_code=503,
            detail="No GitHub credential available. Either set GITHUB_TOKEN in .env, "
                   "or sign in to git once (cloning a private repo over HTTPS stores a "
                   "token in the OS keychain that the platform can reuse).",
        )
    return GitHubIntegration(token)


def _get_project(project_id: str, db: Session) -> TestProject:
    p = db.query(TestProject).filter(TestProject.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    return p


@router.get("/{project_id}/pulls", dependencies=[Depends(get_current_user)])
def list_pulls(project_id: str, db: Session = Depends(get_db)):
    """Open pull requests for the project's GitHub repo."""
    project = _get_project(project_id, db)
    owner, repo = _owner_repo(project.git_url)
    try:
        prs = _github().list_open_prs(owner, repo)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 502
        detail = "GitHub rejected the request (check GITHUB_TOKEN access to this repo)." \
            if code in (401, 403, 404) else f"GitHub API error ({code})."
        raise HTTPException(status_code=502, detail=detail)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Could not reach GitHub: {e}")

    return {"project_id": project_id, "repo": f"{owner}/{repo}", "pull_requests": prs}


@router.post("/{project_id}/pulls/{number}/test", dependencies=[Depends(get_current_user)])
def test_pull(project_id: str, number: int, db: Session = Depends(get_db)):
    """Queue a test run against a PR's head branch."""
    project = _get_project(project_id, db)
    owner, repo = _owner_repo(project.git_url)

    meta = _github().fetch_pr_metadata(owner, repo, number)
    branch = meta.get("branch")
    if not branch:
        raise HTTPException(
            status_code=502, detail=f"Could not resolve the head branch for PR #{number}."
        )

    # Same device-selection priority as the webhook: online iOS → online Android →
    # 'pending' (queued with no device; picked up when one comes online).
    online = [d for d in device_service.get_all_devices() if d.status == DeviceStatus.ONLINE]
    device = (
        next((d for d in online if (d.platform or "").lower() == "ios"), None)
        or next((d for d in online if (d.platform or "").lower() == "android"), None)
    )

    now = datetime.utcnow()
    run_id = str(uuid.uuid4())
    database.insert_test_run(db, {
        "id": run_id,
        "project_id": project.id,
        "test_suite": project.name,
        "test_name": f"PR #{number}: {meta.get('title') or branch}",
        "status": "queued",
        "job_state": "queued",
        "started_at": now,
        "created_at": now,
        "device_name": device.id if device else "pending",
        "os_version": device.platform_version if device else None,
        "platform": device.platform if device else (project.platform or "iOS"),
        "triggered_by": f"pr_test:{owner}/{repo}#{number}",
        "branch": branch,
        "commit_sha": meta.get("commit_sha"),
    })

    logger.info("Queued PR test: %s/%s#%s branch=%s run=%s", owner, repo, number, branch, run_id)
    return {"status": "queued", "run_id": run_id, "branch": branch, "pr_number": number}
