from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional
import uuid
import os
import pathlib
import subprocess

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
from automation.runner.service import runner_service
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


def _is_test_script(rel_path: "pathlib.PurePath") -> bool:
    """A test script = a .py under a test directory, or a test_*.py / *_test.py
    anywhere, or the project's automation.yaml. Filters out app source & config
    JSON so the Scripts tab shows scripts, not the whole repo."""
    parts = rel_path.parts
    name = rel_path.name
    ext = rel_path.suffix.lower()
    if name in ("automation.yaml", "automation.yml"):
        return True
    if ext != ".py":
        return False
    if any(seg in _TEST_DIRS for seg in parts[:-1]):
        return True
    stem = rel_path.stem
    return stem.startswith("test_") or stem.endswith("_test")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_project_or_404(project_id: str, db: Session) -> TestProject:
    project = db.query(TestProject).filter(TestProject.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _get_repo_root(project_id: str) -> pathlib.Path:
    """Return the resolved absolute path to the cloned repository.

    Raises 404 if the repository has not been cloned yet.
    """
    raw = repository_manager.get_repo_path(project_id)
    root = pathlib.Path(raw).resolve()
    if not root.exists():
        raise HTTPException(
            status_code=404,
            detail="Repository not cloned yet. Trigger a run or project sync first.",
        )
    return root


def _safe_resolve(repo_root: pathlib.Path, file_path: str) -> pathlib.Path:
    """Resolve *file_path* relative to *repo_root* and reject path-traversal.

    Raises 400 if the resolved path escapes the repository directory.
    """
    # Normalise separators (Windows paths from the frontend may use backslash).
    normalised = file_path.replace("\\", "/").lstrip("/")
    target = (repo_root / normalised).resolve()
    if not str(target).startswith(str(repo_root) + os.sep) and target != repo_root:
        raise HTTPException(status_code=400, detail="Invalid file path")
    return target


# ── Project CRUD ──────────────────────────────────────────────────────────────

class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    git_url: str
    default_branch: str = "main"
    platform: str = "ios"          # ios | android
    repo_type: str = "github"      # github | gitlab | local
    group_id: Optional[str] = None  # optional — projects may be ungrouped


class ProjectUpdate(BaseModel):
    """All fields optional — only the supplied ones are changed."""
    name: Optional[str] = None
    description: Optional[str] = None
    git_url: Optional[str] = None
    default_branch: Optional[str] = None
    platform: Optional[str] = None
    repo_type: Optional[str] = None
    group_id: Optional[str] = None


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


@router.get("/{project_id}")
def get_project(project_id: str, db: Session = Depends(get_db)):
    return {"project": _serialize(_get_project_or_404(project_id, db), db)}


@router.post("/")
def create_project(project: ProjectCreate, db: Session = Depends(get_db)):
    """Register a project. The clone is kicked off but never blocks the response.

    A clone failure leaves the project in ``clone_status="clone_failed"`` with
    the git error stored — the dashboard surfaces it and offers a retry, rather
    than the registration itself appearing to fail.
    """
    _validate_enums(project.platform, project.repo_type)

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
    tickets, AI recommendations and the learned-locator entry. Without it those
    rows simply lose the project they pointed at — which is how this database
    ended up with recommendations belonging to projects that no longer exist.

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

@router.post("/{project_id}/clone")
def clone_repository(
    project_id: str,
    force: bool = False,
    db: Session = Depends(get_db),
):
    """Clone the repository. ``force=true`` performs a full re-clone."""
    project = _get_project_or_404(project_id, db)

    result = preparation_service.sync_repository(
        project_id,
        project.git_url,
        project.default_branch or "main",
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


@router.get("/{project_id}/status")
def project_status(
    project_id: str,
    check_remote: bool = False,
    db: Session = Depends(get_db),
):
    """Repository + health status. ``check_remote=true`` also detects 'outdated'."""
    _get_project_or_404(project_id, db)
    return preparation_service.get_status(project_id, check_remote=check_remote)


class ValidateBody(BaseModel):
    device_id: Optional[str] = None
    # When true, a missing automation.yaml is generated instead of failing.
    generate_yaml: bool = False


@router.post("/{project_id}/validate")
def validate_project(
    project_id: str,
    body: ValidateBody = ValidateBody(),
    db: Session = Depends(get_db),
):
    """Run the full preparation pipeline: clone/pull → checkout → detect → validate → install.

    Validation happens only AFTER the repository is on disk and prepared, so it
    can no longer fail with "automation.yaml not found" / ".venv not found"
    simply because the clone had not happened yet.

    When automation.yaml is missing the response carries
    ``needs_automation_yaml: true`` so the dashboard can offer to generate one.
    """
    _get_project_or_404(project_id, db)

    result = preparation_service.prepare_for_execution(
        project_id,
        device_id=body.device_id,
        auto_generate_yaml=body.generate_yaml,
    )
    return result.to_dict()


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
        project_id, device_id=body.device_id, generate_yaml=body.generate_yaml
    )


@router.get("/{project_id}/prepare/status")
def preparation_status(project_id: str, db: Session = Depends(get_db)):
    """Live progress of the background preparation task.

    ``status`` is idle | running | completed | failed; ``steps`` streams the
    pipeline log; ``result`` carries the PreparationResult once finished.
    """
    _get_project_or_404(project_id, db)
    return preparation_tracker.status(project_id)


@router.post("/{project_id}/generate-yaml")
def generate_yaml(project_id: str, db: Session = Depends(get_db)):
    """Scaffold an automation.yaml based on the detected project type."""
    project = _get_project_or_404(project_id, db)

    if not repository_manager.is_cloned(project_id):
        raise HTTPException(
            status_code=400, detail="Repository is not cloned yet. Clone it first."
        )

    repo_path = repository_manager.get_repo_path(project_id)
    detection = detect_project_type(repo_path)

    content = write_automation_yaml(
        repo_path,
        project_name=project.name,
        project_type=detection.project_type,
        platform=project.platform or "ios",
        branch=project.default_branch or "main",
    )

    project.project_type = detection.project_type
    db.commit()

    return {
        "status": "generated",
        "project_type": detection.project_type,
        "content": content,
    }


# ── File browser endpoints ────────────────────────────────────────────────────

@router.get(
    "/{project_id}/files",
    summary="List all editable files in a project repository",
    dependencies=[Depends(get_current_user)],
)
def list_project_files(
    project_id: str,
    scripts_only: bool = True,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """List repository files for the editor.

    By default (``scripts_only=True``) only TEST scripts are returned — the
    Scripts tab is for authoring/running test scripts, not browsing the app's
    own source and config JSON. Pass ``scripts_only=false`` to walk the whole
    repo (all allowed extensions). Hidden files and build/cache dirs are always
    excluded.
    """
    _get_project_or_404(project_id, db)
    repo_root = _get_repo_root(project_id)

    files: List[Dict[str, str]] = []

    for root, dirs, filenames in os.walk(repo_root):
        # Prune unwanted directories in-place so os.walk skips them.
        dirs[:] = sorted(
            d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")
        )

        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            _, ext = os.path.splitext(filename)
            if ext not in _ALLOWED_EXTENSIONS:
                continue

            full_path = pathlib.Path(root) / filename
            rel_path = full_path.relative_to(repo_root)
            if scripts_only and not _is_test_script(rel_path):
                continue
            # Always return POSIX-style paths regardless of the host OS.
            files.append(
                {
                    "path": rel_path.as_posix(),
                    "name": filename,
                    "type": "file",
                }
            )

    return {"files": files}


@router.get(
    "/{project_id}/files/{file_path:path}",
    response_class=PlainTextResponse,
    summary="Read the content of a project file",
    dependencies=[Depends(get_current_user)],
)
def get_project_file(
    project_id: str,
    file_path: str,
    db: Session = Depends(get_db),
) -> str:
    """Return the raw UTF-8 text content of *file_path* inside the repository."""
    _get_project_or_404(project_id, db)
    repo_root = _get_repo_root(project_id)
    target = _safe_resolve(repo_root, file_path)

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")

    return target.read_text(encoding="utf-8", errors="replace")


class FileUpdateBody(BaseModel):
    content: str


@router.put(
    "/{project_id}/files/{file_path:path}",
    summary="Save a file and git-commit the change",
    dependencies=[Depends(get_current_user)],
)
def save_project_file(
    project_id: str,
    file_path: str,
    body: FileUpdateBody,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Overwrite *file_path* with *body.content* and create a git commit.

    The commit message is: ``Platform edit: {file_path} via dashboard``

    Returns ``{"status": "saved", "committed": true|false}``.
    A ``committed: false`` result means the file was written but the git
    operation failed (e.g. nothing changed, no git identity set).  This is
    non-fatal — the content on disk is correct regardless.
    """
    _get_project_or_404(project_id, db)
    repo_root = _get_repo_root(project_id)
    target = _safe_resolve(repo_root, file_path)

    # Write content to disk.
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body.content, encoding="utf-8")

    # Attempt to git-commit the change.
    committed = False
    try:
        add = subprocess.run(
            ["git", "add", file_path],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if add.returncode == 0:
            commit_msg = f"Platform edit: {file_path} via dashboard"
            commit = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=15,
            )
            committed = commit.returncode == 0
    except Exception:
        # Commit failure is non-fatal; file is already written.
        committed = False

    return {"status": "saved", "committed": committed}


class FileRunBody(BaseModel):
    device_id: str


@router.post(
    "/{project_id}/files/{file_path:path}/run",
    summary="Trigger a test run for a specific file",
    dependencies=[Depends(get_current_user)],
)
def run_project_file(
    project_id: str,
    file_path: str,
    body: FileRunBody,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Enqueue a new test run scoped to *file_path*.

    The run is created with ``triggered_by="Script Editor"`` so it can be
    distinguished in the dashboard from regular automation runs.

    Returns ``{"run_id": "<uuid>"}`` on success.
    """
    project = _get_project_or_404(project_id, db)

    device_id = body.device_id
    # The user picks the simulator from a dropdown, so honour that exact choice
    # (prefer_requested) — do NOT jump to whatever else is booted. Only an invalid
    # id (e.g. a stale UDID or an Android 'emulator-5554') falls back to a real sim.
    if (project.platform or "ios").lower() == "ios":
        from automation.projects.builder import app_builder
        from automation.device_manager.service import device_service
        resolved, _ = app_builder.resolve_ios_device(device_id, prefer_requested=True)
        if resolved:
            device_id = resolved
        # Boot exactly the chosen simulator so it — not some other booted one — is
        # what runs. Booting a cold sim adds a few seconds; that's expected.
        app_builder.ensure_ios_booted(device_id)
        # The agent-fed registry can be empty (e.g. just after a restart), which
        # would fail execute_project's device check. Register the chosen sim.
        if not device_service.get_device(device_id):
            from automation.scenarios.cross_app_config import list_ios_simulators
            name = next((s["name"] for s in list_ios_simulators()
                         if s["udid"] == device_id), "iOS Simulator")
            device_service.register_local_device(device_id, name)

    try:
        run_id = runner_service.execute_project(
            project_id=project_id,
            device_id=device_id,
            triggered_by="Script Editor",
        )
        return {"run_id": run_id}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{project_id}/trend-summary")
def get_trend_summary(
    project_id: str,
    days: int = 7,
    current_user=Depends(get_current_user),
):
    """AI-generated weekly-trend summary for a project's recent runs."""
    from automation.ai.services.summary import test_summary_generator
    return test_summary_generator.generate_trend_summary(project_id, days=days)


@router.get("/{project_id}/performance-trends")
def get_performance_trends(
    project_id: str,
    limit: int = 10,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Performance scores for a project's last N runs + regression detection."""
    from automation.database.models import PerformanceSummary
    from automation.database.models import TestRun as _TestRun
    from automation.api.v1.routers.intelligence import analyze_performance_trend

    rows = (
        db.query(PerformanceSummary)
        .join(_TestRun, _TestRun.id == PerformanceSummary.run_id)
        .filter(_TestRun.project_id == project_id)
        .order_by(PerformanceSummary.created_at.desc())
        .limit(limit)
        .all()
    )
    return analyze_performance_trend(rows)
