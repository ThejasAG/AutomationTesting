"""The checks a session's changes must pass before a person may approve them.

The session is told to verify its own work; these run AFTER it ends, in the
session runner, so nothing depends on the model having done what it said:

  scope        only automation/, tests/ and scripts/ changed (anything else is reverted)
  compiles     every changed .py compiles
  flows load   cross_app_flows.py imports and every @token has a handler
  checks kept  no flow lost a step that checks a value (total, VAT, amount, …)
               and no existing flow was deleted
  unit tests   the full suite has no failure the base commit did not already have
  on device    each flow whose steps changed, each new flow, and (for engine
               code) at least one flow the change reaches PASSED on the simulators
               after the last edit
  size         the change is small enough to review (settings.max_changed_lines,
               plus LINES_PER_NEW_FLOW for each new flow)
"""
from __future__ import annotations

import importlib.util
import json
import os
import py_compile
import re
import subprocess
import sys
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from automation.agentic.runinfo import CHECK_STEP
from automation.claude_code import impact

EDITABLE_DIRS = ("automation/", "tests/", "scripts/")
# The agent's own guardrails: changing them would let a session loosen its limits.
PROTECTED = ("automation/claude_code/", ".claude/", "scripts/agent_tools.py")
FLOWS_FILE = impact.FLOWS_FILE
DEVICE_CODE = ("automation/scenarios/", "automation/intelligence/", "automation/projects/")

_PY = sys.executable


def gate(name: str, ok: bool, detail: str = "") -> Dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail[:4000]}


# ── scope ────────────────────────────────────────────────────────────────────

def in_scope(path: str) -> bool:
    return path.startswith(EDITABLE_DIRS) and not path.startswith(PROTECTED)


def split_scope(paths: Iterable[str]) -> Tuple[List[str], List[str]]:
    keep, out = [], []
    for p in paths:
        (keep if in_scope(p) else out).append(p)
    return keep, out


# ── compile / import ─────────────────────────────────────────────────────────

def compile_check(root: str, paths: Iterable[str]) -> Dict[str, Any]:
    bad = []
    for p in paths:
        if p.endswith(".py") and os.path.exists(os.path.join(root, p)):
            try:
                py_compile.compile(os.path.join(root, p), doraise=True)
            except py_compile.PyCompileError as e:
                bad.append(f"{p}: {e.msg.strip()[:300]}")
    return gate("compiles", not bad, "\n".join(bad) or "every changed .py compiles")


def _py(root: str, code: str, timeout: int = 180) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=root)
    return subprocess.run([_PY, "-c", code], cwd=root, capture_output=True, text=True,
                          timeout=timeout, env=env)


_DUMP = (
    "import json\n"
    "from automation.scenarios.cross_app_flows import FLOWS, FlowRunner\n"
    "print(json.dumps({'flows': {k: [s for seg in (f.get('segments') or [])"
    " for s in (seg.get('steps') or [])] for k, f in FLOWS.items()},"
    " 'segments': {k: [list(seg.get('steps') or []) for seg in (f.get('segments') or [])]"
    " for k, f in FLOWS.items()},"
    " 'aliases': dict(getattr(FlowRunner, '_PLAIN_STEP_TOKENS', {}))}))\n"
)


def dump_flows(root: str) -> Optional[Dict[str, Any]]:
    """{'flows': {flow_id: [every step]}, 'aliases': {plain step: @token}} as the
    code in `root` defines them (built-ins only: DB edits are not code). None when
    the module does not import."""
    try:
        out = _py(root, _DUMP)
    except subprocess.TimeoutExpired:
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def flows_load(code_root: str, tools: str) -> Dict[str, Any]:
    """check-flows from the trusted tools script (`tools`, the main checkout's) over
    the code in `code_root` (the session's worktree)."""
    env = dict(os.environ, PYTHONPATH=code_root, AGENT_CODE_ROOT=code_root)
    try:
        out = subprocess.run([_PY, tools, "check-flows"], cwd=code_root, capture_output=True,
                             text=True, timeout=180, env=env)
    except subprocess.TimeoutExpired:
        return gate("flows load", False, "check-flows timed out")
    return gate("flows load", out.returncode == 0, (out.stdout or out.stderr).strip())


# ── value checks kept ────────────────────────────────────────────────────────

def checks_kept(before: Dict[str, List[str]], after: Optional[Dict[str, List[str]]]) -> Dict[str, Any]:
    if after is None:
        return gate("checks kept", False, "cross_app_flows.py does not import after the change")
    problems = []
    for fid, steps in before.items():
        if fid not in after:
            problems.append(f"flow {fid} was deleted")
            continue
        now = set(s.strip() for s in after[fid])
        for st in steps:
            if CHECK_STEP.search(st) and st.strip() not in now:
                problems.append(f"{fid}: the value check {st!r} was removed or changed")
    return gate("checks kept", not problems,
                "\n".join(problems) or "no flow lost a value check; no flow was deleted")


def flow_changes(before: Dict[str, List[str]], after: Dict[str, List[str]]) -> Tuple[List[str], List[str]]:
    """(flows whose steps changed, flows that are new)."""
    changed = [f for f in after if f in before and after[f] != before[f]]
    new = [f for f in after if f not in before]
    return changed, new


# ── unit tests ───────────────────────────────────────────────────────────────

_FAILED = re.compile(r"^(FAILED|ERROR) (\S+)", re.M)
_COUNTS = re.compile(r"(\d+) (passed|failed|errors?|skipped)")


# The whole suite in parallel processes (pytest-xdist): MEASURED 2026-10-28, 1,875
# tests in 2m01s with 4 workers against 5m45s in one, same 3 failures. Each worker
# imports tests/conftest.py and so gets its own temporary database. 4, not every
# core: a session's checks can run while the simulators are busy.
UNIT_WORKERS = 4


def _parallel_args(paths: Optional[List[str]]) -> List[str]:
    if paths or importlib.util.find_spec("xdist") is None:
        return []
    return ["-n", str(UNIT_WORKERS)]


def run_unit_tests(root: str, paths: Optional[List[str]] = None, timeout: int = 1500) -> Dict[str, Any]:
    """pytest in `root`. Returns {ok, failed: [nodeid], counts, tail}."""
    cmd = ([_PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfE"] + _parallel_args(paths)
           + (paths or ["tests"]))
    try:
        out = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout,
                             env=dict(os.environ, PYTHONPATH=root))
    except subprocess.TimeoutExpired:
        return {"ok": False, "failed": [], "counts": {}, "tail": f"pytest timed out after {timeout}s",
                "timed_out": True}
    text = out.stdout + out.stderr
    failed = sorted({m.group(2).split(" ")[0] for m in _FAILED.finditer(text)})
    counts: Dict[str, int] = {}
    for n, k in _COUNTS.findall(text.splitlines()[-1] if text.strip() else ""):
        counts[k.rstrip("s") if k.startswith("error") else k] = int(n)
    return {"ok": out.returncode == 0, "failed": failed, "counts": counts,
            "tail": "\n".join(text.strip().splitlines()[-15:]), "timed_out": False,
            "returncode": out.returncode}


def unit_gate(after: Dict[str, Any], baseline: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if after.get("timed_out"):
        return gate("unit tests", False, after["tail"])
    if after.get("returncode") not in (0, 1):            # 2 = interrupted/collection error, …
        return gate("unit tests", False, "pytest could not run the suite:\n" + after.get("tail", ""))
    new = sorted(set(after["failed"]) - set((baseline or {}).get("failed") or []))
    c = after.get("counts") or {}
    head = f"{c.get('passed', 0)} passed, {len(after['failed'])} failed"
    if not new:
        known = len(after["failed"])
        return gate("unit tests", True, head + (f" (all {known} also fail on the base commit)" if known else ""))
    return gate("unit tests", False, head + "; NEW failures:\n" + "\n".join(new))


# ── on device ────────────────────────────────────────────────────────────────

def read_runs(session_dir: str) -> List[Dict[str, Any]]:
    rows = []
    try:
        with open(os.path.join(session_dir, "runs.jsonl")) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def confirm_runs(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The ledger's runs with status and start time taken from the database — the
    ledger says which runs the session made, the test_runs table says how they went."""
    import calendar
    from automation.database.config import SessionLocal
    from automation.database.models import TestRun
    out = []
    with SessionLocal() as db:
        for r in runs:
            row = db.get(TestRun, r.get("run_id") or "")
            if row is None:
                continue
            t0 = calendar.timegm((row.started_at or row.created_at).timetuple()) \
                if (row.started_at or row.created_at) else float(r.get("t0") or 0)
            out.append({**r, "status": row.status or "", "t0": t0})
    return out


def device_gate(runs: List[Dict[str, Any]], last_edit: float, must_pass: Iterable[str],
                any_of: Optional[List[str]],
                current: Optional[Callable[[Dict[str, Any]], Optional[bool]]] = None) -> Dict[str, Any]:
    """`must_pass`: each needs a passing run of the code it has now.
    `any_of`: at least one of these must have one ([impact.ALL] = any flow).

    `current(run)` says whether a run executed the code its flow has now (from the
    run's fingerprint, see fingerprint.py); a run it cannot judge (None) counts only
    when it started after `last_edit`, the last change to any file."""
    def counts(r: Dict[str, Any]) -> bool:
        if r.get("status") != "passed":
            return False
        verdict = current(r) if current is not None else None
        return verdict if verdict is not None else float(r.get("t0") or 0) >= last_edit

    passed_after = {r["flow_id"] for r in runs if counts(r)}
    problems = [f"{f}: no passing run after the last edit that reaches it" for f in sorted(set(must_pass))
                if f not in passed_after]
    if any_of:
        ok_any = bool(passed_after) if impact.ALL in any_of else bool(passed_after & set(any_of))
        if not ok_any:
            what = "any flow" if impact.ALL in any_of else "one of " + ", ".join(any_of[:12])
            problems.append(f"the engine change was not run on the simulators: {what} must pass "
                            f"after the last edit")
    detail = ("passed after the last edit: " + (", ".join(sorted(passed_after)) or "nothing"))
    return gate("on device", not problems, "\n".join(problems + [detail]))


# What one new scenario adds (its flow, handler and unit test): MEASURED over the
# approved 'New scenarios' sessions 2026-10-26..28, 150-370 changed lines each. A
# session makes one per ticked feature, so the limit grows with the ones it keeps.
# MEASURED 2026-10-28: session 60f14a6c kept 3, passed every other check, and its
# 961 lines against a flat 800 left it unapprovable.
LINES_PER_NEW_FLOW = 400


def size_gate(numstat: List[Dict[str, Any]], limit: int, new_flows: int = 0) -> Dict[str, Any]:
    total = sum(int(f.get("added") or 0) + int(f.get("removed") or 0) for f in numstat)
    new_flows = max(0, int(new_flows or 0))
    allowed = limit + LINES_PER_NEW_FLOW * new_flows
    how = (f"limit {limit} + {new_flows} new scenario{'' if new_flows == 1 else 's'} × "
           f"{LINES_PER_NEW_FLOW} = {allowed}") if new_flows else f"limit {limit}"
    return gate("size", total <= allowed, f"{total} changed lines ({how})")
