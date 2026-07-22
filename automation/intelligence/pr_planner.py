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
    base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    model = os.getenv("LLM_MODEL_NAME", "llama3.2")
    resp = httpx.post(
        f"{base}/api/generate",
        json={"model": model, "system": system, "prompt": user, "stream": False,
              "format": "json", "options": {"temperature": 0.2, "num_predict": 1536}},
        timeout=300,
    )
    resp.raise_for_status()
    try:
        return json.loads(resp.json().get("response", "{}"))
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


def plan_pr_tests(gh, owner: str, repo: str, pr_number: int,
                  scenarios: List[Dict[str, Any]]) -> Dict[str, Any]:
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

    user = (
        f"PR #{pr_number}: {title}\n\n"
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

    # Keep only selections that reference real scenarios; attach the steps so the
    # caller can run them without another lookup.
    by_id = {s["id"]: s for s in scenarios}
    selected = []
    for sel in (plan.get("selected_scenarios") or []):
        sc = by_id.get(sel.get("id"))
        if sc:
            selected.append({"id": sc["id"], "name": sc["name"],
                             "reason": sel.get("reason", ""), "steps": sc.get("steps", [])})

    return {
        "pr_number": pr_number, "title": title, "commit_sha": sha,
        "changed_files": files,
        "affected_areas": plan.get("affected_areas") or [],
        "summary": plan.get("summary") or "",
        "path_explanation": plan.get("path_explanation") or "",
        "missing_coverage": plan.get("missing_coverage") or "",
        "selected_scenarios": selected,
    }
