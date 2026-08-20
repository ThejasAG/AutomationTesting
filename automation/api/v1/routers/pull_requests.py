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
from typing import Optional

from pydantic import BaseModel

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
from automation.projects.builder import app_builder

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


_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def _attach_tickets(prs: list, db: Session) -> list:
    """Link each PR to its ticket description, best-effort (never breaks the PR list):
      1) a LOCAL ticket matched by explicit pr_number,
      2) a LOCAL ticket whose key (match_key, e.g. NEWVYA-1134) is in the PR title/branch,
      3) else pull the key from the PR title/branch and fetch the description LIVE from Jira
         (JiraIntegration.from_env() — active only when JIRA_URL/EMAIL/TOKEN are set).
    Attaches ticket_key / ticket_title / ticket_description (+ ticket_status/url/source)."""
    try:
        from automation.database.models import Ticket
        tickets = db.query(Ticket).all()
    except Exception:
        tickets = []
    try:
        from automation.integrations.jira import JiraIntegration
        jira = JiraIntegration.from_env()      # None unless configured in .env
    except Exception:
        jira = None
    jira_cache: dict = {}
    # Generic key pattern. A branch like 'NEWVYA-BUGS-LIST-2' can yield a key-shaped
    # 'LIST-2'; get_issue() returns {} quietly on a 404, so a bogus key just shows no ticket
    # (no noise). This is more robust than scoping to /project keys, which the account can't
    # always list even when it CAN read the individual issues.

    for pr in prs:
        num = str(pr.get("number") or "")
        hay = f"{pr.get('title', '')} {pr.get('branch', '')}"
        low = hay.lower()
        # 1) local ticket by explicit PR number
        match = next((t for t in tickets
                      if t.pr_number and str(t.pr_number).lstrip("#").strip() == num), None)
        # 2) local ticket by key in title/branch
        if match is None:
            match = next((t for t in tickets
                          if (getattr(t, "match_key", "") or "").strip()
                          and (getattr(t, "match_key", "") or "").strip().lower() in low), None)
        if match is not None:
            pr["ticket_key"] = (getattr(match, "match_key", None)
                                or (str(match.pr_number) if match.pr_number else None))
            pr["ticket_title"] = getattr(match, "title", None)
            pr["ticket_description"] = match.description
            pr["ticket_source"] = "local"
            continue
        # 3) no local ticket — extract the key and fetch live from Jira
        m = _JIRA_KEY_RE.search(hay)
        if m and jira is not None:
            key = m.group(1)
            issue = jira_cache.get(key)
            if issue is None:
                issue = jira.get_issue(key) or {}
                jira_cache[key] = issue
            if issue.get("description") or issue.get("summary"):
                pr["ticket_key"] = key
                pr["ticket_title"] = issue.get("summary")
                pr["ticket_description"] = issue.get("description")
                pr["ticket_status"] = issue.get("status")
                pr["ticket_url"] = issue.get("url")
                pr["ticket_source"] = "jira"
    return prs


def _ticket_for_pr(project_id: str, number: int, db: Session) -> dict:
    """The ticket linked to one PR, as {key, title, description}, or {}.

    Reuses the SAME enrichment the PR list uses, so planning sees exactly the ticket
    the dashboard shows — rather than the two disagreeing about which ticket a PR is for.
    """
    try:
        project = _get_project(project_id, db)
        owner, repo = _owner_repo(project.git_url)
        prs = _attach_tickets([_github().fetch_pr_metadata(owner, repo, number)
                               | {"number": number}], db)
        pr = prs[0] if prs else {}
        if not pr.get("ticket_key"):
            return {}
        return {"key": pr.get("ticket_key"), "title": pr.get("ticket_title"),
                "description": pr.get("ticket_description")}
    except Exception as e:                       # planning must never fail over a ticket
        logger.warning("ticket lookup for PR #%s failed: %s", number, e)
        return {}


@router.get("/{project_id}/pulls", dependencies=[Depends(get_current_user)])
def list_pulls(project_id: str, db: Session = Depends(get_db)):
    """Open pull requests plus recently-merged ones for the project's GitHub repo."""
    project = _get_project(project_id, db)
    owner, repo = _owner_repo(project.git_url)
    try:
        prs = _github().list_prs(owner, repo)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 502
        detail = "GitHub rejected the request (check GITHUB_TOKEN access to this repo)." \
            if code in (401, 403, 404) else f"GitHub API error ({code})."
        raise HTTPException(status_code=502, detail=detail)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Could not reach GitHub: {e}")

    prs = _attach_tickets(prs, db)   # link each PR to its ticket description
    return {"project_id": project_id, "repo": f"{owner}/{repo}", "pull_requests": prs}


@router.get("/{project_id}/pulls/{number}/plan", dependencies=[Depends(get_current_user)])
def plan_pull(project_id: str, number: int, db: Session = Depends(get_db)):
    """Work out WHAT to test for this PR and HOW to reach it.

    Reads the PR (title, description, changed files), then uses the local model to
    pick the saved full-path scenarios that verify the change end-to-end (e.g. a
    payment fix → the login→cart→checkout→pay scenario), with reasoning and gaps.
    """
    from automation.database.models import SavedScenario
    from automation.intelligence.pr_planner import plan_pr_tests

    project = _get_project(project_id, db)
    owner, repo = _owner_repo(project.git_url)
    scenarios = [
        {"id": s.id, "name": s.name, "description": s.description, "steps": s.steps or [], "covers": s.covers or []}
        for s in db.query(SavedScenario).filter(
            (SavedScenario.project_id == project_id) | (SavedScenario.project_id.is_(None))
        ).all()
    ]
    try:
        return plan_pr_tests(_github(), owner, repo, number, scenarios,
                             project_id=project_id,
                             ticket=_ticket_for_pr(project_id, number, db))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/{project_id}/pulls/{number}/autotest", dependencies=[Depends(get_current_user)])
def autotest_pull(project_id: str, number: int, db: Session = Depends(get_db)):
    """Autonomous QA for a PR: plan → build the PR branch → run the selected
    scenarios → comment the verdict on the PR. Runs in the background (heavy)."""
    import threading
    from automation.intelligence.pr_autotest import run_pr_autotest

    _get_project(project_id, db)  # 404 if missing
    threading.Thread(
        target=run_pr_autotest, args=(project_id, number), daemon=True,
        name=f"pr-autotest-{number}",
    ).start()
    return {"started": True, "pr_number": number,
            "message": "Autonomous QA started — it will comment the result on the PR."}


class PRTestBody(BaseModel):
    """Optional overrides for a PR test run."""
    device_id: Optional[str] = None      # which simulator to run on


@router.post("/{project_id}/pulls/{number}/test", dependencies=[Depends(get_current_user)])
def test_pull(project_id: str, number: int, body: PRTestBody = PRTestBody(),
              db: Session = Depends(get_db)):
    """Queue a test run against a PR's head branch, optionally on a chosen simulator."""
    project = _get_project(project_id, db)
    owner, repo = _owner_repo(project.git_url)

    meta = _github().fetch_pr_metadata(owner, repo, number)
    # For a MERGED PR the head branch is usually deleted and the code already lives on
    # the base branch — so test the base branch (the merge itself). For an open PR we
    # test its head branch (pre-merge verification).
    branch = meta.get("base") if meta.get("merged_at") else meta.get("branch")
    if not branch:
        branch = meta.get("branch") or meta.get("base")
    if not branch:
        raise HTTPException(
            status_code=502, detail=f"Could not resolve a branch to test for PR #{number}."
        )

    # Same device-selection priority as the webhook: online iOS → online Android →
    # 'pending' (queued with no device; picked up when one comes online). Only
    # freshly-heartbeating devices count, so a dead agent's stale UDID is skipped.
    online = device_service.get_online_devices()

    # 1. An EXPLICIT choice wins — the caller picked a simulator in the Run dialog.
    # 2. Then PR_TEST_IOS_DEVICE, for pinning PR tests to a prepared simulator.
    # 3. Only then the automatic pick.
    #
    # Auto-pick alone chose whichever iOS device happened to be online, which sent a
    # PR run to a BRAND-NEW simulator: a fresh install sits on the first-run onboarding
    # carousel, not signed in, so every scenario timed out looking for Home and the run
    # cycled for 10 minutes producing nothing. Which simulator runs a test is a real
    # choice — a virgin sim and a prepared one are not interchangeable.
    chosen = (body.device_id if body and body.device_id else "") or \
        os.getenv("PR_TEST_IOS_DEVICE", "")
    device = None
    if chosen:
        device = next((d for d in online if d.id == chosen), None)
    if device is None and not chosen:
        device = (
            next((d for d in online if (d.platform or "").lower() == "ios"), None)
            or next((d for d in online if (d.platform or "").lower() == "android"), None)
        )

    platform = (device.platform if device else (project.platform or "iOS"))
    device_name = (chosen or (device.id if device else "pending"))
    # For iOS, store a device that actually exists on this host (prefer a booted
    # sim) so the run does not fall back at build time on a stale/foreign UDID.
    if platform.lower() == "ios":
        resolved, _ = app_builder.resolve_ios_device(device_name if device_name != "pending" else None)
        if resolved:
            device_name = resolved

    # Plan FIRST, and carry the result on the job, so the agent runs what was actually
    # selected for this change instead of the project's fixed command. Best-effort: if
    # planning fails (no model, no scenarios, no graph) the job still runs the default.
    planned = None
    try:
        from automation.database.models import SavedScenario
        from automation.intelligence.pr_planner import plan_pr_tests
        scenarios = [
            {"id": s.id, "name": s.name, "description": s.description,
             "steps": s.steps or [], "covers": s.covers or []}
            for s in db.query(SavedScenario).filter(
                (SavedScenario.project_id == project.id) | (SavedScenario.project_id.is_(None))
            ).all()
        ]
        if scenarios:
            plan = plan_pr_tests(_github(), owner, repo, number, scenarios,
                                 project_id=project.id,
                                 ticket=_ticket_for_pr(project.id, number, db))
            planned = [{"id": s.get("id"), "name": s.get("name"), "steps": s.get("steps") or []}
                       for s in (plan.get("selected_scenarios") or []) if s.get("steps")]
    except Exception as e:
        logger.warning("Could not plan PR #%s (job will use the default command): %s", number, e)

    now = datetime.utcnow()
    run_id = str(uuid.uuid4())
    database.insert_test_run(db, {
        "planned_scenarios": planned or None,
        "id": run_id,
        "project_id": project.id,
        "test_suite": project.name,
        "test_name": f"PR #{number}: {meta.get('title') or branch}",
        "status": "queued",
        "job_state": "queued",
        "started_at": now,
        "created_at": now,
        "device_name": device_name,
        "os_version": device.platform_version if device else None,
        "platform": platform,
        "triggered_by": f"pr_test:{owner}/{repo}#{number}",
        "branch": branch,
        "commit_sha": meta.get("commit_sha"),
    })

    logger.info("Queued PR test: %s/%s#%s branch=%s run=%s", owner, repo, number, branch, run_id)
    return {"status": "queued", "run_id": run_id, "branch": branch, "pr_number": number}
