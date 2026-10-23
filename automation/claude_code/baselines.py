"""Unit-test failures of a commit, measured once and kept.

A session's change must not add a failing test; tests that already fail on the
commit it started from (today: 2 stale consumer-checkout tests and the stray
test_automation_new.db check) are not its fault. Measuring costs a full suite run
(~6 min) in a clean worktree of that commit, so it is done only when the session's
own run had failures, and cached per commit in logs/agent_sessions/baselines/.
"""
from __future__ import annotations

import json
import os
import shutil
from typing import Any, Callable, Dict, Optional

from automation.claude_code import paths


def _file(commit: str) -> str:
    return os.path.join(paths.BASELINES_DIR, f"{commit}.json")


def get(commit: str) -> Optional[Dict[str, Any]]:
    try:
        with open(_file(commit)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def latest() -> Optional[Dict[str, Any]]:
    if not os.path.isdir(paths.BASELINES_DIR):
        return None
    files = sorted((os.path.join(paths.BASELINES_DIR, n) for n in os.listdir(paths.BASELINES_DIR)
                    if n.endswith(".json")), key=os.path.getmtime, reverse=True)
    for p in files:
        try:
            with open(p) as f:
                return json.load(f)
        except (OSError, ValueError):
            continue
    return None


def save(commit: str, result: Dict[str, Any]) -> Dict[str, Any]:
    os.makedirs(paths.BASELINES_DIR, exist_ok=True)
    data = {"commit": commit, "failed": sorted(result.get("failed") or []),
            "counts": result.get("counts") or {}}
    with open(_file(commit), "w") as f:
        json.dump(data, f, indent=1)
    return data


def get_or_measure(commit: str, on_phase: Callable[[str], None] = lambda m: None) -> Dict[str, Any]:
    have = get(commit)
    if have is not None:
        return have
    from automation.claude_code import gates, runner
    tmp = os.path.join(paths.WORKTREES_DIR, f"baseline-{commit[:10]}")
    if os.path.exists(tmp):
        runner.remove_worktree(tmp)
    runner.git(paths.MAIN_ROOT, "worktree", "add", "-q", "--detach", tmp, commit)
    try:
        runner.link_runtime(tmp)
        on_phase(f"measuring the unit tests of {commit[:8]} (once per commit, ~6 min)")
        res = gates.run_unit_tests(tmp)
        if res.get("timed_out") or res.get("returncode") not in (0, 1):
            return {"commit": commit, "failed": [], "counts": {}, "unmeasured": True}
        return save(commit, res)
    finally:
        runner.remove_worktree(tmp)
        shutil.rmtree(tmp, ignore_errors=True)
