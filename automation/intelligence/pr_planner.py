"""PR-aware test planner.

Given a pull request, work out WHAT to test and HOW to get there:
  1. Read the PR — title, description, and the changed files/diff.
  2. Ask the local model which feature area the change touches (payment, login…).
  3. Pick the saved scenarios that exercise that area end-to-end (the recorded
     full paths: login → … → checkout → payment), in the order they should run.
  4. Explain the reasoning and flag any area the scenario library does not cover.

The "how to reach there" is encoded in the saved scenarios themselves — this
module's job is to choose the right full-path scenarios for the PR's change.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List

import httpx

logger = logging.getLogger("pr_planner")


def _changed_files(diff: str) -> List[str]:
    """File paths from a unified diff."""
    return sorted(set(re.findall(r"^\+\+\+ b/(.+)$", diff or "", re.M)))


def _ollama_json(system: str, user: str) -> Dict[str, Any]:
    """PR-planning JSON via the configured provider (Groq by default) — same fast,
    free path the rest of the platform uses, instead of a local Ollama."""
    from automation.ai.provider import create_provider, default_config
    try:
        resp = create_provider(default_config).generate(
            system, user, json_schema={}, max_tokens=1536, temperature=0.2)
        raw = resp.content or "{}"
        m = raw[raw.find("{"): raw.rfind("}") + 1] if "{" in raw else raw
        return json.loads(m)
    except Exception:
        return {}


_SYSTEM = (
    "You are a QA lead deciding which end-to-end tests to run for a pull request. "
    "You are given the PR title, description, the list of changed files, and a "
    "catalog of available saved test scenarios (each is a recorded full user flow). "
    "Decide which feature area the PR affects and which scenarios must run to verify "
    "the fix — ALWAYS choosing scenarios that reach the feature through the full path "
    "(e.g. to verify payment, run a scenario that logs in, builds a cart, checks out "
    "and pays — not an isolated payment step). Only choose from the given scenarios. "
    "Reply ONLY as JSON with this shape: {\"affected_areas\": [string], "
    "\"summary\": string, \"selected_scenarios\": [{\"id\": string, \"name\": string, "
    "\"reason\": string}], \"path_explanation\": string, \"missing_coverage\": string}. "
    "If no saved scenario covers the affected area, leave selected_scenarios empty and "
    "say what scenario should be recorded in missing_coverage."
)


def _role_of(name: str, steps: List[str]) -> str:
    """Classify a scenario into the cross-app role / app it runs on:
    'consumer' (the diner app) vs the Business iPad app, split into 'kitchen' (marks orders
    ready) and 'waiter' (books/assigns/serves/pays). NOTE: the restaurant is literally named
    'NylaiKitchen2', so a bare 'kitchen' match would mis-tag every consumer scenario — match
    the kitchen FLOW (mark-ready ids / 'kitchen mark' in the name) and Business-only ids."""
    low_name = name.lower()
    step_text = " ".join(steps or [])
    KITCHEN_IDS = ("inProgressOrderCard", "orderReadyBtn", "orderCloseBtn", "completedOrderCard")
    WAITER_IDS = ("AssignTableBtn", "T0AssignAnyBtn", "sendItemsBtn", "serveItemsBtn",
                  "closeTableBtn", "notifyPaymentBtn", "modifyTable", "addItemsBtn",
                  "signInBtn", "addNewEvent")
    is_kitchen_flow = ("kitchen mark" in low_name or "kitchen:" in low_name
                       or any(k in step_text for k in KITCHEN_IDS))
    is_business = (low_name.startswith("business:") or "waiter" in low_name
                   or is_kitchen_flow or any(k in step_text for k in WAITER_IDS))
    if not is_business:
        return "consumer"
    return "kitchen" if is_kitchen_flow else "waiter"


def plan_pr_tests(gh, owner: str, repo: str, pr_number: int,
                  scenarios: List[Dict[str, Any]], project_id: str = None,
                  ticket: Dict[str, Any] = None) -> Dict[str, Any]:
    """Build a test plan for a PR. `gh` is a GitHubIntegration; `scenarios` is a
    list of {id, name, description, steps} for the project."""
    meta = gh.fetch_pr_metadata(owner, repo, pr_number)
    title = meta.get("title") or ""
    body = (meta.get("body") or "")[:3000]
    sha = meta.get("commit_sha") or ""
    diff = gh.fetch_commit_diff(owner, repo, sha) if sha else ""
    files = _changed_files(diff)

    catalog = "\n".join(
        f"- id={s['id']} name=\"{s['name']}\" desc=\"{s.get('description') or ''}\" "
        f"steps=[{'; '.join((s.get('steps') or [])[:8])}]"
        for s in scenarios
    ) or "(no saved scenarios yet)"

    # The linked ticket is the clearest statement of INTENT available — a human wrote
    # what the change is meant to achieve. Without it the planner had to infer intent
    # from a title, an often-empty PR body and a file diff, while the ticket text sat
    # unused on the PR page. Acceptance criteria decide what is worth verifying.
    ticket_block = ""
    if ticket and (ticket.get("description") or ticket.get("title")):
        ticket_block = (
            f"Linked ticket {ticket.get('key') or ''}: {ticket.get('title') or ''}\n"
            f"{(ticket.get('description') or '')[:2000]}\n\n"
            "Treat the ticket as the intended behaviour: prefer scenarios that verify it.\n\n"
        )

    user = (
        f"PR #{pr_number}: {title}\n\n"
        f"{ticket_block}"
        f"Description:\n{body or '(none)'}\n\n"
        f"Changed files ({len(files)}):\n" + "\n".join(files[:40]) + "\n\n"
        f"Available saved scenarios:\n{catalog}\n\n"
        "Produce the JSON test plan."
    )

    try:
        plan = _ollama_json(_SYSTEM, user)
    except Exception as e:
        raise RuntimeError(f"Could not reach the local model ({e}). Start Ollama "
                           f"('ollama serve') and pull a model.")

    # Keep only AI selections that reference real scenarios.
    by_id = {s["id"]: s for s in scenarios}
    ai_selected = []
    for sel in (plan.get("selected_scenarios") or []):
        sc = by_id.get(sel.get("id"))
        if sc:
            ai_selected.append({"id": sc["id"], "name": sc["name"],
                                "reason": sel.get("reason", ""), "steps": sc.get("steps", [])})

    # FORMAL graph-driven selection: affected files/modules from the dependency
    # graph, then scenarios whose coverage touches them. This is deterministic; the
    # AI is used for the narrative. Prefer the formal selection when we have a graph.
    formal: Dict[str, Any] = {}
    if project_id:
        try:
            from automation.intelligence.impact_selection import plan_impact
            formal = plan_impact(project_id, files, scenarios)
        except Exception as e:
            logger.warning("formal impact selection failed: %s", e)

    graph_driven = bool(formal.get("affected_modules"))
    selected = formal.get("selected") if graph_driven else ai_selected
    if not selected:
        selected = ai_selected

    # Cross-app verification is three DISTINCT roles — consumer, waiter, kitchen. Tag each
    # selected path with its role and group them, so the plan reads as three separate paths
    # (the app under test switches per role) instead of one lumped list.
    for s in selected:
        s["role"] = _role_of(s.get("name", ""), s.get("steps", []))
    by_role: Dict[str, List[Dict[str, Any]]] = {"consumer": [], "waiter": [], "kitchen": []}
    for s in selected:
        by_role[s["role"]].append(s)

    return {
        "pr_number": pr_number, "title": title, "commit_sha": sha,
        "changed_files": files,
        "affected_areas": plan.get("affected_areas") or [],
        "affected_modules": formal.get("affected_modules") or [],
        "affected_files": formal.get("affected_files") or [],
        "graph_driven": graph_driven,
        "summary": plan.get("summary") or "",
        "path_explanation": plan.get("path_explanation") or "",
        "missing_coverage": plan.get("missing_coverage") or "",
        "selected_scenarios": selected,
        "by_role": by_role,   # cross-app verification split into consumer / waiter / kitchen
        "skipped_scenarios": formal.get("skipped") or [],
        "reduction_pct": formal.get("reduction_pct", 0.0),
        "untagged_count": formal.get("untagged_count", 0),
    }
