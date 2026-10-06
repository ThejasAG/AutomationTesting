"""Claude diagnoses one failed cross-app flow run.

It reads the run (segments, notes, the failure screenshot), the flow's steps and
history, and the app source, then submits a structured verdict. It may propose a
test fix (a step replacement) or an app patch; it never applies anything itself —
fixer.py decides what is safe to apply.
"""
from __future__ import annotations

import base64
import logging
import os
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional

from automation.agentic import claude, sources
from automation.database.config import SessionLocal
from automation.database.models import ScenarioResult, TestRun

logger = logging.getLogger("agentic.triage")

CATEGORIES = ["APP_BUG", "TEST_ISSUE", "LOCATOR", "TIMING", "FLAKY", "INFRA", "UNKNOWN"]
ACTIONS = ["retry", "fix_test", "report_app_bug", "report_infra", "needs_human"]


@lru_cache(maxsize=1)
def system_prompt() -> str:
    """Stable across calls (no timestamps or ids) so the prefix is cached."""
    from automation.scenarios.cross_app_flows import STEP_CATALOG
    agents_md = ""
    try:
        with open(os.path.join(sources.ROOT, "AGENTS.md"), encoding="utf-8") as f:
            text = f.read()
        m = re.search(r"## 5\..*?(?=\n## 6\.)", text, re.S)
        agents_md = m.group(0) if m else ""
    except OSError:
        pass
    catalog = "\n".join(f"- {s['step']}: {s.get('help', '')}"
                        for group in STEP_CATALOG.values() for s in group)
    return f"""You diagnose failed end-to-end test runs for the Vyapy restaurant apps.

Three roles take part in a cross-app flow: the CONSUMER (diner) app on an iPhone
simulator, and the WAITER and KITCHEN roles of the BUSINESS app on an iPad
simulator. A flow is a list of segments; each segment runs one role's steps. Steps
are either plain language resolved against the live screen ("click bookAppoitment")
or @handlers with special logic.

Your job for each failed run: find the real cause and say what should happen next.
Decide between:
- APP_BUG: the app misbehaved (crash, wrong total, button that does nothing,
  screen that never loads while the rig is healthy). Report it; propose an app
  patch only when the source shows the defect clearly.
- LOCATOR / TEST_ISSUE: the test is wrong (an id changed, a step is missing, a
  dialog needs dismissing first). Propose a test fix.
- TIMING / FLAKY: the app was slow or the step raced the UI; a retry is the fix.
- INFRA: the simulator, Appium/WebDriverAgent, Metro, or the staging server failed.

Rules you must follow:
- Base every claim on evidence you looked at: the run notes, the screenshot, the
  app source. Quote ids exactly as they appear in the source. Never invent an id.
- A test fix replaces ONE existing step of the failed segment with one or more new
  steps. Use real ids from the source or the locator map, or the handlers listed
  below. Never remove or weaken a step that checks a value (a total, VAT, a status)
  — if such a check fails, that is an APP_BUG or needs a human, not a test fix.
- An app patch is a unified diff against the files as they are in the checkout
  (paths relative to the repo root, e.g. App/Screens/...). Keep it minimal. Leave
  it empty unless you have read the code you are changing.
- If the evidence is not enough to decide, say so: category UNKNOWN, action
  needs_human, confidence low.
- Finish by calling submit_diagnosis. Use empty strings / empty lists for the
  test_fix and app_fix fields you are not proposing.

Step handlers the runner understands:
{catalog}

Platform knowledge (hard-won gotchas):
{agents_md}

Real element ids per screen (extracted from the app source):
{sources.knowledge("locator_map.md")}
"""


def _rows(run_id: str) -> List[ScenarioResult]:
    with SessionLocal() as db:
        rows = (db.query(ScenarioResult).filter(ScenarioResult.run_id == run_id)
                .order_by(ScenarioResult.created_at).all())
        for r in rows:
            db.expunge(r)
    return rows


def _get_run(inp: Dict[str, Any]) -> str:
    run_id = inp["run_id"]
    with SessionLocal() as db:
        run = db.get(TestRun, run_id)
        if not run:
            raise ValueError(f"no run {run_id}")
        head = (f"Run {run.id}\nflow: {run.test_name}\nstatus: {run.status}\n"
                f"devices: {run.device_name}\nstarted: {run.started_at}  ended: {run.completed_at}\n"
                f"crash_detected: {run.crash_detected}\nerror: {run.error_message or '-'}\n")
    out = [head]
    for r in _rows(run_id):
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


def _get_screenshot(inp: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = [r for r in _rows(inp["run_id"]) if r.screenshot]
    want = (inp.get("segment_num") or "").strip()
    pick = next((r for r in rows if want and r.scenario_num == want), None) or \
        next((r for r in rows if (r.status or "").upper() == "FAIL"), None)
    if not pick:
        return [{"type": "text", "text": "No screenshot was captured for this run."}]
    m = re.match(r"data:(image/[a-z]+);base64,(.*)", pick.screenshot, re.S)
    if not m:
        return [{"type": "text", "text": "Screenshot is stored in an unknown format."}]
    data = m.group(2)
    base64.b64decode(data[:100] + "=" * (-len(data[:100]) % 4))  # sanity: is base64
    return [
        {"type": "text", "text": f"Screen when segment {pick.scenario_num} ({pick.scenario_name}) "
                                 f"ended, status {pick.status}:"},
        {"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": data}},
    ]


def _get_flow(inp: Dict[str, Any]) -> str:
    from automation.scenarios.cross_app_flows import list_flows
    flow = next((f for f in list_flows() if f["id"] == inp["flow_id"]), None)
    if not flow:
        raise ValueError(f"no flow {inp['flow_id']}")
    lines = [f"{flow['id']}: {flow['name']}{' (edited)' if flow.get('edited') else ''}",
             flow.get("description") or ""]
    for s in flow["segments"]:
        lines.append(f"\nSegment {s['num']} [{s['role']}] {s['name']}")
        lines.extend(f"  {i}. {st}" for i, st in enumerate(s["steps"], 1))
    return "\n".join(lines)


def _flow_history(inp: Dict[str, Any]) -> str:
    from automation.scenarios.cross_app_flows import list_flows
    flow = next((f for f in list_flows() if f["id"] == inp["flow_id"]), None)
    if not flow:
        raise ValueError(f"no flow {inp['flow_id']}")
    with SessionLocal() as db:
        runs = (db.query(TestRun).filter(TestRun.bot_type == "ios-crossapp-flow",
                                         TestRun.test_name.like(f"%{flow['name']}"))
                .order_by(TestRun.created_at.desc()).limit(12).all())
        out = []
        for r in runs:
            fail = (db.query(ScenarioResult).filter(ScenarioResult.run_id == r.id,
                                                    ScenarioResult.status == "FAIL").first())
            where = (fail.error or "")[:160] if fail else ""
            out.append(f"{r.created_at:%Y-%m-%d %H:%M} {r.status:8} {r.id[:8]} {where}")
    return "\n".join(out) or "No previous runs of this flow."


def _tools() -> List[Dict[str, Any]]:
    s, st = claude.strict_tool, {"type": "string"}
    app = {"type": "string", "enum": ["consumer", "business"]}
    return [
        s("get_run", "The run's segments with status, error and every runner note "
          "(what each step did, what was on screen).", {"run_id": st}),
        s("get_failure_screenshot", "Screenshot taken when a segment ended. segment_num "
          "may be empty for the first failed segment.", {"run_id": st, "segment_num": st}),
        s("get_flow", "The flow's segments and steps as they run now.", {"flow_id": st}),
        s("flow_history", "This flow's last runs: status and where each failed. "
          "Shows whether a failure is new, recurring, or intermittent.", {"flow_id": st}),
        s("search_app_source", "grep -E over the app's JS/TS source under App/. Use it to "
          "find testIDs/accessibilityLabels and the code behind a screen.",
          {"app": app, "pattern": st}),
        s("read_app_file", "Read lines of a file in the app checkout (max 400 lines).",
          {"app": app, "path": st, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}),
        s("app_recent_changes", "Recent commits on the app checkout, with changed files.",
          {"app": app}),
        s("submit_diagnosis", "Your final verdict. Call exactly once, at the end.", {
            "category": {"type": "string", "enum": CATEGORIES},
            "root_cause": st,
            "evidence": {"type": "array", "items": st},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "recommended_action": {"type": "string", "enum": ACTIONS},
            "test_fix": {
                "type": "object", "additionalProperties": False,
                "required": ["segment_num", "old_step", "new_steps", "reason"],
                "properties": {"segment_num": st, "old_step": st,
                               "new_steps": {"type": "array", "items": st}, "reason": st}},
            "app_fix": {
                "type": "object", "additionalProperties": False,
                "required": ["app", "patch", "explanation"],
                "properties": {"app": {"type": "string", "enum": ["none", "consumer", "business"]},
                               "patch": st, "explanation": st}},
        }),
    ]


_HANDLERS = {
    "get_run": _get_run,
    "get_failure_screenshot": _get_screenshot,
    "get_flow": _get_flow,
    "flow_history": _flow_history,
    "search_app_source": lambda i: sources.search(i["app"], i["pattern"]),
    "read_app_file": lambda i: sources.read_file(i["app"], i["path"], i.get("start_line", 1),
                                                 i.get("end_line", 200)),
    "app_recent_changes": lambda i: sources.recent_changes(i["app"]),
}


def diagnose(run_id: str, flow_id: str, rule_verdict: Optional[Dict[str, Any]], *,
             model: str, budget_left_usd: float) -> claude.LoopResult:
    hint = ""
    if rule_verdict:
        hint = (f"\nThe rule-based sorter labelled it {rule_verdict.get('category')} "
                f"(matched: {rule_verdict.get('matched') or 'nothing'}); confirm or correct that.")
    user = (f"Run {run_id} of flow {flow_id} failed.{hint}\n"
            f"Start with get_run and get_failure_screenshot, then investigate as needed.")
    return claude.run_loop(system=system_prompt(), user_content=user, tools=_tools(),
                           handlers=_HANDLERS, submit_tool="submit_diagnosis", model=model,
                           budget_left_usd=budget_left_usd, effort="high")
