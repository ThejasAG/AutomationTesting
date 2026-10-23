"""Remove a project and every trace of it, except its test history.

`delete_local_repo()` removes one directory and `DELETE /projects/{id}` removes one
row. Everything else a project accumulates is left behind, and the live database
proves it: rows in `ai_recommendations` already point at projects that no longer
exist, and `repos/` holds clones with no project row at all.

WHAT THIS KEEPS, DELIBERATELY
    Test history — `test_runs` and everything hanging off a run (scenario results,
    evidence bundles, RCA reports, performance metrics). Runs are DETACHED, by
    nulling `test_runs.project_id`, not deleted. A run records that something was
    tested and what happened; that stays true after the project is gone. The
    reports on disk are kept for the same reason, so a retained run's artefacts do
    not turn into dead links.

    `test_runs.project_id` is nullable (models.py:137) so detaching is a supported
    state, not a hack. Audit logs are kept too — an audit trail you can erase by
    deleting the thing it audits is not an audit trail.

WHAT IT REMOVES
    The clone, the visual-regression baselines, project-scoped config and secrets,
    saved scenarios, tickets, AI recommendations, and the learned-locator entry —
    everything that only makes sense while the project exists.

ORDERING IS LOAD-BEARING
    `app_bundle_id` lives on the project row, and it is the key into the locator
    store. Read it BEFORE the row is deleted or the store can never be cleaned.
    Phase 0 captures it, along with the run ids used to detach history.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASELINE_ROOT = os.path.join(_ROOT, "baselines")
LEARNED_LOCATORS = os.path.join(_ROOT, "automation", "knowledge", "learned_locators.json")

# Tables carrying a project_id that dies with the project. `test_runs` is NOT here:
# it is detached instead, which is the whole point of keeping history.
_PROJECT_SCOPED = ("ai_recommendations", "project_settings", "saved_scenarios", "tickets")


@dataclass
class PurgePlan:
    """What a purge would do. Rendered for --dry-run and returned by the API."""
    project_id: str
    name: str = ""
    bundle_id: Optional[str] = None
    exists_in_db: bool = False
    repo_path: Optional[str] = None
    repo_bytes: int = 0
    baseline_path: Optional[str] = None
    runs_detached: int = 0
    rows: Dict[str, int] = field(default_factory=dict)
    locator_key: Optional[str] = None
    locator_kept_because: str = ""
    # Simulators/devices still carrying this project's app. Purging the row and the
    # clone but leaving the app installed is how a "deleted" project keeps driving
    # runs: the binary stays, and a debug build with no main.jsbundle then serves
    # whatever Metro is up, which need not match anything left on disk.
    uninstall_from: List[str] = field(default_factory=list)
    uninstalled: List[str] = field(default_factory=list)
    app_kept_because: str = ""
    warnings: List[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.exists_in_db or self.repo_path or self.baseline_path
                    or self.rows or self.locator_key)

    def render(self) -> str:
        out = [f"project {self.project_id}" + (f"  ({self.name})" if self.name else "")]
        if not self.exists_in_db:
            out.append("  ! no database row — orphaned clone or already deleted")
        if self.repo_path:
            out.append(f"  repo       {self.repo_path}  ({_human(self.repo_bytes)})")
        if self.baseline_path:
            out.append(f"  baselines  {self.baseline_path}")
        for table, n in sorted(self.rows.items()):
            if n:
                out.append(f"  delete     {n:>5} row(s) from {table}")
        if self.runs_detached:
            out.append(f"  KEEP       {self.runs_detached:>5} test run(s) — detached, history retained")
        if self.locator_key:
            out.append(f"  locators   drop {self.locator_key!r} from learned_locators.json")
        elif self.locator_kept_because:
            out.append(f"  locators   kept — {self.locator_kept_because}")
        for device in self.uninstall_from:
            out.append(f"  uninstall  {self.bundle_id} from {device}")
        if self.app_kept_because:
            out.append(f"  app        kept — {self.app_kept_because}")
        for w in self.warnings:
            out.append(f"  ! {w}")
        if self.is_empty:
            out.append("  nothing to remove")
        return "\n".join(out)


def _human(n: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}G"


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for f in files:
            fp = os.path.join(root, f)
            try:
                # lstat, not getsize: a broken symlink raises on stat, and a live
                # one would otherwise count its target's bytes against this repo.
                total += os.lstat(fp).st_size
            except OSError:
                pass
    return total


def _repos_base() -> str:
    """Where clones actually live, as the rest of the platform sees it.

    RepositoryManager resolves a RELATIVE "repos" against the process's working
    directory (repository.py:24), so a backend started from somewhere else keeps
    its clones somewhere else. Deriving the path independently here would then
    have the purge looking in the wrong directory and silently reporting nothing
    to remove — so ask the manager rather than recomputing it.
    """
    try:
        from automation.projects.repository import repository_manager
        return repository_manager.base_dir
    except Exception:
        return os.path.join(_ROOT, "repos")


def _table_exists(db: Session, table: str) -> bool:
    from sqlalchemy import text
    try:
        return bool(db.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:t"),
            {"t": table}).first())
    except Exception:
        return True          # not SQLite; assume the model's table is there


def _count(db: Session, table: str, where: str, params: dict) -> int:
    from sqlalchemy import text
    try:
        row = db.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where}"), params).first()
        return int(row[0]) if row else 0
    except Exception as e:
        logger.warning("count on %s failed: %s", table, e)
        return 0


def _bundle_still_in_use(db: Session, bundle_id: str, project_id: str) -> Optional[str]:
    """Why *bundle_id* must survive this purge, or None if it is free to drop.

    Bundle ids are NOT unique to a project — this fleet runs the same business app
    under two projects. Dropping the key on one purge would blind the other, so the
    store is only touched when nothing else claims the id.
    """
    from sqlalchemy import text
    try:
        row = db.execute(text(
            "SELECT name FROM test_projects WHERE app_bundle_id = :b AND id != :p LIMIT 1"),
            {"b": bundle_id, "p": project_id}).first()
        if row:
            return f"project {row[0]!r} also uses {bundle_id}"
        if _table_exists(db, "saved_scenarios"):
            row = db.execute(text(
                "SELECT id FROM saved_scenarios WHERE bundle_id = :b AND "
                "(project_id IS NULL OR project_id != :p) LIMIT 1"),
                {"b": bundle_id, "p": project_id}).first()
            if row:
                return f"a saved scenario outside this project uses {bundle_id}"
    except Exception as e:
        return f"could not confirm {bundle_id} is unused ({e})"
    return None


def _locator_key_for(bundle_id: str, other_bundle_ids: Sequence[str] = ()) -> Optional[str]:
    """The key in the locator store for *bundle_id*, or None.

    Matching is lenient because the store is written with the id the RUNNING app
    reported, which can carry a build suffix the project row does not
    ('...vyabusinessipad' in the DB vs '...vyabusinessipadstaging' on disk), so an
    exact match would leave the entry behind.

    But leniency CUTS BOTH WAYS, and that is the dangerous half. 'vyaconsumer' is a
    prefix of 'vyaconsumerstaging', so purging the prod Consumer project would
    fuzzily claim the STAGING key that a different, live project depends on —
    measured on this database, where both projects exist. So a lenient hit is
    refused whenever some other surviving project's bundle id matches that key
    better (or equally well). Only an unambiguous owner may drop a key.
    """
    try:
        with open(LEARNED_LOCATORS, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    keys = list((data.get("apps") if isinstance(data.get("apps"), dict) else data).keys())
    if bundle_id in keys:
        return bundle_id                       # exact: unambiguous, always safe
    others = [b for b in other_bundle_ids if b]
    for k in keys:
        if not (k.startswith(bundle_id) or bundle_id.startswith(k)):
            continue
        # Someone else owns this key outright, or matches it as closely as we do.
        if any(b == k for b in others):
            continue
        if any((k.startswith(b) or b.startswith(k)) and len(b) >= len(bundle_id)
               for b in others):
            continue
        return k
    return None


def plan_purge(db: Session, project_id: str, repos_base: Optional[str] = None) -> PurgePlan:
    """Everything the purge would touch. Reads only — safe to call at any time."""
    from sqlalchemy import text

    plan = PurgePlan(project_id=project_id)

    row = db.execute(text(
        "SELECT name, app_bundle_id FROM test_projects WHERE id = :p"),
        {"p": project_id}).first()
    if row:
        plan.exists_in_db = True
        plan.name = row[0] or ""
        plan.bundle_id = row[1]
    else:
        plan.warnings.append("no project row; disk artefacts will still be removed")

    base = repos_base or _repos_base()
    repo = os.path.join(base, project_id)
    if os.path.isdir(repo):
        plan.repo_path = repo
        plan.repo_bytes = _dir_size(repo)

    baseline = os.path.join(BASELINE_ROOT, project_id)
    if os.path.isdir(baseline):
        plan.baseline_path = baseline

    for table in _PROJECT_SCOPED:
        if _table_exists(db, table):
            n = _count(db, table, "project_id = :p", {"p": project_id})
            if n:
                plan.rows[table] = n

    if _table_exists(db, "test_runs"):
        plan.runs_detached = _count(db, "test_runs", "project_id = :p", {"p": project_id})

    if plan.bundle_id:
        blocked = _bundle_still_in_use(db, plan.bundle_id, project_id)
        if blocked:
            plan.locator_kept_because = blocked
        else:
            # Every bundle id that OUTLIVES this purge, so a lenient key match
            # cannot steal an entry another project is still learning against.
            survivors = [r[0] for r in db.execute(text(
                "SELECT DISTINCT app_bundle_id FROM test_projects "
                "WHERE app_bundle_id IS NOT NULL AND id != :p"), {"p": project_id}).fetchall()]
            plan.locator_key = _locator_key_for(plan.bundle_id, survivors)
            if not plan.locator_key:
                plan.locator_kept_because = (
                    f"no entry unambiguously owned by {plan.bundle_id}")

        # The installed app goes too, on the same condition as the locator entry:
        # a bundle id another project still claims must survive the purge.
        if blocked:
            plan.app_kept_because = blocked
        else:
            plan.uninstall_from = _devices_with_app(plan.bundle_id)
    return plan


def _devices_with_app(bundle_id: str) -> List[str]:
    """Booted simulators carrying *bundle_id*.

    Booted only, deliberately: `simctl uninstall` boots a shut-down simulator to
    run, which would turn a project delete into a fleet-wide wake-up. A shut-down
    simulator's copy is replaced by the next prepare, which uninstalls first.

    Apple's own apps are never touched. The demo project drives Settings
    (com.apple.Preferences) precisely because it ships with the simulator; deleting
    that project must not strip the OS of an app the platform cannot reinstall.
    """
    if bundle_id.startswith("com.apple."):
        return []
    try:
        from automation.projects.builder import app_builder
        ok, out = _run_simctl(["list", "devices", "booted"])
        if not ok:
            return []
        udids = re.findall(r"\(([0-9A-Fa-f-]{36})\)\s*\(Booted\)", out or "")
        return [u for u in udids if app_builder.is_installed(u, bundle_id)]
    except Exception as e:
        logger.debug("_devices_with_app(%s): %s", bundle_id, e)
        return []


def _run_simctl(args: List[str]) -> tuple:
    """simctl, or (False, '') where there is no Xcode — Linux CI, for one."""
    try:
        p = subprocess.run(["xcrun", "simctl", *args], capture_output=True,
                           text=True, timeout=60)
        return p.returncode == 0, p.stdout
    except Exception:
        return False, ""


def purge_project(db: Session, project_id: str, repos_base: Optional[str] = None,
                  dry_run: bool = False) -> PurgePlan:
    """Execute the plan. Returns what was done (or would be, when *dry_run*).

    The database is committed once, at the end, so a failure part-way leaves the
    project intact rather than half-deleted. Disk removal happens after that commit:
    a leftover directory is recoverable noise, whereas rows pointing at files that
    are already gone are not.
    """
    from sqlalchemy import text

    plan = plan_purge(db, project_id, repos_base)
    if dry_run:
        return plan

    # ── DB ────────────────────────────────────────────────────────────────────
    # Detach history FIRST. If anything later fails, the rollback restores the link.
    if plan.runs_detached:
        db.execute(text("UPDATE test_runs SET project_id = NULL WHERE project_id = :p"),
                   {"p": project_id})
    for table in plan.rows:
        db.execute(text(f"DELETE FROM {table} WHERE project_id = :p"), {"p": project_id})
    if plan.exists_in_db:
        db.execute(text("DELETE FROM test_projects WHERE id = :p"), {"p": project_id})
    db.commit()

    # ── disk ──────────────────────────────────────────────────────────────────
    for path in (plan.repo_path, plan.baseline_path):
        if path and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            if os.path.exists(path):
                plan.warnings.append(f"could not fully remove {path}")
            else:
                logger.info("purged %s", path)

    # ── devices ───────────────────────────────────────────────────────────────
    # After the commit, like the disk removal: a leftover app is recoverable noise,
    # whereas uninstalling for a purge that then fails is not.
    if plan.uninstall_from:
        from automation.projects.builder import app_builder
        for device in plan.uninstall_from:
            ok, msg = app_builder.uninstall(device, plan.bundle_id, "ios")
            if ok:
                plan.uninstalled.append(device)
                logger.info("purge: uninstalled %s from %s", plan.bundle_id, device)
            else:
                plan.warnings.append(
                    f"could not uninstall {plan.bundle_id} from {device}: {msg}")

    # ── knowledge ─────────────────────────────────────────────────────────────
    if plan.locator_key:
        try:
            with open(LEARNED_LOCATORS, encoding="utf-8") as fh:
                data = json.load(fh)
            target = data["apps"] if isinstance(data.get("apps"), dict) else data
            target.pop(plan.locator_key, None)
            with open(LEARNED_LOCATORS, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        except Exception as e:
            plan.warnings.append(f"could not update learned_locators.json: {e}")
    return plan


def find_orphans(db: Session, repos_base: Optional[str] = None) -> Dict[str, List[str]]:
    """Traces left by earlier deletions, which never cleaned up after themselves.

    'clones' are directories under repos/ with no project row. 'baselines' the same
    under baselines/. Neither is reachable from the dashboard, so without this they
    are invisible until someone notices the disk is full.
    """
    from sqlalchemy import text
    ids = {r[0] for r in db.execute(text("SELECT id FROM test_projects")).fetchall()}
    out: Dict[str, List[str]] = {"clones": [], "baselines": []}

    base = repos_base or _repos_base()
    for label, root in (("clones", base), ("baselines", BASELINE_ROOT)):
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            path = os.path.join(root, entry)
            if entry.startswith(".") or not os.path.isdir(path):
                continue
            if entry not in ids:
                out[label].append(entry)
    return out
