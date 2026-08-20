"""Workflow / coverage: the app's spine + the full test-suite coverage matrix.

Powers the Workflow view — a visual map of the whole application's flow, colored
by what's automated / pending / manual-blocked.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from automation.auth.security import get_current_user
from automation.database.config import get_db
from automation.database.models import SavedScenario
from automation.workflow.catalog import (
    SPINE, CONSUMER_FLOW, BUSINESS_FLOW, BRANCH_NODES, CROSS_APP_EDGES,
    WF_EDGES, CATALOG, summary, plan_path, all_nodes,
)

router = APIRouter(prefix="/workflow", tags=["workflow"])

# Which spine step each existing SavedScenario name satisfies (for "built" status).
_SPINE_SCENARIO = {
    "c_book":     "Book an event",
    "b_login":    "Business: Login as waiter",
    "b_assign":   "Business: Assign table + add item + send to kitchen",
    "b_add":      "Business: Assign table + add item + send to kitchen",
    "b_send":     "Business: Assign table + add item + send to kitchen",
    "k_login":    "Business: Kitchen mark ready",
    "k_ready":    "Business: Kitchen mark ready",
    "b_serve":    "Business: Serve + pay + close (waiter)",
    "b_notify":   "Business: Serve + pay + close (waiter)",
    "b_close":    "Business: Serve + pay + close (waiter)",
}


@router.get("/coverage")
def get_coverage(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """The full coverage matrix + which scenarios are actually built."""
    built = {s.name for s in db.query(SavedScenario).all()}
    categories = []
    for cat, items in CATALOG.items():
        rows = []
        for it in items:
            is_built = it["name"] in built
            rows.append({
                "name": it["name"],
                "status": it["status"],                       # auto | manual
                "built": is_built,
            })
        categories.append({
            "category": cat,
            "blocked": "BLOCKED" in cat,
            "scenarios": rows,
        })
    return {"summary": summary(), "categories": categories}


@router.get("/graph")
def get_graph(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """The cross-app spine: two lanes (Consumer + Business) + handoff edges."""
    built = {s.name for s in db.query(SavedScenario).all()}

    def _mk(flow):
        out = []
        for i, step in enumerate(flow):
            scen = _SPINE_SCENARIO.get(step["id"])
            out.append({
                "id": step["id"], "label": step["label"], "app": step["app"],
                "order": i, "status": "built" if (scen and scen in built) else "pending",
                "scenario": scen,
            })
        return out

    consumer = _mk(CONSUMER_FLOW)
    business = _mk(BUSINESS_FLOW)

    # within-lane sequential edges
    edges = []
    for lane in (CONSUMER_FLOW, BUSINESS_FLOW):
        for i in range(len(lane) - 1):
            edges.append({"source": lane[i]["id"], "target": lane[i + 1]["id"], "kind": "flow"})
    # cross-app handoff edges (the connection between the two apps)
    for e in CROSS_APP_EDGES:
        edges.append({**e, "kind": "handoff"})

    # branch endpoints (pay methods, order-later) with build status
    branches = _mk(BRANCH_NODES)

    return {
        "consumer": consumer, "business": business, "branches": branches,
        "nodes": consumer + business + branches,
        "edges": edges,                       # linear + handoff (for the lanes view)
        "graph_edges": WF_EDGES,              # full interconnected graph (with branches)
        "handoffs": CROSS_APP_EDGES, "summary": summary(),
    }


@router.get("/path")
def get_path(goal: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Agent brain: given a GOAL node, read the graph and return the path +
    the building-block scenarios to run, in order, to recreate that scenario."""
    labels = {n["id"]: n["label"] for n in all_nodes()}
    path = plan_path(goal)
    if not path:
        return {"goal": goal, "path": [], "steps": [], "scenarios": []}
    # Map each node on the path to the building-block scenario that performs it.
    scen_seen = []
    for nid in path:
        scen = _SPINE_SCENARIO.get(nid)
        if scen and scen not in scen_seen:
            scen_seen.append(scen)
    return {
        "goal": goal,
        "goal_label": labels.get(goal, goal),
        "path": [{"id": n, "label": labels.get(n, n)} for n in path],
        "scenarios": scen_seen,   # the ordered building blocks to run to reach the goal
    }


@router.post("/recreate")
def recreate(goal: str, db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    """Read the workflow → compose the path's building blocks → RUN them.

    This is the workflow driving execution: name a goal, the platform figures out
    the flow (no hand-written steps) and recreates it. Runs in the background.
    """
    import threading
    from automation.database.models import SavedScenario, TestProject

    path = plan_path(goal)
    if not path:
        return {"started": False, "error": f"No path to goal '{goal}'."}
    scen_names, seen = [], set()
    for nid in path:
        s = _SPINE_SCENARIO.get(nid)
        if s and s not in seen:
            seen.add(s); scen_names.append(s)
    if not scen_names:
        return {"started": False, "error": "No built scenarios cover this path yet.",
                "path": [n for n in path]}

    scenarios = db.query(SavedScenario).filter(SavedScenario.name.in_(scen_names)).all()
    by_name = {s.name: s for s in scenarios}
    ordered = [by_name[n] for n in scen_names if n in by_name]

    def _run():
        from automation.api.v1.routers.scenario import run_scenario_headless, ScenarioRequest
        from automation.database.config import SessionLocal
        for sc in ordered:
            try:
                req = ScenarioRequest(
                    project_id=sc.project_id or "", device_id=sc.device_id or "",
                    bundle_id=sc.bundle_id, steps=sc.steps or [],
                    name=f"[workflow] {sc.name}", save=False, prepare=False)
                with SessionLocal() as _db:
                    run_scenario_headless(req, _db)
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()
    return {"started": True, "goal": goal,
            "composed_from_workflow": scen_names,
            "message": "Platform read the workflow, composed the path, and is running it."}
