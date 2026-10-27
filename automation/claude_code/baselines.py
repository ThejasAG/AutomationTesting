"""Unit-test failures of a commit, measured once and kept.

A session's change must not add a failing test; tests that already fail on the
commit it started from (today: 2 stale consumer-checkout tests and the stray
test_automation_new.db check) are not its fault. Only when the session's own run had
failures are those tests re-run in a clean worktree of that commit (the full suite,
~6 min, only when one of them looks new); cached per commit in
logs/agent_sessions/baselines/.
"""
from __future__ import annotations

import json
import os
import shutil
from typing import Any, Callable, Dict, List, Optional

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


def _in_tree(root: str, nodeid: str) -> bool:
    """Does the commit checked out at `root` have this test? A test the session
    added is not there, and naming it would stop pytest before it runs anything."""
    path, _, rest = nodeid.partition("::")
    func = rest.split("::")[-1].split("[")[0]
    try:
        with open(os.path.join(root, path), encoding="utf-8") as f:
            return not func or f"def {func}(" in f.read()
    except OSError:
        return False


def _measure(commit: str, on_phase: Callable[[str], None],
             tests: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """pytest over a clean worktree of `commit`: every test, or only `tests`."""
    from automation.claude_code import gates, runner
    tmp = os.path.join(paths.WORKTREES_DIR, f"baseline-{commit[:10]}")
    if os.path.exists(tmp):
        runner.remove_worktree(tmp)
    runner.git(paths.MAIN_ROOT, "worktree", "add", "-q", "--detach", tmp, commit)
    try:
        runner.link_runtime(tmp)
        if tests is None:
            on_phase(f"measuring the unit tests of {commit[:8]} (once per commit, ~6 min)")
            return gates.run_unit_tests(tmp)
        there = [t for t in tests if _in_tree(tmp, t)]
        absent = [t for t in tests if t not in there]
        on_phase(f"re-running the {len(there)} failing test(s) on {commit[:8]}")
        if not there:
            return {"failed": [], "counts": {}, "returncode": 0, "absent": absent}
        return {**gates.run_unit_tests(tmp, there, timeout=600), "absent": absent}
    finally:
        runner.remove_worktree(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def get_or_measure(commit: str, on_phase: Callable[[str], None] = lambda m: None,
                   tests: Optional[List[str]] = None) -> Dict[str, Any]:
    """The base commit's failures, as far as `tests` (the session's failures) need.

    MEASURED 2026-10-27: every session's suite had failures (2 stale tests), so every
    session on a new base commit paid ~6 min for the full base suite. With `tests`,
    only those are re-run on the base (seconds). A test the base does not have is new
    by definition. When every other one fails on the base too, the answer is
    complete; when one passes there — a failure that looks new — the full base suite
    is measured before the gate calls it new (a test can fail only alongside others)."""
    have = get(commit)
    if have is not None and not have.get("partial"):
        return have
    if tests:
        have = have or {}
        known, failed = set(have.get("checked") or []), set(have.get("failed") or [])
        absent = set(have.get("absent") or [])
        todo = [t for t in tests if t not in known]
        if todo:
            res = _measure(commit, on_phase, todo)
            if res is not None and not res.get("timed_out") and res.get("returncode") in (0, 1, 5):
                failed |= set(res.get("failed") or [])
                absent |= set(res.get("absent") or [])
                known |= set(todo)
        if set(tests) <= known:
            part = {"commit": commit, "failed": sorted(failed), "counts": {},
                    "checked": sorted(known), "absent": sorted(absent), "partial": True}
            os.makedirs(paths.BASELINES_DIR, exist_ok=True)
            with open(_file(commit), "w") as f:
                json.dump(part, f, indent=1)
            if set(tests) - absent <= failed:
                return part
    res = _measure(commit, on_phase)
    if res is None or res.get("timed_out") or res.get("returncode") not in (0, 1):
        return {"commit": commit, "failed": [], "counts": {}, "unmeasured": True}
    return save(commit, res)
