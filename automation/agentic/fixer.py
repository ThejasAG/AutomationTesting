"""Applies what the triage agent proposed — only what is safe — and can undo it.

Test fixes go through CrossAppFlowEdit rows, the same override the flow editor
uses: the built-in flow in cross_app_flows.py is never touched, and rolling back
restores the previous row (or deletes it, reverting to the built-in).

App patches are never applied to the app checkouts (they carry local staging
config and are what the simulators run). A patch is checked with `git apply
--check`, saved, and — only when app_fix_mode is 'pr' — committed in a separate
git worktree, pushed to a new branch and opened as a pull request for a person
to review and merge.
"""
from __future__ import annotations

import copy
import inspect
import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime
from functools import lru_cache
from typing import Any, Dict, Optional, Set, Tuple

import httpx

from automation.agentic import sources
from automation.database.config import SessionLocal
from automation.database.models import CrossAppFlowEdit

logger = logging.getLogger("agentic.fixer")

PATCH_DIR = os.path.join(sources.ROOT, "logs", "agent_patches")

# A step that checks a value is the test's purpose; editing it to get a pass would
# hide the bug it exists to catch.
_CHECK_STEP = re.compile(r"verify|assert|expect|check|total|vat|amount|€|bill|change\b", re.I)


class FixRefused(Exception):
    pass


@lru_cache(maxsize=1)
def known_tokens() -> Tuple[Set[str], Set[str]]:
    """(@tokens, @prefixes:) the runner dispatches — read from its own source so a
    new handler is accepted the day it is added."""
    from automation.scenarios import cross_app_flows as caf
    src = inspect.getsource(caf.FlowRunner._handle_special)
    exact = set(re.findall(r'"(@[a-z_]+)"', src))
    prefixes = set(re.findall(r'"(@[a-z_]+:)"', src))
    for f in caf.list_flows():                     # every token a shipped flow uses
        for s in f["segments"]:
            for st in s["steps"]:
                if st.startswith("@") and ":" not in st:
                    exact.add(st.strip())
    return exact, prefixes


def token_ok(step: str) -> bool:
    step = step.strip()
    if not step.startswith("@"):
        return True
    exact, prefixes = known_tokens()
    return step in exact or any(step.startswith(p) for p in prefixes)


def _effective_flow(flow_id: str) -> Dict[str, Any]:
    from automation.scenarios.cross_app_flows import list_flows
    flow = next((f for f in list_flows() if f["id"] == flow_id), None)
    if not flow:
        raise FixRefused(f"no flow {flow_id}")
    return flow


def check_test_fix(flow_id: str, fix: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a proposed step replacement. Returns the new segments, or raises
    FixRefused with the reason (shown on the dashboard)."""
    seg_num = str(fix.get("segment_num") or "").strip()
    old = (fix.get("old_step") or "").strip()
    new = [s.strip() for s in (fix.get("new_steps") or []) if s and s.strip()]
    if not (seg_num and old and new):
        raise FixRefused("incomplete fix (segment, old step and new steps are all required)")
    if _CHECK_STEP.search(old):
        raise FixRefused(f"'{old}' checks a value — it is never edited automatically")
    bad = [s for s in new if not token_ok(s)]
    if bad:
        raise FixRefused(f"unknown step handler(s): {', '.join(bad)}")
    flow = _effective_flow(flow_id)
    segs = copy.deepcopy(flow["segments"])
    seg = next((s for s in segs if str(s["num"]) == seg_num), None)
    if not seg:
        raise FixRefused(f"flow {flow_id} has no segment {seg_num}")
    if old not in seg["steps"]:
        raise FixRefused(f"segment {seg_num} has no step '{old}'")
    i = seg["steps"].index(old)
    seg["steps"][i:i + 1] = new
    return {"name": flow["name"], "description": flow.get("description") or "", "segments": segs}


def apply_test_fix(flow_id: str, fix: Dict[str, Any]) -> Dict[str, Any]:
    """Write the fix as a flow edit. Returns the snapshot rollback() needs."""
    new = check_test_fix(flow_id, fix)
    from automation.scenarios.cross_app_flows import FLOWS
    with SessionLocal() as db:
        row = db.get(CrossAppFlowEdit, flow_id)
        snapshot = {"existed": row is not None,
                    "row": row.to_dict() if row else None}
        if row is None:
            row = CrossAppFlowEdit(id=flow_id, based_on=flow_id if flow_id in FLOWS else None)
            db.add(row)
        row.name, row.description, row.segments = new["name"], new["description"], new["segments"]
        db.commit()
    return snapshot


def rollback(flow_id: str, snapshot: Dict[str, Any]) -> None:
    with SessionLocal() as db:
        row = db.get(CrossAppFlowEdit, flow_id)
        if not snapshot.get("existed"):
            if row is not None:
                db.delete(row)
        else:
            prev = snapshot["row"]
            if row is None:
                row = CrossAppFlowEdit(id=flow_id)
                db.add(row)
            row.name, row.description = prev["name"], prev.get("description")
            row.segments, row.based_on = prev["segments"], prev.get("based_on")
        db.commit()


# ── App patches ──────────────────────────────────────────────────────────────

def check_patch(app: str, patch: str) -> Tuple[bool, str]:
    repo = sources.app_repo(app)
    if not repo:
        return False, f"no checkout of the {app} app"
    if not patch.strip():
        return False, "empty patch"
    if not patch.endswith("\n"):
        patch += "\n"
    p = subprocess.run(["git", "apply", "--check", "--recount", "-"], cwd=repo, input=patch,
                       capture_output=True, text=True, timeout=30)
    return p.returncode == 0, (p.stderr.strip() or "applies cleanly")


def save_patch(action_id: str, patch: str) -> str:
    os.makedirs(PATCH_DIR, exist_ok=True)
    path = os.path.join(PATCH_DIR, f"{action_id}.diff")
    with open(path, "w", encoding="utf-8") as f:
        f.write(patch if patch.endswith("\n") else patch + "\n")
    return path


def _git(args, cwd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          timeout=kw.pop("timeout", 120), **kw)


def open_pull_request(app: str, patch: str, title: str, body: str) -> Dict[str, Any]:
    """Branch off the checkout's upstream branch in a throwaway worktree, apply,
    commit, push and open a PR. The checkout the simulators use is not touched."""
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise FixRefused("GITHUB_TOKEN is not set")
    repo = sources.app_repo(app)
    if not repo:
        raise FixRefused(f"no checkout of the {app} app")
    base = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip()
    url = _git(["remote", "get-url", "origin"], repo).stdout.strip()
    m = re.search(r"github\.com[/:]([^/]+)/([^/.]+)", url)
    if not m:
        raise FixRefused(f"origin is not a GitHub repo ({url})")
    owner, name = m.group(1), m.group(2)
    branch = f"agent-fix/{datetime.now():%Y%m%d-%H%M}-{re.sub(r'[^a-z0-9]+', '-', title.lower())[:30].strip('-')}"
    auth_url = f"https://x-access-token:{token}@github.com/{owner}/{name}.git"

    fetch = _git(["fetch", auth_url, base], repo, timeout=300)
    if fetch.returncode != 0:
        raise FixRefused(f"git fetch failed: {fetch.stderr.strip()[-300:]}")
    tmp = tempfile.mkdtemp(prefix="agent-fix-")
    wt = os.path.join(tmp, "wt")
    try:
        r = _git(["worktree", "add", "-b", branch, wt, "FETCH_HEAD"], repo)
        if r.returncode != 0:
            raise FixRefused(f"worktree failed: {r.stderr.strip()[-300:]}")
        r = subprocess.run(["git", "apply", "--recount", "-"], cwd=wt,
                           input=patch if patch.endswith("\n") else patch + "\n",
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise FixRefused("patch does not apply to the upstream branch "
                             f"(it may touch locally-modified files): {r.stderr.strip()[-300:]}")
        _git(["add", "-A"], wt)
        r = _git(["-c", "user.name=Vya Test Agent", "-c", "user.email=test-agent@localhost",
                  "commit", "-m", title, "-m", body], wt)
        if r.returncode != 0:
            raise FixRefused(f"commit failed: {r.stderr.strip()[-300:]}")
        r = _git(["push", auth_url, f"{branch}:{branch}"], wt, timeout=300)
        if r.returncode != 0:
            raise FixRefused(f"push failed: {r.stderr.replace(token, '***').strip()[-300:]}")
    finally:
        _git(["worktree", "remove", "--force", wt], repo)
        _git(["branch", "-D", branch], repo)

    resp = httpx.post(f"https://api.github.com/repos/{owner}/{name}/pulls",
                      headers={"Authorization": f"Bearer {token}",
                               "Accept": "application/vnd.github+json"},
                      json={"title": title, "head": branch, "base": base, "body": body,
                            "draft": True}, timeout=30)
    if resp.status_code >= 300:
        raise FixRefused(f"branch {branch} pushed, but the PR could not be opened: "
                         f"HTTP {resp.status_code} {resp.text[:200]}")
    return {"branch": branch, "pr_url": resp.json().get("html_url"), "base": base}
