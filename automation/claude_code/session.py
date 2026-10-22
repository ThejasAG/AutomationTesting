"""AI Agent sessions as the API and the CLI see them: start, follow, stop, approve.

A session row is created here and handed to its own process
(`python -m automation.claude_code.runner <id>`), so the work goes on through a
backend restart. Approving merges the session's branch into the checkout's
current branch (never main); rejecting deletes the branch.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from automation.claude_code import events, paths

KINDS = ("heal", "fix_run", "analyze", "generate", "nightly")
ACTIVE = ("queued", "preparing", "running", "checking")
APPROVABLE = ("review",)
PROTECTED_BRANCHES = ("main", "master")


class Busy(Exception):
    pass


class Refused(Exception):
    pass


# ── Claude Code itself ───────────────────────────────────────────────────────

_claude_cache: Dict[str, Any] = {"t": 0.0, "v": None}


def claude_status(max_age: float = 120.0) -> Dict[str, Any]:
    """Is Claude Code installed and logged in (and on which plan)? Never the email
    or any token. Cached: `claude auth status` takes about a second."""
    if _claude_cache["v"] is not None and time.time() - _claude_cache["t"] < max_age:
        return _claude_cache["v"]
    import shutil
    exe = shutil.which("claude") or ("/usr/local/bin/claude" if os.path.exists("/usr/local/bin/claude") else "")
    v: Dict[str, Any] = {"installed": bool(exe), "version": "", "logged_in": False, "plan": "", "auth": ""}
    if exe:
        env = {k: val for k, val in os.environ.items()
               if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20, env=env)
            v["version"] = out.stdout.strip().split(" ")[0]
            out = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, timeout=30, env=env)
            d = json.loads(out.stdout or "{}")
            v.update(logged_in=bool(d.get("loggedIn")), plan=d.get("subscriptionType") or "",
                     auth=d.get("authMethod") or "")
        except (subprocess.SubprocessError, ValueError, OSError):
            pass
    _claude_cache.update(t=time.time(), v=v)
    return v


# ── rows ─────────────────────────────────────────────────────────────────────

def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def as_dict(r: Any) -> Dict[str, Any]:
    return {"id": r.id, "kind": r.kind, "trigger": r.trigger, "status": r.status, "phase": r.phase,
            "options": r.options or {}, "created_by": r.created_by,
            "created_at": _iso(r.created_at), "started_at": _iso(r.started_at),
            "finished_at": _iso(r.finished_at), "branch": r.branch, "base_commit": r.base_commit,
            "files_changed": r.files_changed or [], "gates": r.gates or [],
            "summary": r.summary or {}, "result": r.result or {}, "error": r.error,
            "decision_by": r.decision_by, "decided_at": _iso(r.decided_at),
            "claude_session_id": r.claude_session_id, "stop_requested": bool(r.stop_requested)}


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def get(sid: str) -> Optional[Dict[str, Any]]:
    from automation.database.config import SessionLocal
    from automation.database.models import AgentSession
    with SessionLocal() as db:
        r = db.get(AgentSession, sid)
        return as_dict(r) if r else None


def list_sessions(limit: int = 30) -> List[Dict[str, Any]]:
    from automation.database.config import SessionLocal
    from automation.database.models import AgentSession
    with SessionLocal() as db:
        rows = db.query(AgentSession).order_by(AgentSession.created_at.desc()).limit(limit).all()
        return [as_dict(r) for r in rows]


def active() -> Optional[Dict[str, Any]]:
    """The session in progress, if any. A row whose runner died is closed here."""
    from automation.database.config import SessionLocal
    from automation.database.models import AgentSession
    with SessionLocal() as db:
        rows = db.query(AgentSession).filter(AgentSession.status.in_(ACTIVE)).all()
        live = None
        for r in rows:
            young = r.created_at and (datetime.utcnow() - r.created_at).total_seconds() < 60
            if _pid_alive(r.pid) or (r.status == "queued" and young):
                live = live or as_dict(r)
            else:
                r.status, r.finished_at = "interrupted", datetime.utcnow()
                r.phase = "the session's process ended unexpectedly"
        db.commit()
    return live


def reap_orphans() -> int:
    """On backend start: close sessions whose runner is gone, drop stale worktrees."""
    before = len([s for s in list_sessions(50) if s["status"] in ACTIVE])
    still = active()
    subprocess.run(["git", "-C", paths.MAIN_ROOT, "worktree", "prune"], capture_output=True, timeout=60)
    return before - (1 if still else 0)


# ── start / stop ─────────────────────────────────────────────────────────────

def create(kind: str, trigger: str = "manual", options: Optional[Dict[str, Any]] = None,
           created_by: str = "", spawn: bool = True) -> str:
    from automation.database.config import SessionLocal, initialize_database
    from automation.database.models import AgentSession
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    options = dict(options or {})
    if kind == "fix_run" and not options.get("run_ids"):
        raise ValueError("fix_run needs the failed run's id")
    cur = active()
    if cur:
        raise Busy(f"an AI Agent session is already {cur['status']} ({cur['kind']}, {cur['id'][:8]})")
    st = claude_status()
    if not (st["installed"] and st["logged_in"]):
        raise Refused("Claude Code is not installed or not logged in on this Mac "
                      "(run `claude` once in a terminal and log in)")
    initialize_database()
    import uuid
    sid = str(uuid.uuid4())
    with SessionLocal() as db:
        db.add(AgentSession(id=sid, kind=kind, trigger=trigger, status="queued", options=options,
                            created_by=created_by or None, phase="starting"))
        db.commit()
    if spawn:
        spawn_runner(sid)
    return sid


def spawn_runner(sid: str) -> int:
    from automation.database.config import SessionLocal
    from automation.database.models import AgentSession
    sdir = paths.session_dir(sid)
    os.makedirs(sdir, exist_ok=True)
    out = open(os.path.join(sdir, "runner.out"), "a")
    env = dict(os.environ, PYTHONPATH=paths.MAIN_ROOT, AGENT_MAIN_ROOT=paths.MAIN_ROOT)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    proc = subprocess.Popen([sys.executable, "-m", "automation.claude_code.runner", sid],
                            cwd=paths.MAIN_ROOT, stdout=out, stderr=subprocess.STDOUT, env=env,
                            start_new_session=True)
    with SessionLocal() as db:
        r = db.get(AgentSession, sid)
        if r is not None:
            r.pid = proc.pid
            db.commit()
    return proc.pid


def stop(sid: str) -> bool:
    from automation.database.config import SessionLocal
    from automation.database.models import AgentSession
    with SessionLocal() as db:
        r = db.get(AgentSession, sid)
        if r is None or r.status not in ACTIVE:
            return False
        r.stop_requested = True
        if not _pid_alive(r.pid):
            r.status, r.finished_at, r.phase = "stopped", datetime.utcnow(), "stopped"
        db.commit()
    return True


# ── follow ───────────────────────────────────────────────────────────────────

def session_events(sid: str, after: int = 0) -> Dict[str, Any]:
    s = get(sid)
    if s is None:
        raise ValueError("no such session")
    sdir = paths.session_dir(sid)
    roots = [p for p in (os.path.join(paths.WORKTREES_DIR, sid[:8]), sdir, paths.MAIN_ROOT) if p]
    items, last = events.condense(os.path.join(sdir, "transcript.jsonl"), after, roots)
    return {"events": items, "last": last, "status": s["status"], "phase": s["phase"]}


def read_file(sid: str, name: str, limit: int = 2_000_000) -> str:
    allowed = {"report.md", "changes.patch", "stopped.patch", "runner.log", "prompt.md"}
    if name not in allowed:
        raise ValueError("not a session file")
    try:
        with open(os.path.join(paths.session_dir(sid), name), encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def runs(sid: str) -> List[Dict[str, Any]]:
    from automation.claude_code import gates
    return gates.read_runs(paths.session_dir(sid))


def app_patches(sid: str) -> List[Dict[str, str]]:
    d = os.path.join(paths.session_dir(sid), "app_fixes")
    out = []
    for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if n.endswith(".patch"):
            with open(os.path.join(d, n), encoding="utf-8", errors="replace") as f:
                out.append({"name": n, "patch": f.read(200_000)})
    return out


def latest_feature_map() -> Optional[Dict[str, Any]]:
    for s in list_sessions(60):
        fmap = (s.get("summary") or {}).get("feature_map")
        if s["kind"] in ("analyze", "nightly") and isinstance(fmap, dict) and fmap.get("apps"):
            return {**fmap, "session_id": s["id"], "finished_at": s["finished_at"]}
    return None


# ── decide ───────────────────────────────────────────────────────────────────

def _git(*args: str, check: bool = True) -> str:
    out = subprocess.run(["git", "-C", paths.MAIN_ROOT, *args], capture_output=True, text=True, timeout=120)
    if check and out.returncode != 0:
        raise Refused(out.stderr.strip() or f"git {' '.join(args)} failed")
    return out.stdout


def _decide(sid: str, status: str, by: str) -> None:
    from automation.database.config import SessionLocal
    from automation.database.models import AgentSession
    with SessionLocal() as db:
        r = db.get(AgentSession, sid)
        r.status, r.decision_by, r.decided_at = status, by or None, datetime.utcnow()
        r.phase = {"approved": "approved and merged", "rejected": "rejected"}.get(status, status)
        db.commit()


def approve(sid: str, by: str = "") -> Dict[str, Any]:
    s = get(sid)
    if s is None:
        raise ValueError("no such session")
    if s["status"] not in APPROVABLE:
        raise Refused("only a session whose checks all passed can be approved "
                      f"(this one is '{s['status']}')")
    branch = s["branch"]
    if not branch or not _git("branch", "--list", branch).strip():
        raise Refused(f"the session's branch {branch!r} no longer exists")
    current = _git("rev-parse", "--abbrev-ref", "HEAD").strip()
    if current in PROTECTED_BRANCHES or current == "HEAD":
        raise Refused(f"the checkout is on '{current}': agent changes are never merged into it. "
                      "Switch to your working branch first.")
    try:
        _git("merge", "--no-ff", "-m", f"Merge {branch} (AI Agent session {sid[:8]}, approved)", branch)
    except Refused as e:
        _git("merge", "--abort", check=False)
        raise Refused(f"the merge did not apply cleanly ({e}); nothing was changed. Commit or "
                      "stash your own edits to the same files, then approve again.")
    check = subprocess.run([paths.PYTHON, paths.TOOLS, "check-flows"], cwd=paths.MAIN_ROOT,
                           capture_output=True, text=True, timeout=180,
                           env=dict(os.environ, PYTHONPATH=paths.MAIN_ROOT))
    _decide(sid, "approved", by)
    return {"merged": True, "into": current, "branch": branch,
            "check_flows": (check.stdout or check.stderr).strip()[:1000],
            "restart_needed": True}


def reject(sid: str, by: str = "") -> Dict[str, Any]:
    s = get(sid)
    if s is None:
        raise ValueError("no such session")
    if s["status"] not in ("review", "gates_failed"):
        raise Refused(f"nothing to reject (the session is '{s['status']}')")
    if s["branch"]:
        _git("branch", "-D", s["branch"], check=False)
    _decide(sid, "rejected", by)
    return {"rejected": True}


# ── the backend itself ───────────────────────────────────────────────────────

def restart_backend_if_idle() -> Dict[str, Any]:
    """Approved changes load on the next backend start. Restart only when nothing is
    running: the supervisor (scripts/supervise_backend.sh) starts it again in ~2 s."""
    from automation.claude_code.runner import active_runs
    busy = active_runs()
    if busy:
        raise Busy(f"run {busy[0].id[:8]} '{busy[0].test_name}' is {busy[0].status} — "
                   "restart when it has finished")
    if active():
        raise Busy("an AI Agent session is in progress — restart when it has finished")
    try:
        from automation.api.v1.routers.jobs import run_all_in_progress
        if run_all_in_progress():
            raise Busy("a Run all is between two flows — restart when it has finished")
    except ImportError:
        pass
    try:
        with open(os.path.join(paths.MAIN_ROOT, "logs", "supervisor.pid")) as f:
            supervised = _pid_alive(int(f.read().strip() or 0))
    except (OSError, ValueError):
        supervised = False
    if not supervised:
        raise Refused("the backend is not running under the supervisor, so it would not come "
                      "back by itself — restart it with ./stop.sh && ./start.sh")
    import signal
    import threading
    threading.Timer(1.0, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
    return {"restarting": True}
