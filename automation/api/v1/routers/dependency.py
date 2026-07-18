"""Cross-app dependency analysis API.

Groups a set of apps (consumer / business / superadmin / backend), scans each for
the HTTP endpoints it calls, and answers the question that matters for
regression: "a file changed in app X — which OTHER apps must I re-test?"
"""

import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from automation.database.config import get_db
from automation.database.models import (
    ApplicationGroup,
    DependencyGraph,
    TestProject,
)
from automation.intelligence.cross_app_analyzer import cross_app_analyzer
from automation.intelligence.hybrid_impact_analyzer import hybrid_impact_analyzer
from automation.projects.detector import ProjectType, detect_project_type
from automation.projects.repository import repository_manager
from automation.runner.service import runner_service

logger = logging.getLogger("dependency")

router = APIRouter(prefix="/dependency", tags=["Cross-App Dependency"])

# The analyzer's scanners are keyed by source language, which is not the same as
# the platform's project type. Map one to the other.
_PROJECT_TYPE_TO_SCANNER = {
    ProjectType.IOS: "ios_swift",
    ProjectType.REACT_NATIVE: "react_native",
    ProjectType.PYTHON: "python",
    ProjectType.JAVA: "python",       # no Java scanner yet — see note below
    ProjectType.FLUTTER: "react_native",
    ProjectType.ANDROID: "react_native",
}


# ── Schemas ──────────────────────────────────────────────────────────────────

class ImpactRequest(BaseModel):
    changed_files: List[str]


class AnalyzeRequest(BaseModel):
    changed_files: List[str]
    group_id: Optional[str] = None
    # Queue a TestRun for every affected suite. Off by default: analysis should
    # be inspectable without silently spawning runs.
    create_runs: bool = False
    device_id: Optional[str] = None
    depth: int = 2


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_group_or_404(group_id: str, db: Session) -> ApplicationGroup:
    group = (
        db.query(ApplicationGroup).filter(ApplicationGroup.id == group_id).first()
    )
    if not group:
        raise HTTPException(status_code=404, detail="App group not found")
    return group


def _group_projects(group_id: str, db: Session) -> List[TestProject]:
    """Projects belonging to a group.

    Membership is TestProject.group_id — the same link the Projects page writes.
    This module used to own a SECOND group model (AppGroup/AppGroupMember) that
    nothing else ever wrote to, so a group created in the UI was invisible here and
    the graph always reported "no groups". One source of truth now.
    """
    return (
        db.query(TestProject)
        .filter(TestProject.group_id == group_id)
        .order_by(TestProject.name.asc())
        .all()
    )


def _latest_graph(group_id: str, db: Session) -> Optional[DependencyGraph]:
    return (
        db.query(DependencyGraph)
        .filter(DependencyGraph.group_id == group_id)
        .order_by(DependencyGraph.scanned_at.desc())
        .first()
    )


def _scanner_type(project: TestProject) -> str:
    """Which source scanner to use for a project.

    Prefers the type auto-detected from the cloned tree over the stored value,
    which can be stale.
    """
    repo_path = repository_manager.get_repo_path(project.id)
    ptype = project.project_type or ProjectType.UNKNOWN
    if repository_manager.is_cloned(project.id):
        ptype = detect_project_type(repo_path).project_type

    scanner = _PROJECT_TYPE_TO_SCANNER.get(ptype, "react_native")

    # React Native and React-on-the-web are both JS/TS and look identical to the
    # type detector — but a WEB app has no native project dir. Detect that rather
    # than making someone hand-tag the app with a role, which is a field that can
    # be set wrong and then silently scans the app with the wrong patterns.
    if scanner == "react_native" and repository_manager.is_cloned(project.id):
        has_native = any(
            os.path.isdir(os.path.join(repo_path, d)) for d in ("ios", "android")
        )
        if not has_native:
            scanner = "web_react"

    return scanner


def _members_as_apps(group: ApplicationGroup, db: Session) -> List[Dict[str, Any]]:
    """Turn a group's projects into the app dicts the analyzer expects."""
    apps: List[Dict[str, Any]] = []

    for project in _group_projects(group.id, db):
        apps.append({
            "name": project.name,
            "path": repository_manager.get_repo_path(project.id),
            "type": _scanner_type(project),
            "test_suite": f"{project.name.lower().replace(' ', '-')}-tests",
            "project_id": project.id,
            "cloned": repository_manager.is_cloned(project.id),
        })

    return apps


def _serialize_group(group: ApplicationGroup, db: Session) -> Dict[str, Any]:
    members = [
        {
            "id": p.id,
            "project_id": p.id,
            "project_name": p.name,
            "platform": p.platform,
            "scanner": _scanner_type(p),
            "cloned": repository_manager.is_cloned(p.id),
        }
        for p in _group_projects(group.id, db)
    ]

    latest = _latest_graph(group.id, db)
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "created_at": group.created_at.isoformat() if group.created_at else None,
        "members": members,
        "member_count": len(members),
        "last_scanned_at": latest.scanned_at.isoformat() if latest and latest.scanned_at else None,
        "has_graph": latest is not None,
    }


# ── Endpoints ────────────────────────────────────────────────────────────────

# Groups are created / deleted through /groups (routers/groups.py), which is what
# the Projects page uses. This module only READS them — owning a second way to
# create a group is what split the data in the first place.

@router.get("/groups")
def list_groups(db: Session = Depends(get_db)):
    """List all app groups."""
    groups = (
        db.query(ApplicationGroup).order_by(ApplicationGroup.name.asc()).all()
    )
    return {"groups": [_serialize_group(g, db) for g in groups]}


@router.get("/groups/{group_id}")
def get_group(group_id: str, db: Session = Depends(get_db)):
    """Get a group with its members and the latest dependency graph."""
    group = _get_group_or_404(group_id, db)
    latest = _latest_graph(group_id, db)

    return {
        "group": _serialize_group(group, db),
        "graph": latest.graph_data if latest else None,
        "api_endpoints": latest.api_endpoints if latest else None,
        "scanned_at": latest.scanned_at.isoformat() if latest and latest.scanned_at else None,
    }


@router.post("/groups/{group_id}/scan")
def scan_group(group_id: str, db: Session = Depends(get_db)):
    """Scan every app in the group for API usage and persist the graph."""
    group = _get_group_or_404(group_id, db)
    apps = _members_as_apps(group, db)

    if not apps:
        raise HTTPException(
            status_code=400, detail="This group has no member projects to scan."
        )

    not_cloned = [a["name"] for a in apps if not a["cloned"]]
    scannable = [a for a in apps if a["cloned"]]

    if not scannable:
        raise HTTPException(
            status_code=400,
            detail=(
                "None of this group's projects are cloned locally, so there is no "
                f"source to scan. Clone them first: {', '.join(not_cloned)}"
            ),
        )

    graph = cross_app_analyzer.build_dependency_graph(scannable)

    record = DependencyGraph(
        id=str(uuid.uuid4()),
        group_id=group_id,
        graph_data=graph,
        api_endpoints=graph.get("endpoints", {}),
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    return {
        "status": "scanned",
        "graph": graph,
        "stats": graph.get("stats", {}),
        "scanned_at": record.scanned_at.isoformat() if record.scanned_at else None,
        # Be explicit about what was NOT covered rather than silently reporting
        # a partial graph as complete.
        "skipped_not_cloned": not_cloned,
    }


@router.post("/groups/{group_id}/impact")
def analyze_impact(
    group_id: str, body: ImpactRequest, db: Session = Depends(get_db)
):
    """Cross-app impact of a set of changed files."""
    _get_group_or_404(group_id, db)

    latest = _latest_graph(group_id, db)
    if not latest or not latest.graph_data:
        raise HTTPException(
            status_code=400,
            detail="No dependency graph for this group yet. Run a scan first.",
        )

    if not body.changed_files:
        raise HTTPException(status_code=400, detail="changed_files must not be empty.")

    impact = cross_app_analyzer.find_cross_app_impact(
        body.changed_files, latest.graph_data
    )
    impact["group_id"] = group_id
    impact["graph_scanned_at"] = (
        latest.scanned_at.isoformat() if latest.scanned_at else None
    )
    return impact


def _hybrid_app_configs(group: ApplicationGroup, db: Session) -> List[Dict[str, Any]]:
    """Group members shaped for HybridImpactAnalyzer."""
    configs: List[Dict[str, Any]] = []
    for app in _members_as_apps(group, db):
        configs.append({
            "name": app["name"],
            "repo_path": app["path"],
            "app_type": app["type"],
            "app_role": app["app_role"],
            "test_suite": app["test_suite"],
            "project_id": app["project_id"],
            "cloned": app["cloned"],
        })
    return configs


@router.post("/analyze")
def analyze_impact_hybrid(body: AnalyzeRequest, db: Session = Depends(get_db)):
    """Hybrid cross-app impact analysis.

    Graphify supplies the intra-app blast radius (which other files in the same
    app depend on the change); the endpoint graph supplies the inter-app layer
    (which OTHER apps call the endpoints that blast radius touches).

    That combination catches a change to a file with no HTTP call of its own —
    a model or a constant — which an endpoint-only scan misses entirely.
    """
    if not body.changed_files:
        raise HTTPException(status_code=400, detail="changed_files must not be empty.")
    if not body.group_id:
        raise HTTPException(status_code=400, detail="group_id is required.")

    group = _get_group_or_404(body.group_id, db)
    configs = _hybrid_app_configs(group, db)

    if not configs:
        raise HTTPException(status_code=400, detail="This group has no member projects.")

    not_cloned = [c["name"] for c in configs if not c["cloned"]]
    scannable = [c for c in configs if c["cloned"]]
    if not scannable:
        raise HTTPException(
            status_code=400,
            detail=(
                "None of this group's projects are cloned locally, so there is no "
                f"source to analyse. Clone them first: {', '.join(not_cloned)}"
            ),
        )

    result = hybrid_impact_analyzer.analyze(
        body.changed_files, scannable, depth=body.depth
    )
    result["group_id"] = group.id
    result["group_name"] = group.name
    # Never let a partial analysis look complete.
    result["skipped_not_cloned"] = not_cloned

    # Optionally queue a run for every affected suite.
    queued: List[Dict[str, Any]] = []
    if body.create_runs:
        by_name = {c["name"]: c for c in scannable}
        targets = [result["primary_app"]] if result.get("primary_app") else []
        targets += [c["app"] for c in result.get("cross_app_impact", [])]

        for app_name in targets:
            cfg = by_name.get(app_name)
            if not cfg or not cfg.get("project_id"):
                continue
            try:
                run_id = runner_service.execute_project(
                    project_id=cfg["project_id"],
                    device_id=body.device_id or "pending",
                    triggered_by=f"cross-app-impact:{group.name}",
                )
                queued.append({
                    "app": app_name,
                    "run_id": run_id,
                    "test_suite": cfg.get("test_suite"),
                })
            except Exception as e:
                logger.error("Could not queue run for %s: %s", app_name, e)
                queued.append({"app": app_name, "run_id": None, "error": str(e)})

    result["queued_runs"] = queued
    return result


@router.get("/groups/{group_id}/graph")
def get_graph(group_id: str, db: Session = Depends(get_db)):
    """Graph data shaped for a D3 force-directed layout."""
    _get_group_or_404(group_id, db)

    latest = _latest_graph(group_id, db)
    if not latest or not latest.graph_data:
        return {"nodes": [], "links": [], "stats": {}, "scanned_at": None}

    graph = latest.graph_data
    app_nodes = {n["id"]: n for n in graph.get("nodes", []) if n.get("type") == "app"}

    nodes = []
    for n in graph.get("nodes", []):
        node = {
            "id": n["id"],
            "type": n.get("type"),
            "label": n["id"],
        }
        if n.get("type") == "app":
            node.update({
                "app_role": n.get("app_role"),
                "app_type": n.get("app_type"),
                "test_suite": n.get("test_suite"),
                "project_id": n.get("project_id"),
            })
        else:
            # How many apps depend on this endpoint — drives node emphasis and
            # tells you at a glance where the cross-app risk is.
            users = graph.get("endpoints", {}).get(n["id"], {}).get("apps", [])
            node.update({"used_by": users, "shared": len(users) > 1})
        nodes.append(node)

    # D3 expects source/target. Collapse duplicate edges (an app may call the
    # same endpoint from many files) and keep the call sites on the link.
    links_by_pair: Dict[str, Dict[str, Any]] = {}
    for e in graph.get("edges", []):
        key = f"{e['from']}->{e['to']}"
        link = links_by_pair.setdefault(key, {
            "source": e["from"],
            "target": e["to"],
            "call_sites": [],
        })
        site = f"{e.get('file', '')}:{e.get('line', 0)}"
        if site not in link["call_sites"]:
            link["call_sites"].append(site)

    for link in links_by_pair.values():
        link["value"] = len(link["call_sites"])

    return {
        "nodes": nodes,
        "links": list(links_by_pair.values()),
        "shared_endpoints": graph.get("shared_endpoints", []),
        "stats": graph.get("stats", {}),
        "app_count": len(app_nodes),
        "scanned_at": latest.scanned_at.isoformat() if latest.scanned_at else None,
    }


@router.get("/projects/{project_id}/module-graph")
def get_module_graph(project_id: str, db: Session = Depends(get_db)):
    """Intra-app FILE dependency graph (Graphify) for a single project.

    Same {nodes, links} shape as the endpoint graph, so the dashboard renders it
    with the same force layout — just a different data source (one app's internal
    files instead of apps joined by shared endpoints).
    """
    project = db.query(TestProject).filter(TestProject.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not repository_manager.is_cloned(project_id):
        raise HTTPException(status_code=400, detail="Project is not cloned yet.")

    repo_path = repository_manager.get_repo_path(project_id)
    graph = hybrid_impact_analyzer.build_module_graph(repo_path)
    if not graph.get("available"):
        raise HTTPException(
            status_code=503,
            detail="Graphify is not installed — the module graph is unavailable.",
        )
    return graph
