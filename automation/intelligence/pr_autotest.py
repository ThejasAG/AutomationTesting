"""Autonomous PR test loop.

For a pull request: plan which full-path scenarios verify the change, check out
and build the PR branch, run those scenarios, and post the result back as a PR
comment. This is the capstone that makes the platform hands-off — a developer
opens a PR and gets a QA verdict where they work.

Runs headless (no UI stream). Heavy (build + Appium per scenario), so callers run
it in a background thread.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("pr_autotest")

# One PR test at a time. prepare_for_execution() checks out a branch in the
# project's single working tree and installs onto one simulator, so two
# concurrent PR tests build over each other and both report garbage. The
# second caller waits rather than being rejected — its run row already exists
# and shows as running, so a queued PR is visible instead of silently dropped.
# ponytail: one global lock; make it per-project if two projects ever run PR QA
# on separate simulators at once.
_PR_TEST_LOCK = threading.Lock()

# Marks the PR-level run row. The startup reaper in api/main.py uses this to
# mark a PR test killed by a restart as failed instead of leaving it 'running'.
PR_QA_BOT_TYPE = "ios-pr-qa"


def _device() -> str:
    """The simulator PR tests run on (pinned, not booted-wins)."""
    return os.getenv("PR_TEST_IOS_DEVICE", "").strip() or "booted"


def _persist_pr_run(project_id: str, pr_number: int, sha: str, branch: Optional[str],
                    **fields: Any) -> Optional[str]:
    """Upsert the PR-level run row (one per commit, so a re-test replaces it).

    This row is what the PR poller's commit-sha dedup looks for. Without it the
    autotest path left no trace of the SHA, so the poller re-tested every open
    PR on every cycle, forever — stacking builds onto one simulator.
    """
    from automation.database.config import SessionLocal
    from automation.database import database

    run_id = f"prqa-{project_id[:8]}-{pr_number}-{(sha or 'nosha')[:8]}"
    try:
        with SessionLocal() as db:
            database.insert_test_run(db, {
                "id": run_id, "project_id": project_id,
                "test_suite": "PR AutoQA", "test_name": f"PR #{pr_number}",
                "device_name": _device(), "platform": "iOS",
                "bot_type": PR_QA_BOT_TYPE, "triggered_by": "pr-autotest",
                "branch": branch, "commit_sha": sha,
                **fields,
            })
        return run_id
    except Exception as e:
        logger.warning("PR run persist failed for #%s: %s", pr_number, e)
        return None


def _build_comment(plan: Dict[str, Any], results: List[Dict[str, Any]],
                   prepared: bool, prep_error: Optional[str]) -> str:
    pr = plan.get("pr_number")
    areas = ", ".join(plan.get("affected_areas") or []) or "—"
    lines = [f"### 🤖 Automated QA — PR #{pr}", "", f"**Affected area:** {areas}  "]
    if plan.get("summary"):
        lines.append(f"**Change:** {plan['summary']}  ")
    lines.append("")

    if prep_error:
        lines += [f"⚠️ Could not build/install the PR branch: `{prep_error}`", "",
                  "_No scenarios were run._"]
        return "\n".join(lines)

    if not results:
        miss = plan.get("missing_coverage") or "record a scenario that reaches this feature."
        lines += ["⚠️ **No saved scenario covers this change.**", "",
                  f"Suggested: {miss}"]
        return "\n".join(lines)

    total_pass = sum(1 for r in results if r["outcome"].get("ok"))
    overall = "✅ PASS" if total_pass == len(results) else "❌ FAIL"
    lines.append(f"**Result: {overall}** — {total_pass}/{len(results)} scenarios passed")
    lines.append("")
    lines.append("| Scenario | Steps | Result |")
    lines.append("|---|---|---|")
    for r in results:
        o = r["outcome"]
        mark = "✅" if o.get("ok") else "❌"
        steps = f"{o.get('passed', 0)}/{o.get('total', 0)}"
        note = "" if o.get("ok") else f" — {(o.get('error') or 'a step failed')[:80]}"
        lines.append(f"| {r['name']} | {steps} | {mark}{note} |")
    lines += ["", "_Ran via the automation platform against the PR branch._"]
    return "\n".join(lines)


def run_pr_autotest(project_id: str, pr_number: int, post_comment: bool = True) -> Dict[str, Any]:
    """Plan → build PR branch → run the selected scenarios → comment on the PR."""
    from automation.database.config import SessionLocal
    from automation.database.models import SavedScenario, TestProject
    from automation.integrations.github import GitHubIntegration
    from automation.intelligence.pr_planner import plan_pr_tests
    from automation.projects.preparation import preparation_service
    from automation.scenarios import run_records  # noqa: F401 — backend run bookkeeping
    from automation.scenarios.service import ScenarioRequest, run_scenario_headless
    from automation.api.v1.routers.pull_requests import _owner_repo, _github

    with SessionLocal() as db:
        project = db.query(TestProject).filter(TestProject.id == project_id).first()
        if not project:
            return {"ok": False, "error": "project not found"}
        owner, repo = _owner_repo(project.git_url)
        scenarios = [
            {"id": s.id, "name": s.name, "description": s.description, "steps": s.steps or [], "covers": s.covers or []}
            for s in db.query(SavedScenario).filter(
                (SavedScenario.project_id == project_id) | (SavedScenario.project_id.is_(None))
            ).all()
        ]

    gh = _github()
    device = _device()

    # Record the run BEFORE the slow work (planning is an LLM call, prepare is a
    # build): the SHA has to be on the board straight away or the poller starts
    # this same PR again on its next cycle.
    meta = gh.fetch_pr_metadata(owner, repo, pr_number)
    started = datetime.utcnow()
    run_id = _persist_pr_run(project_id, pr_number, meta.get("commit_sha"), meta.get("branch"),
                             status="running", job_state="running",
                             started_at=started, created_at=started)

    plan = plan_pr_tests(gh, owner, repo, pr_number, scenarios, project_id=project_id)
    selected = plan.get("selected_scenarios") or []

    prep_error = None
    results: List[Dict[str, Any]] = []
    if selected:
        # Build + install the PR branch ONCE, then run every scenario against it.
        # Serialized: one checkout + one simulator, shared by every PR test.
        with _PR_TEST_LOCK:
            prep = preparation_service.prepare_for_execution(
                project_id, device_id=device, branch=meta.get("branch"))
            if not prep.ok:
                prep_error = (prep.error or "prepare failed")[:200]
            else:
                with SessionLocal() as db:
                    for sc in selected:
                        req = ScenarioRequest(
                            project_id=project_id, steps=sc["steps"], device_id=device,
                            name=sc["name"], save=False, prepare=False,
                        )
                        outcome = run_scenario_headless(req, db)
                        results.append({"name": sc["name"], "outcome": outcome})

    comment = _build_comment(plan, results, prepared=not prep_error, prep_error=prep_error)
    posted = False
    if post_comment:
        try:
            posted = gh.comment_on_pr(owner, repo, pr_number, comment)
        except Exception as e:
            logger.warning("Could not comment on PR #%s: %s", pr_number, e)

    # Fan out to Slack + Jira (no-op when unconfigured).
    from automation.notifications import dispatch
    failed = bool(results) and any(not r["outcome"].get("ok") for r in results)

    finished = datetime.utcnow()
    _persist_pr_run(
        project_id, pr_number, meta.get("commit_sha"), meta.get("branch"),
        status=("failed" if (failed or prep_error) else "passed" if results else "skipped"),
        job_state="completed", completed_at=finished,
        duration_ms=int((finished - started).total_seconds() * 1000),
        error_message=prep_error,
    )
    dispatch.send_slack_text(f"*QA — {owner}/{repo}#{pr_number}*\n{comment}")
    jira_key = None
    if failed or prep_error:
        fails = [r["name"] for r in results if not r["outcome"].get("ok")]
        desc = (f"Automated QA failed for PR #{pr_number} ({owner}/{repo}).\n\n"
                f"Affected: {', '.join(plan.get('affected_areas') or []) or '—'}\n"
                f"Failing scenarios: {', '.join(fails) or (prep_error or 'build/install')}\n\n{comment}")
        jira_key = dispatch.create_jira_ticket(
            summary=f"[Auto-QA] PR #{pr_number} failed: {plan.get('title') or ''}"[:200],
            description=desc)
        if jira_key:
            try:
                gh.comment_on_pr(owner, repo, pr_number, f"🐛 Filed Jira ticket **{jira_key}** for this failure.")
            except Exception:
                pass

    return {
        "ok": True, "pr_number": pr_number, "run_id": run_id, "selected": len(selected),
        "results": results, "comment_posted": posted, "comment": comment,
        "jira_key": jira_key,
    }
