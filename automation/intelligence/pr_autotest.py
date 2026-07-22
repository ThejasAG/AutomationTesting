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
from typing import Any, Dict, List, Optional

logger = logging.getLogger("pr_autotest")


def _device() -> str:
    """The simulator PR tests run on (pinned, not booted-wins)."""
    return os.getenv("PR_TEST_IOS_DEVICE", "").strip() or "booted"


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
    from automation.api.v1.routers.scenario import ScenarioRequest, run_scenario_headless
    from automation.api.v1.routers.pull_requests import _owner_repo, _github

    with SessionLocal() as db:
        project = db.query(TestProject).filter(TestProject.id == project_id).first()
        if not project:
            return {"ok": False, "error": "project not found"}
        owner, repo = _owner_repo(project.git_url)
        scenarios = [
            {"id": s.id, "name": s.name, "description": s.description, "steps": s.steps or []}
            for s in db.query(SavedScenario).filter(
                (SavedScenario.project_id == project_id) | (SavedScenario.project_id.is_(None))
            ).all()
        ]

    gh = _github()
    plan = plan_pr_tests(gh, owner, repo, pr_number, scenarios)
    selected = plan.get("selected_scenarios") or []
    device = _device()

    prep_error = None
    results: List[Dict[str, Any]] = []
    if selected:
        # Build + install the PR branch ONCE, then run every scenario against it.
        meta = gh.fetch_pr_metadata(owner, repo, pr_number)
        branch = meta.get("branch")
        prep = preparation_service.prepare_for_execution(project_id, device_id=device, branch=branch)
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

    return {
        "ok": True, "pr_number": pr_number, "selected": len(selected),
        "results": results, "comment_posted": posted, "comment": comment,
    }
