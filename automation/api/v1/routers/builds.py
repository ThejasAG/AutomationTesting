"""Latest-build notification + one-click deploy to EVERY device.

Two endpoints back the dashboard bell:

  GET  /builds/updates   — which projects have commits waiting on the remote
  POST /builds/deploy    — pull -> build ONCE -> install on every discovered device
  GET  /builds/deploy/status — live progress of the above

Device count is never hardcoded. The install fan-out enumerates simulators at deploy
time via DeviceDiscoveryService, so 3 devices today and 5 tomorrow both work with no
code change — add a simulator and the next deploy picks it up.

The build runs ONCE per project and the resulting artifact is installed on each
device. Rebuilding per device would multiply a multi-minute xcodebuild by the fleet
size for a byte-identical artifact.
"""
import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from automation.database.config import get_db
from automation.database.models import TestProject
from automation.device_manager.service import DeviceDiscoveryService
from automation.projects.builder import AppBuilder
from automation.projects.repository import repository_manager

logger = logging.getLogger("builds")
router = APIRouter(prefix="/builds", tags=["builds"])

_devices = DeviceDiscoveryService()
_builder = AppBuilder()


def _bundle_id_for_project(project_id: str) -> Optional[str]:
    """The app's bundle id from its most recent build artifact, if there is one."""
    try:
        import os
        from automation.projects.detector import detect_project_type  # noqa: F401
        repo = repository_manager.get_repo_path(project_id)
        base = os.path.join(repo, "build", "ios", "Build", "Products")
        for root, dirs, _files in os.walk(base):
            for dname in dirs:
                if dname.endswith(".app"):
                    return _builder._bundle_id(os.path.join(root, dname))
    except Exception:
        pass
    return None


# ── update check ────────────────────────────────────────────────────────────
@router.get("/updates")
def check_updates(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Projects whose remote branch is ahead of the local checkout.

    This is what the dashboard bell polls. `has_remote_updates` does a `git fetch`
    per project, so keep the poll interval sane (the UI uses 60s).
    """
    out: List[Dict[str, Any]] = []
    for p in db.query(TestProject).filter(TestProject.status == "active").all():
        if not repository_manager.is_cloned(p.id):
            continue
        # The project's CONFIGURED branch decides what "latest build" means — not
        # whatever happens to be checked out. A PR test leaves the worktree on the
        # PR branch, and preferring that silently built and installed the wrong
        # variant (a production-configured app for a project named "staging").
        branch = p.default_branch or repository_manager.get_current_branch(p.id) or "main"
        try:
            behind = repository_manager.has_remote_updates(p.id, branch)
        except Exception as e:                      # offline / no remote — not an error
            logger.debug("update check failed for %s: %s", p.id, e)
            continue
        meta = repository_manager.get_git_metadata(p.id) or {}
        # What is installed on each simulator RIGHT NOW, so the version is visible
        # before you deploy — not only in the log of a deploy you already ran.
        installed = []
        try:
            bid = _bundle_id_for_project(p.id)
            if bid:
                for dev in _devices.discover_local_simulators(booted_only=False):
                    v = _builder.installed_version(dev.id, bid)
                    if v.get("version"):
                        installed.append({"device": dev.name, "device_id": dev.id,
                                          "version": v.get("version"), "build": v.get("build")})
        except Exception as e:
            logger.debug("installed-version scan failed for %s: %s", p.id, e)
        out.append({
            "installed": installed,
            "project_id": p.id,
            "name": p.name,
            "platform": p.platform or "ios",
            "branch": branch,
            "has_updates": bool(behind),
            "current_commit": (meta.get("commit_sha") or "")[:8],
            "author": meta.get("author") or "",
        })
    return {"projects": out, "count": sum(1 for p in out if p["has_updates"])}


# ── deploy ──────────────────────────────────────────────────────────────────
class DeployBody(BaseModel):
    project_ids: Optional[List[str]] = None      # default: every project with updates
    device_ids: Optional[List[str]] = None       # default: every discovered simulator
    include_shutdown: bool = True                # boot shut-down sims and install too
    force_build: bool = True


class _Deploy:
    """Single in-flight deploy. One at a time — a build and N installs are heavy, and
    two concurrent deploys would fight over the same simulators."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state: Dict[str, Any] = {"status": "idle", "steps": [], "results": []}

    def log(self, msg: str) -> None:
        logger.info("[deploy] %s", msg)
        self.state["steps"].append({"at": datetime.utcnow().isoformat(), "message": msg})

    def running(self) -> bool:
        return self.state.get("status") == "running"

    def start(self, body: DeployBody, projects: List[Dict[str, Any]]) -> Dict[str, Any]:
        with self.lock:
            if self.running():
                raise HTTPException(409, "A deploy is already running")
            self.state = {"status": "running", "started_at": datetime.utcnow().isoformat(),
                          "steps": [], "results": [], "projects": [p["name"] for p in projects]}
        threading.Thread(target=self._run, args=(body, projects),
                         name="build-deploy", daemon=True).start()
        return {"started": True, "projects": [p["name"] for p in projects]}

    def _target_devices(self, body: DeployBody) -> List[Any]:
        """Every simulator we should install onto — resolved NOW, not from a fixed list."""
        devs = _devices.discover_local_simulators(booted_only=not body.include_shutdown)
        if body.device_ids:
            wanted = set(body.device_ids)
            devs = [d for d in devs if d.id in wanted]
        return devs

    def _run(self, body: DeployBody, projects: List[Dict[str, Any]]) -> None:
        try:
            devices = self._target_devices(body)
            self.log(f"{len(devices)} device(s) targeted: "
                     f"{', '.join(f'{d.name} [{d.id[:8]}]' for d in devices) or 'none'}")
            if not devices:
                self.state["status"] = "failed"
                self.log("No simulators found — nothing to install onto.")
                return

            for proj in projects:
                pid, name = proj["project_id"], proj["name"]
                platform = (proj.get("platform") or "ios").lower()
                branch = proj.get("branch") or "main"

                self.log(f"{name}: pulling {branch} …")
                try:
                    # pull() rebases origin/<branch> onto the CURRENT checkout, so
                    # switch first — otherwise we rebase the target branch onto an
                    # unrelated one and build a hybrid of the two.
                    current = repository_manager.get_current_branch(pid)
                    if current != branch:
                        self.log(f"{name}: switching {current or '?'} -> {branch}")
                        repository_manager.checkout_branch(pid, branch)
                    repository_manager.pull(pid, branch)
                except Exception as e:
                    self.log(f"{name}: pull FAILED — {e}")
                    self.state["results"].append(
                        {"project": name, "stage": "pull", "ok": False, "detail": str(e)[:300]})
                    continue

                repo_path = repository_manager.get_repo_path(pid)
                self.log(f"{name}: building ({platform}) — this is the slow part …")
                built = _builder.build(repo_path, platform, force=body.force_build,
                                       device_id=devices[0].id)
                if not built.ok or not built.artifact_path:
                    detail = (getattr(built, "error", "") or "build failed")[:300]
                    self.log(f"{name}: build FAILED — {detail}")
                    self.state["results"].append(
                        {"project": name, "stage": "build", "ok": False, "detail": detail})
                    continue
                # State WHAT was built: version, build number and commit. "installed"
                # on its own asks you to trust that it was the build you expected.
                ver = _builder.app_version(built.artifact_path)
                commit = repository_manager.get_git_metadata(pid).get("commit_sha", "")[:8]
                vlabel = (f"{ver.get('version') or '?'} ({ver.get('build') or '?'})"
                          f" @ {commit or '?'}")
                self.log(f"{name}: built {ver.get('name') or ''} {vlabel}")

                # Install the SAME artifact on every device.
                for d in devices:
                    if platform == "ios":
                        ok_boot, boot_msg = _builder.ensure_ios_booted(d.id)
                        if not ok_boot:
                            self.log(f"{name} -> {d.name}: {boot_msg}")
                            self.state["results"].append(
                                {"project": name, "device": d.name, "device_id": d.id,
                                 "stage": "boot", "ok": False, "detail": boot_msg[:300]})
                            continue
                    prev = _builder.installed_version(d.id, built.bundle_id or "")
                    ok, msg = _builder.install(d.id, built.artifact_path, platform)
                    # Read it back FROM THE DEVICE after installing — the only honest
                    # confirmation that the artifact actually landed.
                    now = (_builder.installed_version(d.id, built.bundle_id or "")
                           if ok else {"version": None, "build": None})
                    was = (f"{prev.get('version')} ({prev.get('build')})"
                           if prev.get("version") else "not installed")
                    became = (f"{now.get('version')} ({now.get('build')})"
                              if now.get("version") else "?")
                    self.log(f"{name} -> {d.name} [{d.id[:8]}]: "
                             f"{'installed ' + became + ' — was ' + was if ok else 'INSTALL FAILED'}")
                    self.state["results"].append(
                        {"project": name, "device": d.name, "device_id": d.id,
                         "stage": "install", "ok": bool(ok), "detail": str(msg)[:300],
                         "bundle_id": built.bundle_id,
                         "version": now.get("version"), "build": now.get("build"),
                         "previous_version": prev.get("version"),
                         "previous_build": prev.get("build"),
                         "commit": commit,
                         # The artifact said X; the device now says Y. If they differ the
                         # install did not take, however green the step looked.
                         "version_matches": bool(ok and now.get("version") == ver.get("version")
                                                 and now.get("build") == ver.get("build"))})

            failures = [r for r in self.state["results"] if not r["ok"]]
            self.state["status"] = "completed" if not failures else "completed_with_errors"
            self.log(f"Done — {len(self.state['results']) - len(failures)} ok, "
                     f"{len(failures)} failed.")
        except Exception as e:                      # never leave the UI spinning
            logger.exception("deploy crashed")
            self.state["status"] = "failed"
            self.log(f"Deploy crashed: {e}")
        finally:
            self.state["finished_at"] = datetime.utcnow().isoformat()


_deploy = _Deploy()


@router.post("/deploy", status_code=202)
def deploy_latest(body: DeployBody = DeployBody(), db: Session = Depends(get_db)):
    """Pull the latest code, build once per project, install on every device.

    Returns immediately (202) — a cold xcodebuild takes minutes. Poll
    ``GET /builds/deploy/status``.
    """
    known = {p["project_id"]: p for p in check_updates(db)["projects"]}
    if body.project_ids:
        chosen = [known[i] for i in body.project_ids if i in known]
        missing = [i for i in body.project_ids if i not in known]
        if missing:
            raise HTTPException(404, f"Unknown or un-cloned project(s): {missing}")
    else:
        chosen = [p for p in known.values() if p["has_updates"]]
    if not chosen:
        raise HTTPException(400, "Nothing to deploy — no project has pending updates.")
    return _deploy.start(body, chosen)


@router.get("/deploy/status")
def deploy_status() -> Dict[str, Any]:
    return _deploy.state
