"""PR comment bot — posts a QA verdict comment and a commit status back onto the GitHub PR."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests

from automation.notifications.dispatch import dashboard_url

logger = logging.getLogger("pr_comment")

GITHUB_API = "https://api.github.com"
STATUS_CONTEXT = "Vyapy QA Platform"


def _token() -> Optional[str]:
    return os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")


def _default_owner() -> str:
    return os.getenv("GITHUB_REPO_OWNER", "xorstack-admin")


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"token {_token()}",
        "Accept": "application/vnd.github+json",
    }


class PRCommentBot:
    def post_pr_comment(self, pr_number: int, repo: str,
                        run_results: Dict[str, Any], owner: Optional[str] = None) -> bool:
        """Post a formatted QA-verdict comment on the PR."""
        if not _token():
            logger.info("[pr-comment suppressed, no GITHUB_TOKEN] PR #%s", pr_number)
            return False
        owner = owner or _default_owner()
        body = self._render(run_results)
        url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        try:
            res = requests.post(url, headers=_headers(), json={"body": body}, timeout=15)
            ok = res.status_code in (200, 201)
            if not ok:
                logger.warning("PR comment failed %s: %s", res.status_code, res.text[:200])
            return ok
        except Exception as e:
            logger.warning("PR comment error: %s", e)
            return False

    def update_pr_status(self, repo: str, sha: str, state: str, description: str,
                         owner: Optional[str] = None, target_url: Optional[str] = None) -> bool:
        """Set a commit status (success | failure | pending | error) under our context."""
        if not _token() or not sha:
            return False
        owner = owner or _default_owner()
        url = f"{GITHUB_API}/repos/{owner}/{repo}/statuses/{sha}"
        payload = {
            "state": state,
            "description": description[:140],
            "context": STATUS_CONTEXT,
        }
        if target_url:
            payload["target_url"] = target_url
        try:
            res = requests.post(url, headers=_headers(), json=payload, timeout=15)
            ok = res.status_code in (200, 201)
            if not ok:
                logger.warning("PR status failed %s: %s", res.status_code, res.text[:200])
            return ok
        except Exception as e:
            logger.warning("PR status error: %s", e)
            return False

    # ── rendering ────────────────────────────────────────────────────────────
    def _render(self, r: Dict[str, Any]) -> str:
        verdict = str(r.get("verdict") or r.get("status") or "unknown").lower()
        total = r.get("total", 0)
        passed = r.get("passed", 0)
        failed = r.get("failed", 0)
        run_id = r.get("run_id", "")

        if verdict in ("no-tests", "no_tests"):
            head = "### 🟡 Vyapy QA — No tests ran\nNo tagged scenarios covered these changes. This is **not** a pass — add scenarios to get coverage."
        elif verdict in ("passed", "success") and not failed:
            head = "### ✅ Vyapy QA — Passed"
        else:
            head = "### ❌ Vyapy QA — Failed"

        lines: List[str] = [head, ""]
        lines.append("| Metric | Value |")
        lines.append("| --- | --- |")
        lines.append(f"| Scenarios | {total} |")
        lines.append(f"| Passed | {passed} |")
        lines.append(f"| Failed | {failed} |")
        if r.get("flaky"):
            lines.append(f"| Flaky | {r.get('flaky')} |")
        if r.get("duration_ms") is not None:
            lines.append(f"| Duration | {round((r.get('duration_ms') or 0)/1000, 1)}s |")

        # AI root-cause analysis
        rca = r.get("rca") or r.get("root_cause")
        if rca:
            lines += ["", "**🤖 AI Root-Cause Analysis**", "", f"> {str(rca)[:600]}"]

        # Visual regression
        vr = r.get("visual_regression") or {}
        vr_regs = vr.get("regressions") if isinstance(vr, dict) else None
        if vr_regs:
            lines += ["", f"**🖼️ Visual regressions ({len(vr_regs)})**", "",
                      "| Screen | Diff % | Severity |", "| --- | --- | --- |"]
            for g in vr_regs[:8]:
                lines.append(f"| {g.get('screen')} | {g.get('diff_percentage')}% | {g.get('severity')} |")

        # AI summary
        if r.get("summary"):
            lines += ["", str(r["summary"])[:600]]

        if run_id:
            lines += ["", f"[📊 Full report]({dashboard_url()}/runs/{run_id})"]
        lines += ["", "<sub>Posted automatically by the Vyapy QA Platform.</sub>"]
        return "\n".join(lines)


pr_comment_bot = PRCommentBot()
