from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Dict, Any, Optional
import uuid
import os

from automation.database import database
from automation.database.database import utc_iso
from automation.database.config import get_db
from automation.database.models import TestProject
from automation.projects.repository import repository_manager
from automation.projects.detector import (
    PROJECT_TYPE_LABELS,
    detect_project_type,
    write_automation_yaml,
)
from automation.projects.preparation import preparation_service, preparation_tracker
from automation.auth.security import get_current_user
from pydantic import BaseModel

router = APIRouter(prefix="/projects", tags=["projects"])

# Allowed enum values for the editable fields.
_PLATFORMS = {"ios", "android"}
_REPO_TYPES = {"github", "gitlab", "local"}

# ── Extensions and directories that the editor is allowed to open ────────────
_ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {".py", ".yaml", ".yml", ".json", ".txt", ".md"}
)
_SKIP_DIRS: frozenset[str] = frozenset(
    {"__pycache__", ".git", ".venv", "node_modules", ".pytest_cache", ".mypy_cache"}
)

# Directories that hold the platform's TEST scripts (as opposed to the app's own
# source). The Scripts tab lists these, not the whole repo.
_TEST_DIRS: frozenset[str] = frozenset({"e2e", "tests", "test", "e2e_tests", "appium"})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_project_or_404(project_id: str, db: Session) -> TestProject:
    project = db.query(TestProject).filter(TestProject.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


# ── Project CRUD ──────────────────────────────────────────────────────────────

class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    git_url: str
    default_branch: str = "main"
    platform: str = "ios"          # ios | android
    repo_type: str = "github"      # github | gitlab | local
    group_id: Optional[str] = None  # optional — projects may be ungrouped
    # The bundle id this project is meant to PRODUCE. Optional, and normally left
    # unset: a build records whatever it produced. It has to be declarable for
    # variants, though -- a staging project differs from its production twin only by
    # bundle id, and the platform must know that id BEFORE building in order to pass
    # the override. Left to the build alone the field is circular (the build sets it,
    # so it can only ever be the production id), which is exactly how a project named
    # "staging" ended up carrying a production artifact.
    app_bundle_id: Optional[str] = None
    # A name from project-environments.json ("production", "staging"). Resolved to
    # that environment's bundle id -- the dashboard picks an environment, never
    # types a bundle id. Wins over app_bundle_id when both are sent.
    environment: Optional[str] = None


class ProjectUpdate(BaseModel):
    """All fields optional — only the supplied ones are changed."""
    name: Optional[str] = None
    description: Optional[str] = None
    git_url: Optional[str] = None
    default_branch: Optional[str] = None
    platform: Optional[str] = None
    repo_type: Optional[str] = None
    group_id: Optional[str] = None
    app_bundle_id: Optional[str] = None   # see ProjectCreate.app_bundle_id
    environment: Optional[str] = None     # see ProjectCreate.environment


def _environment_name(bundle_id: Optional[str]) -> Optional[str]:
    from automation.projects import environments as envmod
    return envmod.environment_for_bundle(bundle_id) if bundle_id else None


def _environments_for(bundle_id: Optional[str], name: Optional[str]) -> list:
    """The environments project-environments.json defines for this project."""
    from automation.projects import environments as envmod
    entry = envmod.find_project_entry(bundle_id=bundle_id, name=name)
    envs = (entry or {}).get("environments") or {}
    return [{"name": n, "bundle_id": e.get("bundle_id"),
             "api_base_url": e.get("api_base_url")}
            for n, e in sorted(envs.items())]


def _bundle_for_environment(environment: str, bundle_id: Optional[str],
                            name: Optional[str]) -> str:
    """The bundle id that makes a project build as *environment*, or 400."""
    from automation.projects import environments as envmod
    try:
        cfg = envmod.resolve(environment, bundle_id=bundle_id, name=name)
    except envmod.EnvironmentNotConfigured as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not cfg.bundle_id:
        raise HTTPException(status_code=400, detail=(
            f"Environment '{cfg.environment}' declares no bundle_id in "
            "project-environments.json, so it cannot be selected."))
    return cfg.bundle_id


def _validate_enums(platform: Optional[str], repo_type: Optional[str]) -> None:
    if platform is not None and platform not in _PLATFORMS:
        raise HTTPException(
            status_code=400,
            detail=f"platform must be one of {sorted(_PLATFORMS)}",
        )
    if repo_type is not None and repo_type not in _REPO_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"repo_type must be one of {sorted(_REPO_TYPES)}",
        )


def _agent_status(db: Session) -> str:
    """'online' when at least one execution agent is available, else 'offline'."""
    from automation.database.models import ExecutionAgent

    try:
        online = (
            db.query(ExecutionAgent)
            .filter(ExecutionAgent.status.in_(["online", "busy"]))
            .count()
        )
        return "online" if online else "offline"
    except Exception:
        return "unknown"


def _last_run(db: Session, project_id: str) -> Optional[Dict[str, Any]]:
    """Most recent run for this project, for the health panel."""
    from automation.database.models import TestRun

    try:
        run = (
            db.query(TestRun)
            .filter(TestRun.project_id == project_id)
            .order_by(TestRun.created_at.desc())
            .first()
        )
        if not run:
            return None
        return {
            "id": run.id,
            "status": run.status,
            "job_state": run.job_state,
            "created_at": utc_iso(run.created_at),
            "duration_ms": run.duration_ms,
        }
    except Exception:
        return None


def _serialize(p: TestProject, db: Optional[Session] = None) -> Dict[str, Any]:
    """Full dashboard-facing representation of a project."""
    cloned = repository_manager.is_cloned(p.id)
    repo_path = repository_manager.get_repo_path(p.id)

    # Trust the live filesystem over a stale DB flag.
    clone_status = p.clone_status or "not_cloned"
    if not cloned and clone_status not in ("syncing", "clone_failed"):
        clone_status = "not_cloned"

    project_type = p.project_type or "unknown"
    if cloned:
        project_type = detect_project_type(repo_path).project_type

    repo_health = repository_manager.get_health(p.id)
    has_yaml = (
        os.path.exists(os.path.join(repo_path, "automation.yaml")) if cloned else False
    )

    # Dependencies are "installed" per the toolchain the project actually uses —
    # a React Native project must never be judged on a Python venv.
    if project_type == "python":
        deps_ok = repo_health.get("dependencies_installed", False)
    elif project_type == "react_native":
        deps_ok = os.path.isdir(os.path.join(repo_path, "node_modules")) if cloned else False
    else:
        # Native / Flutter / Java resolve dependencies as part of their build.
        deps_ok = cloned

    return {
        "id": p.id,
        "group_id": p.group_id,
        "group_name": p.group.name if p.group else None,
        "name": p.name,
        "description": p.description,
        "git_url": p.git_url,
        "default_branch": p.default_branch,
        "status": p.status,
        "platform": p.platform or "ios",
        "repo_type": p.repo_type or "github",
        "project_type": project_type,
        "project_type_label": PROJECT_TYPE_LABELS.get(project_type, "Unknown"),
        "clone_status": clone_status,
        "clone_error": p.clone_error,
        "app_bundle_id": p.app_bundle_id,
        "environment": _environment_name(p.app_bundle_id),
        "environments": _environments_for(p.app_bundle_id, p.name),
        "local_path": repo_path,
        "current_branch": repository_manager.get_current_branch(p.id),
        "has_automation_yaml": has_yaml,
        "last_pull_at": utc_iso(p.last_pull_at),
        "last_execution_at": utc_iso(p.last_execution_at),
        "health": {
            **repo_health,
            "dependencies_ok": deps_ok,
            "automation_yaml": has_yaml,
            "agent_status": _agent_status(db) if db is not None else "unknown",
            "last_run": _last_run(db, p.id) if db is not None else None,
        },
    }


@router.get("/")
def list_projects(db: Session = Depends(get_db)):
    projects = database.get_test_projects(db)
    return {"projects": [_serialize(p, db) for p in projects]}


@router.post("/")
def create_project(project: ProjectCreate, db: Session = Depends(get_db)):
    """Register a project. The clone is kicked off but never blocks the response.

    A clone failure leaves the project in ``clone_status="clone_failed"`` with
    the git error stored — the dashboard surfaces it and offers a retry, rather
    than the registration itself appearing to fail.
    """
    _validate_enums(project.platform, project.repo_type)
    if project.environment:
        project.app_bundle_id = _bundle_for_environment(
            project.environment, project.app_bundle_id, project.name)

    project_id = str(uuid.uuid4())
    database.insert_test_project(db, {
        "id": project_id,
        "name": project.name,
        "description": project.description,
        "git_url": project.git_url,
        "default_branch": project.default_branch,
        "status": "active",
        "platform": project.platform,
        "repo_type": project.repo_type,
        "group_id": project.group_id or None,
        "clone_status": "not_cloned",
        "project_type": "unknown",
        "app_bundle_id": project.app_bundle_id or None,
    })

    # Clone + detect type immediately so the card is useful straight away.
    sync = preparation_service.sync_repository(
        project_id, project.git_url, project.default_branch
    )
    if sync.ok:
        preparation_service.detect_and_store_type(project_id)

    db.expire_all()
    created = db.query(TestProject).filter(TestProject.id == project_id).first()
    return {"status": "success", "id": project_id, "project": _serialize(created, db)}


@router.put("/{project_id}")
def update_project(project_id: str, body: ProjectUpdate, db: Session = Depends(get_db)):
    """Edit a project. Changing git_url invalidates the existing clone."""
    project = _get_project_or_404(project_id, db)
    _validate_enums(body.platform, body.repo_type)

    # group_id may be explicitly set to null to detach a project from its group,
    # so it is the one field where None is a meaningful value.
    raw = body.model_dump(exclude_unset=True)
    updates = {
        k: v for k, v in raw.items() if v is not None or k == "group_id"
    }
    environment = updates.pop("environment", None)
    if environment:
        updates["app_bundle_id"] = _bundle_for_environment(
            environment, project.app_bundle_id, updates.get("name", project.name))

    # If the remote changed, the local checkout no longer corresponds to it.
    if "git_url" in updates and updates["git_url"] != project.git_url:
        repository_manager.delete_local_repo(project_id)
        project.clone_status = "not_cloned"
        project.current_branch = None
        project.last_pull_at = None
        project.clone_error = None

    for key, value in updates.items():
        setattr(project, key, value)

    db.commit()
    db.refresh(project)
    return {"status": "updated", "project": _serialize(project, db)}


@router.delete("/{project_id}")
def delete_project(
    project_id: str,
    delete_local: bool = False,
    purge: bool = False,
    dry_run: bool = False,
    db: Session = Depends(get_db),
):
    """Delete a project from the database.

    ``delete_local=true`` also removes the cloned repository from disk — the
    dashboard asks for confirmation before sending it.

    ``purge=true`` removes everything else the project left behind as well: the
    baselines, project settings (which hold jira/github tokens), saved scenarios,
    tickets, AI recommendations, the learned-locator entry, and the app itself from
    any booted simulator carrying it. Without it those rows simply lose the project
    they pointed at — which is how this database ended up with recommendations
    belonging to projects that no longer exist, and the app stayed installed so a
    "deleted" project could still be driven by a run.

    An app whose bundle id another project also uses is KEPT, and the response says
    so in ``app_kept_because``.

    Test history is KEPT either way. A purge detaches runs (``project_id`` goes
    null) rather than deleting them, so what was tested and what happened stays on
    the record. ``dry_run=true`` reports what a purge would remove and changes
    nothing.
    """
    _get_project_or_404(project_id, db)

    if purge:
        from automation.projects.purge import purge_project
        plan = purge_project(db, project_id, dry_run=dry_run)
        return {
            "status": "would_purge" if dry_run else "purged",
            "id": project_id,
            "local_repository_deleted": bool(plan.repo_path) and not dry_run,
            "baselines_deleted": bool(plan.baseline_path) and not dry_run,
            "rows_deleted": plan.rows,
            "runs_detached": plan.runs_detached,
            "bytes_freed": plan.repo_bytes,
            "locator_entry_removed": plan.locator_key,
            "app_uninstalled_from": plan.uninstall_from if dry_run else plan.uninstalled,
            "app_kept_because": plan.app_kept_because,
            "warnings": plan.warnings,
        }

    project = _get_project_or_404(project_id, db)
    local_deleted = False
    if delete_local:
        local_deleted = repository_manager.delete_local_repo(project_id)

    db.delete(project)
    db.commit()

    return {
        "status": "deleted",
        "id": project_id,
        "local_repository_deleted": local_deleted,
    }


# ── Repository management ─────────────────────────────────────────────────────

@router.get("/branches-for-url")
def list_branches_for_url(git_url: str):
    """Branches for a repo URL, for the Register Project form.

    The project does not exist yet there, so there is no id to ask by — and that
    form is exactly where a branch was typed blind.
    """
    branches = repository_manager.list_remote_branches(git_url)
    if not branches:
        raise HTTPException(
            status_code=502,
            detail=f"Could not read branches from {git_url} — check the URL and "
                   "that credentials are configured.",
        )
    return {"branches": branches, "default": branches[0]}


@router.get("/{project_id}/branches")
def list_branches(project_id: str, db: Session = Depends(get_db)):
    """Every branch on the project's remote, default first.

    Reads the remote directly, so it answers for a project that has never been
    cloned — which is exactly when the branch needs choosing. The dashboard
    offers these instead of asking someone to type a branch from memory: a typo
    otherwise surfaces only when the clone fails.
    """
    project = _get_project_or_404(project_id, db)
    branches = repository_manager.list_remote_branches(project.git_url)
    if not branches:
        raise HTTPException(
            status_code=502,
            detail=f"Could not read branches from {project.git_url} — "
                   "check the URL and that credentials are configured.",
        )
    return {
        "branches": branches,
        "default": branches[0],
        "current": project.current_branch or project.default_branch,
    }


@router.post("/{project_id}/clone")
def clone_repository(
    project_id: str,
    force: bool = False,
    branch: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Clone the repository. ``force=true`` performs a full re-clone.

    ``branch`` checks out that branch instead of the project's default — the
    dashboard passes whichever one was picked from ``GET /branches``.
    """
    project = _get_project_or_404(project_id, db)

    result = preparation_service.sync_repository(
        project_id,
        project.git_url,
        branch or project.default_branch or "main",
        force_reclone=force,
    )
    if result.ok:
        preparation_service.detect_and_store_type(project_id)

    db.expire_all()
    refreshed = db.query(TestProject).filter(TestProject.id == project_id).first()

    if not result.ok:
        raise HTTPException(
            status_code=400,
            detail={"error": result.error, "steps": result.steps},
        )

    return {"status": "cloned", "steps": result.steps, "project": _serialize(refreshed, db)}


@router.post("/{project_id}/pull")
def pull_repository(project_id: str, db: Session = Depends(get_db)):
    """Pull the latest changes for the configured branch."""
    project = _get_project_or_404(project_id, db)

    if not repository_manager.is_cloned(project_id):
        raise HTTPException(
            status_code=400,
            detail="Repository is not cloned yet. Clone it first.",
        )

    result = preparation_service.sync_repository(
        project_id, project.git_url, project.default_branch or "main"
    )
    if result.ok:
        preparation_service.detect_and_store_type(project_id)

    db.expire_all()
    refreshed = db.query(TestProject).filter(TestProject.id == project_id).first()

    if not result.ok:
        raise HTTPException(
            status_code=400,
            detail={"error": result.error, "steps": result.steps},
        )

    return {"status": "pulled", "steps": result.steps, "project": _serialize(refreshed, db)}


class ValidateBody(BaseModel):
    device_id: Optional[str] = None
    # When true, a missing automation.yaml is generated instead of failing.
    generate_yaml: bool = False
    # Check out this branch before preparing. Without it the pipeline prepares
    # whatever happens to be checked out, so picking a branch on the card and
    # pressing Execute silently built the previous one.
    branch: Optional[str] = None


@router.post("/{project_id}/prepare", status_code=202)
def start_preparation(
    project_id: str,
    body: ValidateBody = ValidateBody(),
    db: Session = Depends(get_db),
):
    """Start the full preparation pipeline in the BACKGROUND and return immediately.

    clone/pull → checkout → detect → validate → install deps → build app →
    install app on the device → launch it.

    A cold xcodebuild takes minutes, so this must not run inside the request:
    holding a worker thread that long starved the execution agent's heartbeat.
    Poll ``GET /projects/{id}/prepare/status`` for progress.
    """
    _get_project_or_404(project_id, db)
    return preparation_tracker.start(
        project_id, device_id=body.device_id, generate_yaml=body.generate_yaml,
        branch=body.branch,
    )


@router.get("/{project_id}/prepare/status")
def preparation_status(project_id: str, db: Session = Depends(get_db)):
    """Live progress of the background preparation task.

    ``status`` is idle | running | completed | failed; ``steps`` streams the
    pipeline log; ``result`` carries the PreparationResult once finished.
    """
    _get_project_or_404(project_id, db)
    return preparation_tracker.status(project_id)


@router.get("/{project_id}/trend-summary")
def get_trend_summary(
    project_id: str,
    days: int = 7,
    current_user=Depends(get_current_user),
):
    """AI-generated weekly-trend summary for a project's recent runs."""
    from automation.ai.services.summary import test_summary_generator
    return test_summary_generator.generate_trend_summary(project_id, days=days)

