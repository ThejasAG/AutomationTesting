"""Analyze the application: what it does, what the tests cover, what is missing —
then write scenarios for the gaps.

Two stages:
  1. inventory.py (free): every screen of both apps from the navigators, its
     element ids, and which of them the automation already drives.
  2. Claude: reads that map, the existing flows, the scenario library and the
     source of the uncovered screens; groups screens into features; rates each
     feature covered / partial / missing; and writes runnable flows for the most
     important gaps.

Proposals are stored as `scenario_proposal` actions. Nothing runs until a person
approves one on the AI Agent page; approval saves it as a new cross-app flow
(id `agent_<slug>`, a CrossAppFlowEdit row), and the nightly batch runs it from
then on. "Approve & test" also runs it once right away.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

from automation.agentic import claude, inventory, sources, triage
from automation.agentic.fixer import token_ok

logger = logging.getLogger("agentic.generator")

_LIB = "vya_scenario_library.md"
_CHUNK = 12000
MAX_SCENARIOS = 8


def _library(inp: Dict[str, Any]) -> str:
    text = sources.knowledge(_LIB)
    parts = max(1, -(-len(text) // _CHUNK))
    n = min(max(1, int(inp.get("part") or 1)), parts)
    return f"[part {n} of {parts}]\n" + text[(n - 1) * _CHUNK:n * _CHUNK]


def _flows_outline() -> str:
    from automation.scenarios.cross_app_flows import list_flows
    out = []
    for f in list_flows():
        segs = " → ".join(f"[{s['role']}] {s['name']}" for s in f["segments"])
        out.append(f"- {f['id']}: {f['name']} :: {segs}")
    return "\n".join(out)


def _screen(inv: Dict[str, Any]):
    def get(inp: Dict[str, Any]) -> str:
        app, name = inp["app"], inp["route"].strip().lower()
        hits = [r for r in inv["apps"].get(app, {}).get("routes", []) if r["route"].lower() == name]
        if not hits:
            raise ValueError(f"no route '{inp['route']}' in the {app} app")
        return json.dumps(hits, indent=1)
    return get


SYSTEM_EXTRA = f"""
You are now acting as a test architect analysing the whole application, not a
failure analyst.

You are given (in the first message) a computed map of every screen in both apps,
the element ids on each, and which screens the current automation already drives,
plus the existing flows. Your job:

1. Understand the product: what each role can do, screen by screen. Read the source
   of screens you need to understand (read_app_file / search_app_source); the
   scenario library describes intended behaviour.
2. Group the screens into user-facing FEATURES (e.g. "Table booking with
   pre-order", "Edit profile", "Inventory: add item", "Refund request"). For each,
   rate coverage: covered (an existing flow exercises it end to end), partial
   (touched but key paths unchecked), or missing. Name the flows that cover it and
   the business risk if it broke (high / medium / low).
3. Write up to {MAX_SCENARIOS} NEW flows for the highest-risk missing/partial
   features. Do not duplicate an existing flow or another proposal.

Rules for a flow:
- Roles: consumer / waiter / kitchen; one role per segment.
- Every step must be executable: an @handler from the list, or a plain step naming
  a REAL id or exact visible text you saw in the source or the map — "click
  SettingsBtn", "type 4 into guestCount", "select Not Sure". Never invent ids.
- Reuse how existing flows log in, reach home and book (they start with
  @consumer_home / @first_time_slot etc.). Booking only opens within 30 minutes of
  the slot. A flow that needs an order in the kitchen needs a waiter segment that
  sends one first.
- Prefer flows that end by checking a visible result (a status, a total, a screen)
  so a broken feature actually fails the test.

Finish by calling submit_analysis with app_summary, features and scenarios.
"""


def _tools() -> List[Dict[str, Any]]:
    s, st = claude.strict_tool, {"type": "string"}
    app = {"type": "string", "enum": ["consumer", "business"]}
    seg = {"type": "object", "additionalProperties": False, "required": ["name", "role", "steps"],
           "properties": {"name": st,
                          "role": {"type": "string", "enum": ["consumer", "waiter", "kitchen"]},
                          "steps": {"type": "array", "items": st}}}
    flow = {"type": "object", "additionalProperties": False,
            "required": ["name", "description", "rationale", "feature", "segments"],
            "properties": {"name": st, "description": st, "rationale": st, "feature": st,
                           "segments": {"type": "array", "items": seg}}}
    feature = {"type": "object", "additionalProperties": False,
               "required": ["name", "app", "screens", "coverage", "covered_by", "risk", "notes"],
               "properties": {
                   "name": st,
                   "app": {"type": "string", "enum": ["consumer", "business", "both"]},
                   "screens": {"type": "array", "items": st},
                   "coverage": {"type": "string", "enum": ["covered", "partial", "missing"]},
                   "covered_by": {"type": "array", "items": st},
                   "risk": {"type": "string", "enum": ["high", "medium", "low"]},
                   "notes": st}}
    return [
        s("get_screen", "One screen from the map: file, ids, which ids tests use.",
          {"app": app, "route": st}),
        s("get_flow", "One existing flow's segments and steps.", {"flow_id": st}),
        s("read_scenario_library", "The product's scenario library, in parts (1, 2, …).",
          {"part": {"type": "integer"}}),
        s("search_app_source", "grep -E over the app's JS/TS source under App/.",
          {"app": app, "pattern": st}),
        s("read_app_file", "Read lines of a file in the app checkout (max 400 lines).",
          {"app": app, "path": st, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}),
        s("submit_analysis", "Your analysis and new flows. Call once, at the end.",
          {"app_summary": st, "features": {"type": "array", "items": feature},
           "scenarios": {"type": "array", "items": flow}}),
    ]


def validate(flow: Dict[str, Any]) -> List[str]:
    problems = []
    if not flow.get("segments"):
        problems.append("no segments")
    for i, seg in enumerate(flow.get("segments") or [], 1):
        steps = [x for x in seg.get("steps") or [] if x.strip()]
        if not steps:
            problems.append(f"segment {i} has no steps")
        problems += [f"segment {i}: unknown handler {x}" for x in steps if not token_ok(x)]
    return problems


def slug(name: str) -> str:
    return "agent_" + (re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:50] or "flow")


def analyze(*, model: str, budget_left_usd: float, focus: str = "",
            inv: Dict[str, Any] | None = None,
            pending: List[str] | None = None) -> claude.LoopResult:
    inv = inv or inventory.build()
    handlers = {
        "get_screen": _screen(inv),
        "get_flow": triage._get_flow,
        "read_scenario_library": _library,
        "search_app_source": triage._HANDLERS["search_app_source"],
        "read_app_file": triage._HANDLERS["read_app_file"],
    }
    user = (
        "Analyze the Vyapy application and write scenarios for what is not tested."
        + (f"\nFocus especially on: {focus}" if focus else "")
        + "\n\n# Existing flows\n" + _flows_outline()
        + ("\n\n# Already proposed (do not repeat)\n" + "\n".join(f"- {p}" for p in pending) if pending else "")
        + "\n\n# Screen map (computed from the source)" + inventory.compact(inv)
    )
    return claude.run_loop(system=triage.system_prompt() + SYSTEM_EXTRA, user_content=user,
                           tools=_tools(), handlers=handlers, submit_tool="submit_analysis",
                           model=model, budget_left_usd=budget_left_usd, effort="high",
                           max_turns=45)
