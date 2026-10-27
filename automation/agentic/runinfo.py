"""Plain readers over runs and flows — what the Claude Code agent's tools print.

No AI here. Kept from the retired API-key agent (triage.py / fixer.py /
orchestrator.py) because the agent tools (scripts/agent_tools.py) and the session
gates (automation/claude_code/gates.py) need exactly these answers:

  run_text(run_id)       a failed run as text: segments, errors, notes
  flow_text(flow_id)     a flow's segments and steps as they run now
  flow_history(flow_id)  its last runs and where each failed
  token_ok(step)         is this @token dispatched by FlowRunner._handle_special?
  wait_for(run_id)       block until a run ends (status)
  load(run_id)           (TestRun, [ScenarioResult]) detached from the session
"""
from __future__ import annotations

import inspect
import re
import time
from functools import lru_cache
from typing import Any, Dict, List, Optional, Set, Tuple

from automation.database.config import SessionLocal
from automation.database.models import ScenarioResult, TestRun

RUN_TIMEOUT_S = 60 * 60          # a single flow attempt
POLL_S = 5
TERMINAL = ("passed", "failed", "error", "stopped", "cancelled", "canceled")

# A step that checks a value is the test's purpose; removing or editing it to get a
# pass would hide the bug it exists to catch. Used by the session gates.
CHECK_STEP = re.compile(r"verify|assert|expect|check|total|vat|amount|€|bill|change\b|^\s*@see:", re.I)


# ── runs ─────────────────────────────────────────────────────────────────────

def load(run_id: str) -> Tuple[Any, List[Any]]:
    with SessionLocal() as db:
        run = db.get(TestRun, run_id)
        rows = (db.query(ScenarioResult).filter(ScenarioResult.run_id == run_id)
                .order_by(ScenarioResult.created_at).all())
        if run is not None:
            db.expunge(run)
        for r in rows:
            db.expunge(r)
    return run, rows


def status_of(run_id: str) -> str:
    with SessionLocal() as db:
        r = db.get(TestRun, run_id)
        return (r.status or "") if r else "missing"


def wait_for(run_id: str, timeout_s: float = RUN_TIMEOUT_S) -> str:
    """Wait for a run to end. A run that outlives `timeout_s` is asked to stop and
    marked failed, so a wedged flow can never hold the rig forever."""
    from automation.scenarios.cross_app_flows import request_flow_stop
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = status_of(run_id)
        if st not in ("running", "queued", ""):
            return st
        time.sleep(POLL_S)
    request_flow_stop(run_id)
    with SessionLocal() as db:
        r = db.get(TestRun, run_id)
        if r is not None:
            r.status = "failed"
            r.error_message = "Agent: the run exceeded 60 minutes and was stopped."
            db.commit()
    return "failed"


def run_text(run_id: str) -> str:
    run, rows = load(run_id)
    if not run:
        raise ValueError(f"no run {run_id}")
    out = [f"Run {run.id}\nflow: {run.test_name}\nstatus: {run.status}\n"
           f"devices: {run.device_name}\nstarted: {run.started_at}  ended: {run.completed_at}\n"
           f"crash_detected: {run.crash_detected}\nerror: {run.error_message or '-'}\n"]
    for r in rows:
        out.append(f"\n## Segment {r.scenario_num}: {r.scenario_name} — {r.status} "
                   f"(consumer {r.consumer_status}, business {r.business_status})")
        if r.error:
            out.append(f"error: {r.error}")
        notes = [str(n) for n in (r.reasons or [])]
        if len(notes) > 60:
            notes = notes[:10] + [f"… {len(notes) - 50} notes omitted …"] + notes[-40:]
        out.extend("  " + n[:400] for n in notes)
        out.append(f"  (screenshot: {'yes' if r.screenshot else 'no'})")
    return "\n".join(out)


# ── flows ────────────────────────────────────────────────────────────────────

def _flow(flow_id: str) -> Dict[str, Any]:
    from automation.scenarios.cross_app_flows import list_flows
    flow = next((f for f in list_flows() if f["id"] == flow_id), None)
    if not flow:
        raise ValueError(f"no flow {flow_id}")
    return flow


def flow_text(flow_id: str) -> str:
    flow = _flow(flow_id)
    lines = [f"{flow['id']}: {flow['name']}{' (edited)' if flow.get('edited') else ''}",
             flow.get("description") or ""]
    for s in flow["segments"]:
        lines.append(f"\nSegment {s['num']} [{s['role']}] {s['name']}")
        lines.extend(f"  {i}. {st}" for i, st in enumerate(s["steps"], 1))
    return "\n".join(lines)


def flow_history(flow_id: str, limit: int = 12) -> str:
    flow = _flow(flow_id)
    with SessionLocal() as db:
        runs = (db.query(TestRun).filter(TestRun.bot_type == "ios-crossapp-flow",
                                         TestRun.test_name.like(f"%{flow['name']}"))
                .order_by(TestRun.created_at.desc()).limit(limit).all())
        out = []
        for r in runs:
            fail = (db.query(ScenarioResult).filter(ScenarioResult.run_id == r.id,
                                                    ScenarioResult.status == "FAIL").first())
            where = (fail.error or "")[:160] if fail else ""
            out.append(f"{r.created_at:%Y-%m-%d %H:%M} {r.status:8} {r.id[:8]} {where}")
    return "\n".join(out) or "No previous runs of this flow."


def flow_id_for_run(run: Any) -> Optional[str]:
    """Which flow a run row belongs to: its test_name is '[Env] <flow name>'."""
    from automation.scenarios.cross_app_flows import list_flows
    name = re.sub(r"^\[[^\]]*\]\s*", "", getattr(run, "test_name", "") or "").strip()
    f = next((f for f in list_flows() if f["name"] == name), None)
    return f["id"] if f else None


# ── step tokens ──────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def known_tokens() -> Tuple[Set[str], Set[str]]:
    """(@tokens, @prefixes:) the runner dispatches — read from its own source so a
    new handler is accepted the day it is added. Only what _handle_special names
    counts: tokens are NOT trusted because a flow uses them, or a draft flow's
    invented @token would vouch for itself (measured 2026-10-22: every token the
    shipped flows use is named there)."""
    from automation.scenarios import cross_app_flows as caf
    src = inspect.getsource(caf.FlowRunner._handle_special)
    exact = set(re.findall(r'"(@[a-z_]+)"', src))
    prefixes = set(re.findall(r'"(@[a-z_]+:)"', src))
    return exact, prefixes


def token_ok(step: str) -> bool:
    step = step.strip()
    if not step.startswith("@"):
        return True
    exact, prefixes = known_tokens()
    return step in exact or any(step.startswith(p) for p in prefixes)


def inventory_summary(inv: Dict[str, Any]) -> Dict[str, Any]:
    """The coverage map, trimmed for the dashboard."""
    out: Dict[str, Any] = {}
    for app, a in inv["apps"].items():
        out[app] = {k: a[k] for k in ("screens", "screens_covered", "ids", "ids_covered")}
        out[app]["routes"] = [{"route": r["route"], "file": r["file"], "covered": r["covered"],
                               "ids": len(r["ids"]), "covered_ids": r["covered_ids"][:10]}
                              for r in a["routes"]]
    return out
