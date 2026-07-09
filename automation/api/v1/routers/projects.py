from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from typing import List, Dict, Any
import uuid
import os
import pathlib
import subprocess

from automation.database import database
from automation.database.config import get_db
from automation.database.models import TestProject
from automation.projects.repository import repository_manager
from automation.auth.security import get_current_user
from automation.runner.service import runner_service
from pydantic import BaseModel

router = APIRouter(prefix="/projects", tags=["projects"])

# ── Extensions and directories that the editor is allowed to open ────────────
_ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {".py", ".yaml", ".yml", ".json", ".txt", ".md"}
)
_SKIP_DIRS: frozenset[str] = frozenset(
    {"__pycache__", ".git", ".venv", "node_modules", ".pytest_cache", ".mypy_cache"}
)


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


# ── Existing endpoints ────────────────────────────────────────────────────────

class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    git_url: str
    default_branch: str = "main"

@router.get("/")
def list_projects(db: Session = Depends(get_db)):
    projects = database.get_test_projects(db)
    result = []
    for p in projects:
        health = repository_manager.get_health(p.id)
        result.append({
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "git_url": p.git_url,
            "default_branch": p.default_branch,
            "status": p.status,
            "health": health
        })
    return {"projects": result}

@router.post("/")
def create_project(project: ProjectCreate, db: Session = Depends(get_db)):
    project_id = str(uuid.uuid4())
    data = {
        "id": project_id,
        "name": project.name,
        "description": project.description,
        "git_url": project.git_url,
        "default_branch": project.default_branch,
        "status": "active"
    }
    database.insert_test_project(db, data)
    
    # Optionally trigger clone immediately
    repository_manager.clone_or_pull(project_id, project.git_url, project.default_branch)
    
    return {"status": "success", "id": project_id}


# ── File browser endpoints ────────────────────────────────────────────────────

@router.get(
    "/{project_id}/files",
    summary="List all editable files in a project repository",
    dependencies=[Depends(get_current_user)],
)
def list_project_files(
    project_id: str,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Walk the cloned repository and return all files with allowed extensions.

    Hidden files and build/cache directories are excluded automatically.
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
    _get_project_or_404(project_id, db)

    try:
        run_id = runner_service.execute_project(
            project_id=project_id,
            device_id=body.device_id,
            triggered_by="Script Editor",
        )
        return {"run_id": run_id}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
