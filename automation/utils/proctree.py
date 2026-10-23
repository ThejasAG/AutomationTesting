"""Track the processes THIS platform spawns, so teardown can take the whole tree.

Appium is launched through npx, which spawns node, which spawns xcodebuild, which
spawns the WDA test runner. `Popen.terminate()` signals only the npx parent — every
descendant survives as an orphan and keeps holding the simulator and a CPU core.

The fix is a dedicated process group per spawn (``start_new_session=True``) plus a
disk-backed registry of what we started, so teardown can `killpg` the group and
nothing else. The registry is the safety boundary: we only ever signal a pid we
recorded, never one found by scanning for a matching command name.
"""

import errno
import json
import logging
import os
import signal
import subprocess
import time
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Anchored to the project root, NOT the cwd. The registry's whole job is to be
# found again after a restart, and a cwd-relative path quietly fails at exactly
# that: an agent relaunched from another directory would read an empty registry
# and see no orphans. PROC_REGISTRY_PATH overrides it (tests, alternate layouts).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REGISTRY_PATH = os.getenv("PROC_REGISTRY_PATH") or os.path.join(_PROJECT_ROOT, "logs", "proc_registry.json")

# Audit trail, one JSON object per line. Defaults to sitting beside the registry
# so pointing PROC_REGISTRY_PATH somewhere else (a test) moves both together and
# nothing leaks into the real logs directory.
AUDIT_PATH = os.getenv("PROC_AUDIT_PATH") or os.path.join(os.path.dirname(REGISTRY_PATH), "proc_sweep_audit.jsonl")

# ── Orphan sweep configuration ───────────────────────────────────────────────
# Reaping requires THREE independent conditions, and every default is the safe
# one. Two separate opt-ins mean neither can be flipped by accident alone, and
# the dry-run override wins over both — so a single env var can always disarm a
# machine without touching the others.
#
#   PROC_SWEEP_DRY_RUN=true   -> never signals, whatever else is set
#   PROC_SWEEP_ENABLED=false  -> never signals
#   PROC_SWEEP_ALLOW_REAP=false -> never signals
#   all three aligned         -> reaping allowed
def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes"}


PROC_SWEEP_ENABLED = _flag("PROC_SWEEP_ENABLED", "false")
PROC_SWEEP_ALLOW_REAP = _flag("PROC_SWEEP_ALLOW_REAP", "false")
PROC_SWEEP_DRY_RUN = _flag("PROC_SWEEP_DRY_RUN", "true")

# Kinds that are never candidates, whatever else the entry says. Metro is shared
# across jobs and long-lived by design: killing it breaks every other run on the
# machine, so it is excluded structurally rather than by policy at the call site.
NEVER_REAP = frozenset({"metro"})

# An entry younger than this is left alone — it is far more likely to belong to a
# job still starting up than to be an orphan.
SWEEP_MIN_AGE_SEC = float(os.getenv("PROC_SWEEP_MIN_AGE_SEC", "900"))

# TestRun.job_state values that mean the job is still in flight. Mirrors the
# backend's own list (automation/api/v1/routers/jobs.py) minus the terminal ones.
ACTIVE_JOB_STATES = frozenset({
    "queued", "assigned", "downloading", "preparing", "running", "collecting_evidence",
})


class RegistryError(Exception):
    """Registry is unreadable or corrupt. Callers must NOT kill anything."""


def _proc_identity(pid: int) -> str:
    """Start time of *pid*, or "" if it no longer exists.

    This is the PID-reuse guard: a recycled pid gets a different start time, so a
    mismatch means the process we recorded is already gone and this pid belongs to
    somebody else. Uses ps rather than psutil — psutil is not a dependency here.
    """
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _group_members(pgid: int) -> List[int]:
    """Pids still RUNNING in process group *pgid*.

    Zombies are excluded: a process that has exited but whose parent has not yet
    wait()ed on it still appears in `ps`, and signalling it fails with EPERM. It
    holds no resources, so counting it as a survivor would make every clean
    teardown look like a failure.
    """
    try:
        out = subprocess.run(
            ["ps", "-g", str(pgid), "-o", "pid=,state="],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return []
    if out.returncode != 0:
        return []
    members = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and not parts[1].startswith("Z"):
            members.append(int(parts[0]))
    return members


def _descendant_groups(pid: int) -> List[tuple]:
    """(leader_pid, identity) for every process group a descendant of *pid* leads.

    Appium does not keep its whole tree in one group: appium-webdriveragent spawns
    xcodebuild with `detached: true` (xcodebuild.js), i.e. setsid(), so xcodebuild
    leads its OWN group and killpg(appium_group) never reaches it — that is exactly
    the WDA/xcodebuild leak. So we follow parentage down from the pid we spawned
    and collect the breakaway groups too.

    This walks the process table but selects ONLY descendants of a pid we recorded.
    Nothing is chosen by matching a command name.
    """
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,pgid="],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return []
    if out.returncode != 0:
        return []

    children: Dict[int, List[int]] = {}
    pgid_of: Dict[int, int] = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3 or not all(x.lstrip("-").isdigit() for x in parts[:3]):
            continue
        cpid, ppid, cpgid = (int(x) for x in parts[:3])
        children.setdefault(ppid, []).append(cpid)
        pgid_of[cpid] = cpgid

    own_pgid = pgid_of.get(pid)
    seen, queue, groups = set(), list(children.get(pid, [])), []
    while queue:
        cur = queue.pop()
        if cur in seen:
            continue
        seen.add(cur)
        queue.extend(children.get(cur, []))
        # A descendant that leads its own group broke away from ours.
        if pgid_of.get(cur) == cur and cur != own_pgid:
            groups.append((cur, _proc_identity(cur)))
    return groups


def _load() -> Dict[str, dict]:
    """Registry contents. Raises RegistryError if it exists but cannot be parsed."""
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        raise RegistryError(f"{REGISTRY_PATH}: {e}") from e
    if not isinstance(data, dict):
        raise RegistryError(f"{REGISTRY_PATH}: expected an object, got {type(data).__name__}")
    return data


def _save(entries: Dict[str, dict]) -> None:
    """Write the registry atomically, so a crash mid-write cannot corrupt it."""
    os.makedirs(os.path.dirname(REGISTRY_PATH), exist_ok=True)
    tmp = f"{REGISTRY_PATH}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, REGISTRY_PATH)


# ponytail: read-modify-write under a coarse file lock. Registry writes happen a
# handful of times per job, so contention is not worth a finer scheme.
def _update(mutate) -> None:
    import fcntl

    os.makedirs(os.path.dirname(REGISTRY_PATH), exist_ok=True)
    lock_path = f"{REGISTRY_PATH}.lock"
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            entries = _load()
            mutate(entries)
            _save(entries)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def register(pid: int, *, job_id: str, kind: str, cmd: List[str]) -> None:
    """Record an already-spawned process group leader."""
    entry = {
        "pid": pid,
        "pgid": os.getpgid(pid),
        "job_id": job_id,
        "kind": kind,
        "identity": _proc_identity(pid),
        "cmd": list(cmd),
        "recorded_at": time.time(),
    }
    _update(lambda entries: entries.__setitem__(str(pid), entry))


def unregister(pid: int) -> None:
    _update(lambda entries: entries.pop(str(pid), None))


def spawn_tracked(cmd, *, job_id: str, kind: str, **popen_kwargs) -> subprocess.Popen:
    """Popen(cmd) in its own session, recorded in the registry.

    ``start_new_session=True`` makes the child a session/group leader, so its pid is
    also its pgid and every descendant inherits that group — which is what lets
    reap_pid() take the whole tree down at teardown.
    """
    popen_kwargs.pop("start_new_session", None)
    proc = subprocess.Popen(cmd, start_new_session=True, **popen_kwargs)
    try:
        register(proc.pid, job_id=job_id, kind=kind, cmd=list(cmd))
    except Exception as e:
        # An unrecorded process is a future orphan, but killing a just-started
        # Appium over a bookkeeping failure is worse. Warn loudly and carry on.
        logger.error(f"proctree: could not register pid {proc.pid} ({kind}/{job_id}): {e}")
    return proc


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM


def reap_pid(pid: int, *, timeout: float = 10.0) -> bool:
    """SIGTERM then SIGKILL the process group we recorded for *pid*.

    Returns True if the group is gone afterwards. Refuses (returns False, kills
    nothing) when the registry is unreadable, when the pid was not spawned by us,
    or when the recorded start time no longer matches — that pid has been recycled.
    """
    try:
        entries = _load()
    except RegistryError as e:
        logger.error(f"proctree: registry unreadable, refusing to kill pid {pid}: {e}")
        return False

    entry = entries.get(str(pid))
    if entry is None:
        logger.warning(f"proctree: pid {pid} is not one of ours — not killing it")
        return False

    pgid = entry.get("pgid")
    if not isinstance(pgid, int) or pgid <= 1:
        logger.error(f"proctree: entry for pid {pid} has no usable pgid — refusing")
        return False

    leader_alive = _alive(pid)
    if leader_alive:
        if _proc_identity(pid) != entry.get("identity"):
            logger.warning(f"proctree: pid {pid} was recycled — not killing it")
            unregister(pid)
            return False
        try:
            if os.getpgid(pid) != pgid:
                logger.warning(f"proctree: pid {pid} left group {pgid} — not killing it")
                unregister(pid)
                return False
        except OSError:
            pass
    elif _proc_identity(pgid):
        # Leader gone but something still answers to that pid: the number was
        # reused, so the group it now leads is not ours. Descendants of ours may
        # leak, but signalling a stranger's group is the worse outcome.
        logger.warning(f"proctree: pgid {pgid} reused by another process — not killing it")
        unregister(pid)
        return False

    def _signal(sig) -> None:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass
        except OSError as e:
            logger.warning(f"proctree: killpg({pgid}, {sig}) failed: {e}")

    # Snapshot breakaway groups BEFORE signalling — once the leader dies we lose
    # the parentage that proves those processes were ours.
    breakaway = _descendant_groups(pid) if leader_alive else []

    logger.info(f"proctree: REAP_SIGTERM group {pgid} ({entry.get('kind')}/{entry.get('job_id')})")
    _signal(signal.SIGTERM)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _group_members(pgid):
            break
        time.sleep(0.2)

    survivors = _group_members(pgid)
    if survivors:
        logger.warning(f"proctree: REAP_SIGKILL group {pgid} still has {survivors} after SIGTERM")
        _signal(signal.SIGKILL)
        time.sleep(0.5)
        survivors = _group_members(pgid)

    # Detached descendants (xcodebuild + the WDA runner under it) outlive the
    # group kill. Each is re-checked against the start time recorded a moment ago,
    # so a pid recycled in the meantime is left alone.
    for leader, identity in breakaway:
        if not _group_members(leader):
            continue
        if _proc_identity(leader) != identity:
            logger.warning(f"proctree: descendant group {leader} was recycled — leaving it")
            continue
        logger.info(f"proctree: terminating detached descendant group {leader}")
        try:
            os.killpg(leader, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            continue
        sub_deadline = time.time() + timeout
        while time.time() < sub_deadline and _group_members(leader):
            time.sleep(0.2)
        if _group_members(leader) and _proc_identity(leader) == identity:
            try:
                os.killpg(leader, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        if _group_members(leader):
            logger.error(f"proctree: descendant group {leader} survived teardown")
            survivors = survivors or []
            survivors.append(leader)

    if survivors:
        # Keep the registry entry: a group we could not kill must stay visible to
        # the next sweep. Forgetting it here would turn a failure into an
        # invisible orphan — the exact thing the registry exists to prevent.
        logger.error(f"proctree: REAP_FAILED group {pgid} survived teardown: {survivors}")
        return False

    logger.info(f"proctree: REAP_VERIFIED group {pgid} is gone")
    unregister(pid)
    return True


def reap_job(job_id: str, *, timeout: float = 10.0) -> int:
    """Reap every tracked group belonging to *job_id*. Returns how many were reaped."""
    try:
        entries = _load()
    except RegistryError as e:
        logger.error(f"proctree: registry unreadable, refusing to reap job {job_id}: {e}")
        return 0
    reaped = 0
    for pid_s, entry in list(entries.items()):
        if entry.get("job_id") != job_id:
            continue
        try:
            if reap_pid(int(pid_s), timeout=timeout):
                reaped += 1
        except Exception as e:
            logger.warning(f"proctree: reap of pid {pid_s} failed: {e}")
    return reaped


# ── Orphan sweep (OBSERVATIONAL — sends no signals) ──────────────────────────

class SweepUnavailable(Exception):
    """Active-job state could not be established. Nothing may be classified reapable."""


# How this process learns which jobs are still in flight. The AGENT registers a
# client that asks the backend over its authenticated session (Phase 4F.7A);
# injected rather than imported so this module reaches neither the database nor
# an HTTP stack, and so it cannot acquire a second way of authenticating.
#
# Nothing registered means we cannot ask, which is "unknown" — never "nothing is
# running". Running this module's CLI directly therefore classifies nothing as
# reapable, which is the safe direction.
_active_jobs_source = None


def set_active_jobs_source(source) -> None:
    """Register a callable returning the ids of jobs the backend still has in flight."""
    global _active_jobs_source
    _active_jobs_source = source


def active_job_ids() -> set:
    """Job ids the backend considers still in flight.

    The backend's TestRun.job_state column is the authority — not this agent's
    memory of what it started, which is empty after a restart, exactly when the
    sweep runs.

    Raises SweepUnavailable if the backend cannot be reached. That is the whole
    point: an unreachable backend means "unknown", never "nothing is running".
    """
    if _active_jobs_source is None:
        raise SweepUnavailable("no active-job source registered")
    try:
        ids = _active_jobs_source()
    except SweepUnavailable:
        raise
    except Exception as e:
        raise SweepUnavailable(f"backend job state unreadable: {e}") from e
    if isinstance(ids, (str, bytes)) or not isinstance(ids, (set, frozenset, list, tuple)):
        raise SweepUnavailable(
            f"backend job state malformed: expected a collection, got {type(ids).__name__}")
    return {str(i) for i in ids}


def _classify(entry: dict, active: set, now: float) -> tuple:
    """(verdict, reason) for one registry entry. Pure inspection — no signals."""
    kind = entry.get("kind")
    if kind in NEVER_REAP:
        return "NEVER_REAP", f"kind {kind!r} is excluded from reaping"

    pid = entry.get("pid")
    if not isinstance(pid, int):
        return "DO_NOT_REAP", "entry has no usable pid"

    job_id = entry.get("job_id")
    if job_id in active:
        return "DO_NOT_REAP", f"job {job_id} is still active"

    if not _alive(pid):
        return "ALREADY_GONE", "process no longer exists — nothing to reap"

    if _proc_identity(pid) != entry.get("identity"):
        return "DO_NOT_REAP", "IDENTITY_MISMATCH — pid was recycled by another process"

    # Positive association with the recorded platform-owned entry: the live
    # process must still lead the group we recorded for it.
    try:
        if os.getpgid(pid) != entry.get("pgid"):
            return "DO_NOT_REAP", "GROUP_MISMATCH — process left the group we recorded"
    except OSError as e:
        return "DO_NOT_REAP", f"process group unreadable: {e}"

    age = now - float(entry.get("recorded_at") or 0)
    if age < SWEEP_MIN_AGE_SEC:
        return "TOO_YOUNG", f"recorded {age:.0f}s ago, minimum is {SWEEP_MIN_AGE_SEC:.0f}s"

    return "WOULD_REAP", f"orphan of inactive job {job_id}, {age:.0f}s old"


def reaping_allowed() -> tuple:
    """(allowed, reason). Reaping needs all three switches aligned; default is no.

    Read at call time, not import time, so a test or an operator changing the
    module attributes takes effect without a reimport.
    """
    if PROC_SWEEP_DRY_RUN:
        return False, "PROC_SWEEP_DRY_RUN=true overrides every other switch"
    if not PROC_SWEEP_ENABLED:
        return False, "PROC_SWEEP_ENABLED=false"
    if not PROC_SWEEP_ALLOW_REAP:
        return False, "PROC_SWEEP_ALLOW_REAP=false"
    return True, "PROC_SWEEP_ENABLED and PROC_SWEEP_ALLOW_REAP set, dry-run off"


def _audit(row: dict, action: str, extra: str = "") -> None:
    """One structured record per entry considered — to the log and to AUDIT_PATH.

    The file is what makes an observation *period* analysable: a single sweep's
    return value is a snapshot, and the question this phase needs answered ("what
    would this have killed over a week?") spans many agent restarts.
    """
    ts = time.time()
    logger.info(
        f"proctree audit: ts={ts:.3f} job={row['job_id']} pid={row['pid']} "
        f"pgid={row['pgid']} kind={row['kind']} verdict={row['verdict']} "
        f"identity_match={row['identity_match']} job_active={row['job_active']} "
        f"action={action}" + (f" {extra}" if extra else "")
    )
    record = {
        "ts": ts, "pid": row["pid"], "pgid": row["pgid"], "job_id": row["job_id"],
        "kind": row["kind"], "verdict": row["verdict"], "action": action,
        "identity_match": row["identity_match"], "job_active": row["job_active"],
        "age_sec": row.get("age_sec"), "recorded_at": row.get("recorded_at"),
        "reason": extra or row.get("reason"),
    }
    try:
        os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
        with open(AUDIT_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        # Observability must never be able to break a sweep.
        logger.warning(f"proctree: could not append audit record: {e}")


def sweep_orphans(*, dry_run: Optional[bool] = None, active: Optional[set] = None,
                  now: Optional[float] = None, timeout: float = 10.0) -> dict:
    """Classify every registry entry, and reap the eligible ones IF armed.

    Reaping happens only when reaping_allowed() says so — three switches, all
    defaulting to safe. ``dry_run=True`` forces classification only; ``dry_run``
    left as None means "whatever the switches say". Passing ``dry_run=False``
    does NOT arm anything: an explicit argument can disarm the sweep but never
    arm it, so no caller can bypass the configuration.

    Every kill goes through reap_pid() — there is no second killing
    implementation here. Before it is called, identity is validated a SECOND time
    (classification happens earlier, and a pid can in theory be recycled in the
    gap); reap_pid then validates a third time immediately before signalling.

    Only entries THIS platform recorded are considered. Nothing is discovered by
    scanning the machine for appium/xcodebuild/node by name — an orphan we did
    not record stays invisible to the sweep, which is the intended safe default.
    """
    allowed, gate_reason = reaping_allowed()
    if dry_run is True:
        allowed, gate_reason = False, "caller requested dry_run=True"

    report = {
        "dry_run": not allowed,
        "reap_armed": allowed,
        "gate_reason": gate_reason,
        "sweep_enabled": PROC_SWEEP_ENABLED,
        "allow_reap": PROC_SWEEP_ALLOW_REAP,
        "dry_run_flag": PROC_SWEEP_DRY_RUN,
        "min_age_sec": SWEEP_MIN_AGE_SEC,
        "available": True,
        "unavailable_reason": None,
        "entries": [],
        "would_reap": [],
        "reaped": [],
        "failed": [],
        "signals_sent": 0,
    }

    try:
        entries = _load()
    except RegistryError as e:
        report["available"] = False
        report["unavailable_reason"] = f"registry unreadable: {e}"
        logger.error(f"proctree sweep: {report['unavailable_reason']} — classifying nothing")
        return report

    if active is None:
        try:
            active = active_job_ids()
        except SweepUnavailable as e:
            report["available"] = False
            report["unavailable_reason"] = str(e)
            active = None
            logger.warning(f"proctree sweep: {e} — no entry can be considered reapable")

    now = time.time() if now is None else now
    for pid_s, entry in sorted(entries.items()):
        if active is None:
            verdict, reason = "UNKNOWN", "active-job state unavailable — failing closed"
        else:
            verdict, reason = _classify(entry, active, now)
        row = {
            "pid": entry.get("pid"),
            "pgid": entry.get("pgid"),
            "job_id": entry.get("job_id"),
            "kind": entry.get("kind"),
            "cmd": entry.get("cmd"),
            "recorded_at": entry.get("recorded_at"),
            "age_sec": round(now - float(entry.get("recorded_at") or 0), 1),
            "identity_recorded": entry.get("identity"),
            "identity_now": _proc_identity(entry["pid"]) if isinstance(entry.get("pid"), int) else "",
            "job_active": (entry.get("job_id") in active) if active is not None else None,
            "verdict": verdict,
            "reason": reason,
            "action": "NONE",
        }
        row["identity_match"] = bool(row["identity_now"]) and row["identity_now"] == row["identity_recorded"]
        report["entries"].append(row)

        if verdict != "WOULD_REAP":
            _audit(row, "NONE", reason)
            continue

        report["would_reap"].append(entry.get("pid"))

        if not allowed:
            row["action"] = "DRY_RUN"
            _audit(row, "DRY_RUN", gate_reason)
            continue

        # ── The final safety gate, immediately before any signal ────────────
        # Classification ran earlier in this loop. However small, that gap is
        # long enough for the pid to be recycled, so identity is re-read now
        # rather than trusted from the row above.
        pid = entry["pid"]
        if _proc_identity(pid) != entry.get("identity"):
            row["verdict"] = "DO_NOT_REAP"
            row["reason"] = "IDENTITY_CHANGED — pid was recycled between classification and signal"
            row["action"] = "SKIPPED"
            report["would_reap"].remove(pid)
            _audit(row, "SKIPPED", row["reason"])
            continue

        row["action"] = "REAP"
        _audit(row, "REAP_STARTED")
        try:
            ok = reap_pid(pid, timeout=timeout)   # SIGTERM -> grace -> SIGKILL -> verify
        except Exception as e:
            ok = False
            logger.error(f"proctree: REAP_FAILED pid {pid}: {e}")
        report["signals_sent"] += 1

        # Only call it reaped if the disappearance was actually verified.
        if ok and not _group_members(entry.get("pgid") or pid):
            row["action"] = "REAP_COMPLETE"
            report["reaped"].append(pid)
            _audit(row, "REAP_COMPLETE")
        else:
            row["action"] = "REAP_FAILED"
            row["verdict"] = "REAP_FAILED"
            report["failed"].append(pid)
            # The registry entry is deliberately left in place by reap_pid so the
            # next startup sweep sees it again.
            _audit(row, "REAP_FAILED", "entry retained for the next sweep")

    logger.info(
        f"proctree sweep ({'REAPING' if allowed else 'dry-run'}): "
        f"{len(report['entries'])} entries, {len(report['would_reap'])} eligible, "
        f"{len(report['reaped'])} reaped, {len(report['failed'])} failed, "
        f"{report['signals_sent']} reap calls"
        + ("" if report["available"] else f" [UNAVAILABLE: {report['unavailable_reason']}]")
    )
    return report


def log_sweep_report(report: dict) -> None:
    """One log line per classified entry."""
    if not report["entries"]:
        logger.info("proctree sweep: registry is empty — nothing to classify")
        return
    for row in report["entries"]:
        logger.info(
            f"proctree sweep: pid={row['pid']} pgid={row['pgid']} kind={row['kind']} "
            f"job={row['job_id']} age={row['age_sec']}s identity_match={row['identity_match']} "
            f"job_active={row['job_active']} -> {row['verdict']} [{row['action']}] ({row['reason']})"
        )


# ── Observation reporting (read-only: registry + audit records) ──────────────

AGE_BUCKETS = (
    ("<15m", 0, 900),
    ("15m-1h", 900, 3600),
    ("1h-6h", 3600, 21600),
    ("6h+", 21600, float("inf")),
)


def _age_bucket(age_sec) -> str:
    try:
        age = float(age_sec)
    except (TypeError, ValueError):
        return "unknown"
    for name, lo, hi in AGE_BUCKETS:
        if lo <= age < hi:
            return name
    return "unknown"


def _category(row: dict) -> str:
    """Report bucket for one observation, from structured fields only.

    Derived from verdict/kind/job_active/identity_match rather than by matching
    text in the reason string, so rewording a message cannot silently move
    entries between categories.
    """
    verdict = row.get("verdict")
    if verdict == "NEVER_REAP":
        return "NEVER_REAP - Metro" if row.get("kind") == "metro" else "NEVER_REAP - other"
    if verdict == "DO_NOT_REAP":
        if row.get("job_active") is True:
            return "DO_NOT_REAP - active job"
        if row.get("identity_match") is False:
            return "DO_NOT_REAP - identity mismatch"
        return "DO_NOT_REAP - other"
    return verdict or "UNKNOWN"


def read_audit(path: Optional[str] = None, since: Optional[float] = None) -> List[dict]:
    """Audit records, newest last. Malformed lines are skipped, never fatal."""
    path = path or AUDIT_PATH
    rows, skipped = [], 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                if not isinstance(rec, dict):
                    skipped += 1
                    continue
                if since is not None and float(rec.get("ts") or 0) < since:
                    continue
                rows.append(rec)
    except FileNotFoundError:
        return []
    except OSError as e:
        logger.warning(f"proctree: audit file unreadable: {e}")
        return []
    if skipped:
        logger.warning(f"proctree: skipped {skipped} malformed audit record(s) in {path}")
    return rows


def summarize(rows: List[dict]) -> dict:
    """Aggregate observations. Reads nothing but the rows it is given.

    The same orphan is re-observed by every sweep, so counts are over DISTINCT
    entries — keyed by (pid, recorded_at), which a recycled pid cannot collide
    with — while ``observations`` keeps the raw record count.
    """
    latest = {}
    for r in rows:
        latest[(r.get("pid"), r.get("recorded_at"))] = r   # last observation wins

    counts, by_kind, by_job, by_age = {}, {}, {}, {}
    for r in latest.values():
        cat = _category(r)
        counts[cat] = counts.get(cat, 0) + 1
        if cat != "WOULD_REAP":
            continue
        kind = r.get("kind") or "unknown"
        by_kind.setdefault(kind, {})
        job = r.get("job_id") or "unknown"
        by_kind[kind][job] = by_kind[kind].get(job, 0) + 1
        by_job[job] = by_job.get(job, 0) + 1
        bucket = _age_bucket(r.get("age_sec"))
        by_age[bucket] = by_age.get(bucket, 0) + 1

    stamps = [float(r["ts"]) for r in rows if r.get("ts") not in (None, "")]
    return {
        "observations": len(rows),
        "entries_examined": len(latest),
        "period_start": min(stamps) if stamps else None,
        "period_end": max(stamps) if stamps else None,
        "counts": counts,
        "would_reap_by_kind": by_kind,
        "would_reap_by_job": by_job,
        "would_reap_by_age": by_age,
    }


REPORT_ORDER = (
    "WOULD_REAP", "DO_NOT_REAP - active job", "DO_NOT_REAP - identity mismatch",
    "DO_NOT_REAP - other", "TOO_YOUNG", "NEVER_REAP - Metro", "NEVER_REAP - other",
    "ALREADY_GONE", "UNKNOWN", "REAP_FAILED", "REAP_COMPLETE",
)


def format_summary(summary: dict) -> str:
    """Compact human-readable observation report."""
    def stamp(v):
        return datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M") if v else "-"

    out = [
        f"Observation period : {stamp(summary['period_start'])} -> {stamp(summary['period_end'])}",
        f"Observations       : {summary['observations']}",
        f"Entries examined   : {summary['entries_examined']}  (distinct registry entries)",
        "",
    ]
    counts = summary["counts"]
    for cat in REPORT_ORDER:
        if cat in counts:
            out.append(f"  {cat:<32} {counts[cat]}")
    for cat, n in sorted(counts.items()):        # anything not in the fixed order
        if cat not in REPORT_ORDER:
            out.append(f"  {cat:<32} {n}")

    if summary["would_reap_by_kind"]:
        out += ["", "WOULD_REAP"]
        for kind, jobs in sorted(summary["would_reap_by_kind"].items()):
            out.append(f"  {kind}:")
            for job, n in sorted(jobs.items(), key=lambda kv: (-kv[1], kv[0])):
                out.append(f"    {job}: {n}")
        out += ["", "Age:"]
        for name, _lo, _hi in AGE_BUCKETS:
            if name in summary["would_reap_by_age"]:
                out.append(f"  {name}: {summary['would_reap_by_age'][name]}")
        if "unknown" in summary["would_reap_by_age"]:
            out.append(f"  unknown: {summary['would_reap_by_age']['unknown']}")
    return "\n".join(out)


def observation_report(path: Optional[str] = None, since: Optional[float] = None) -> str:
    """Read-only summary of the audit trail. Touches no process."""
    return format_summary(summarize(read_audit(path=path, since=since)))


if __name__ == "__main__":  # python -m automation.utils.proctree
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(
        description="Orphan sweep and observation report. Reaping requires all three "
                    "PROC_SWEEP_* switches; the default configuration signals nothing.")
    ap.add_argument("--json", action="store_true", help="print the raw report")
    ap.add_argument("--summary", action="store_true",
                    help="summarize the audit trail instead of sweeping (read-only)")
    ap.add_argument("--since-hours", type=float, default=None,
                    help="with --summary, only records from the last N hours")
    ap.add_argument("--audit-file", default=None, help="audit trail to read (default: AUDIT_PATH)")
    args = ap.parse_args()

    if args.summary:
        since = time.time() - args.since_hours * 3600 if args.since_hours else None
        if args.json:
            print(json.dumps(summarize(read_audit(path=args.audit_file, since=since)),
                             indent=2, default=str))
        else:
            print(observation_report(path=args.audit_file, since=since))
        raise SystemExit(0)

    rep = sweep_orphans(dry_run=True)
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        log_sweep_report(rep)
        print(f"\nentries={len(rep['entries'])} would_reap={rep['would_reap']} "
              f"available={rep['available']} signals_sent={rep['signals_sent']}")
