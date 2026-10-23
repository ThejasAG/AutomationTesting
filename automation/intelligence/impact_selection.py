"""Graph-driven smart test selection.

Turns a PR's changed files into the deterministic set of AFFECTED files/modules
(via the dependency graph — HybridImpactAnalyzer over Graphify), then selects only
the scenarios whose declared coverage touches those modules. This is the proposal's
core promise: run only the tests that a change can actually affect, and report the
reduction.

A scenario with no coverage tags can't be judged, so it is run to be safe (and
flagged), which keeps selection sound while the library gets tagged.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger("impact_selection")


def compute_affected(project_id: str, changed_files: List[str]) -> Dict[str, Any]:
    """Deterministic affected files + modules for a change, from the dependency graph."""
    from automation.intelligence.hybrid_impact_analyzer import HybridImpactAnalyzer
    from automation.projects.repository import repository_manager

    repo = repository_manager.get_repo_path(project_id)
    files: List[str] = []
    nodes = 0
    if repo and changed_files:
        try:
            files, nodes = HybridImpactAnalyzer().intra_app_blast_radius(repo, changed_files)
        except Exception as e:
            logger.warning("blast radius failed: %s", e)

    rel = [os.path.relpath(f, repo) if (repo and os.path.isabs(f)) else f for f in files]
    modules = set()
    for f in rel:
        parts = f.replace("\\", "/").split("/")
        modules.add(os.path.splitext(parts[-1])[0])   # file stem (e.g. Store)
        if len(parts) >= 2:
            modules.add(parts[-2])                     # parent dir (e.g. StoreView)
    return {"affected_files": rel, "affected_modules": sorted(modules), "graph_nodes": nodes}


def select_scenarios(affected_files: List[str], affected_modules: List[str],
                     scenarios: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pick scenarios whose coverage intersects the affected files/modules."""
    aff = [f.lower() for f in affected_files]
    mods = {m.lower() for m in affected_modules}
    selected, skipped, untagged = [], [], []

    for sc in scenarios:
        covers = [c.lower() for c in (sc.get("covers") or []) if c.strip()]
        if not covers:
            untagged.append(sc)
            continue
        hit_on = [c for c in covers
                  if c in mods or any(c in f for f in aff)]
        entry = {"id": sc.get("id"), "name": sc.get("name"), "steps": sc.get("steps", []),
                 "covers": sc.get("covers", [])}
        if hit_on:
            entry["reason"] = f"covers {', '.join(hit_on)} — affected by this change"
            selected.append(entry)
        else:
            entry["reason"] = "coverage not affected by this change"
            skipped.append(entry)

    total = len(scenarios)
    # Untagged scenarios run to stay safe, but are flagged so they can be tagged.
    to_run = selected + [{"id": s.get("id"), "name": s.get("name"), "steps": s.get("steps", []),
                          "covers": [], "reason": "no coverage tags — run to be safe (tag it to enable skipping)"}
                         for s in untagged]
    reduction = round(100 * len(skipped) / total, 1) if total else 0.0
    return {
        "selected": to_run, "skipped": skipped,
        "untagged_count": len(untagged),
        "total": total, "run_count": len(to_run),
        "reduction_pct": reduction,
    }


def plan_impact(project_id: str, changed_files: List[str],
                scenarios: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Full formal plan: affected modules (graph) + selected scenarios + reduction."""
    aff = compute_affected(project_id, changed_files)
    sel = select_scenarios(aff["affected_files"], aff["affected_modules"], scenarios)
    return {**aff, **sel}
