"""One AI Agent session, start to finish. Runs as its own detached process:

    python -m automation.claude_code.runner <session_id>

so it survives a backend restart (the backend only creates the row and spawns
this). Steps:

 1. lock (one session at a time); for device work make sure the rig is up and wait
    while a person's run is on the simulators;
 2. a git worktree on a new branch `agent/<stamp>-<kind>` from HEAD, with the
    untracked runtime files (.env, cross_app_config.json, repos/, .venv) linked in;
 3. `claude -p` in that worktree on the logged-in Claude plan (ANTHROPIC_API_KEY is
    removed from its environment), with the session's permission rules and the
    guard hook, until it ends, hits its time limit or is stopped;
 4. harvest: what changed (only automation/, tests/, scripts/ — anything else is
    reverted), then the gates (gates.py); the change is committed on the branch and
    waits for a person: `review` when every gate passed, `gates_failed` otherwise;
 5. report (email/Slack when configured) and clean up the worktree (the branch stays).
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from automation.claude_code import paths

DEVICE_KINDS = ("heal", "fix_run", "generate", "nightly")
BUSY_WAIT_MIN = 30
_STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
_COMMIT_TRAILER = "Co-Authored-By: Claude Code (AI Agent) <noreply@anthropic.com>"


# ── small helpers ────────────────────────────────────────────────────────────

def git(repo: str, *args: str, check: bool = True, timeout: int = 120) -> str:
    out = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=timeout)
    if check and out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"git {' '.join(args)} failed")
    return out.stdout


def log(sdir: str, msg: str) -> None:
    line = f"{datetime.now():%H:%M:%S} {msg}"
    print(line, flush=True)
    with open(os.path.join(sdir, "runner.log"), "a") as f:
        f.write(line + "\n")


def _set(sid: str, **kw) -> None:
    from automation.database.config import SessionLocal
    from automation.database.models import AgentSession
    with SessionLocal() as db:
        row = db.get(AgentSession, sid)
        if row is None:
            return
        for k, v in kw.items():
            setattr(row, k, v)
        db.commit()


def _row(sid: str) -> Dict[str, Any]:
    from automation.database.config import SessionLocal
    from automation.database.models import AgentSession
    with SessionLocal() as db:
        r = db.get(AgentSession, sid)
        if r is None:
            raise SystemExit(f"no agent session {sid}")
        return {c.name: getattr(r, c.name) for c in r.__table__.columns}


def _stop_requested(sid: str) -> bool:
    try:
        return bool(_row(sid).get("stop_requested"))
    except Exception:
        return False


def phase(sid: str, sdir: str, text: str) -> None:
    log(sdir, text)
    _set(sid, phase=text[:300])


# ── 1. the rig ───────────────────────────────────────────────────────────────

def ensure_rig(sdir: str) -> bool:
    from automation.agentic import preflight
    rig = [c for c in preflight.run("staging") if c["name"] != "Staging backend"]
    if preflight.all_ok(rig):
        return True
    log(sdir, "rig down (" + "; ".join(f"{c['name']}: {c['detail']}" for c in rig if not c["ok"])
        + ") — running ./start.sh")
    with open(os.path.join(sdir, "start.log"), "w") as f:
        subprocess.run(["./start.sh"], cwd=paths.MAIN_ROOT, stdout=f, stderr=subprocess.STDOUT,
                       timeout=1200)
    return preflight.all_ok([c for c in preflight.run("staging") if c["name"] != "Staging backend"])


def active_runs() -> List[Any]:
    """Runs on the rig now (a person's or a session's), ignoring stale rows."""
    from automation.database.config import SessionLocal
    from automation.database.models import TestRun
    since = datetime.utcnow() - timedelta(minutes=90)
    with SessionLocal() as db:
        rows = (db.query(TestRun).filter(TestRun.status.in_(["running", "queued"]),
                                         TestRun.created_at >= since).all())
        for r in rows:
            db.expunge(r)
    return rows


def wait_until_idle(sid: str, sdir: str, minutes: float = BUSY_WAIT_MIN) -> bool:
    t_end = time.time() + minutes * 60
    while time.time() < t_end:
        busy = active_runs()
        if not busy:
            return True
        if _stop_requested(sid):
            return False
        phase(sid, sdir, f"waiting: run {busy[0].id[:8]} '{busy[0].test_name}' is {busy[0].status} "
                         f"(a person's runs go first)")
        time.sleep(30)
    return not active_runs()


# ── 2. the worktree ──────────────────────────────────────────────────────────

def make_worktree(sid: str, kind: str) -> Dict[str, str]:
    base = git(paths.MAIN_ROOT, "rev-parse", "HEAD").strip()
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    branch, i = f"agent/{stamp}-{kind}", 2
    while git(paths.MAIN_ROOT, "branch", "--list", branch).strip():
        branch, i = f"agent/{stamp}-{kind}-{i}", i + 1
    os.makedirs(paths.WORKTREES_DIR, exist_ok=True)
    wt = os.path.join(paths.WORKTREES_DIR, sid[:8])
    if os.path.exists(wt):
        remove_worktree(wt)
    git(paths.MAIN_ROOT, "worktree", "add", "-q", "-b", branch, wt, base)
    link_runtime(wt)
    return {"worktree": wt, "branch": branch, "base": base}


def flows_at(commit: str) -> Optional[Dict[str, Any]]:
    """The flows as `commit` defines them — a continued session's worktree already holds
    its edits, so the 'before' side is read from a clean worktree of the base."""
    from automation.claude_code import gates
    tmp = os.path.join(paths.WORKTREES_DIR, f"flows-{commit[:10]}")
    if os.path.exists(tmp):
        remove_worktree(tmp)
    git(paths.MAIN_ROOT, "worktree", "add", "-q", "--detach", tmp, commit)
    try:
        link_runtime(tmp)
        return gates.dump_flows(tmp)
    finally:
        remove_worktree(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def continuing(row: Dict[str, Any]) -> bool:
    """A session being continued after its process ended (session.continue_session)."""
    return bool((row.get("options") or {}).get("continued")) and bool(row.get("worktree")) \
        and os.path.isdir(row["worktree"]) and bool(row.get("branch"))


def link_runtime(wt: str) -> None:
    for name in paths.WORKTREE_LINKS:
        src = os.path.join(paths.MAIN_ROOT, name)
        dst = os.path.join(wt, name)
        if not os.path.exists(src):
            continue
        if not os.path.lexists(dst):
            os.symlink(src, dst)
        elif os.path.isdir(dst) and not os.path.islink(dst) and os.path.isdir(src):
            # A folder git tracks part of (repos/ holds a few tracked entries, while
            # the app checkouts in it are untracked): link what the worktree lacks.
            # MEASURED 2026-10-22: without this the analysis found no app at all.
            for child in os.listdir(src):
                c_dst = os.path.join(dst, child)
                if not os.path.lexists(c_dst):
                    os.symlink(os.path.join(src, child), c_dst)


def remove_worktree(wt: str) -> None:
    git(paths.MAIN_ROOT, "worktree", "remove", "--force", wt, check=False)
    shutil.rmtree(wt, ignore_errors=True)
    git(paths.MAIN_ROOT, "worktree", "prune", check=False)


# ── 3. Claude Code ───────────────────────────────────────────────────────────

def session_settings(wt: str, sdir: str) -> Dict[str, Any]:
    """Permission rules for the unattended session (`--permission-mode dontAsk`
    refuses everything not allowed) plus the guard hook, which re-checks each call
    with real paths."""
    w, s, m = "/" + wt.rstrip("/"), "/" + sdir.rstrip("/"), "/" + paths.MAIN_ROOT.rstrip("/")
    allow = [f"Read({w}/**)", f"Read({s}/**)", f"Read({m}/repos/**)",
             "Grep", "Glob", "TodoWrite", "Skill", "Agent", "Task",
             f"Bash(.venv/bin/python {paths.TOOLS_REL} *)", f"Bash(.venv/bin/python {paths.TOOLS_REL})",
             "Bash(git diff)", "Bash(git diff *)", "Bash(git status)", "Bash(git status *)",
             "Bash(git log *)", "Bash(git show *)", f"Write({s}/**)", f"Edit({s}/**)"]
    for d in ("automation", "tests", "scripts"):
        allow += [f"Edit({w}/{d}/**)", f"Write({w}/{d}/**)"]
    deny = [f"Read({r}/{f})" for r in (w, m) for f in (".env", ".env.*", "cross_app_config.json")]
    deny += [f"Edit({m}/repos/**)", f"Write({m}/repos/**)", "WebFetch", "WebSearch"]
    hook = f"{paths.PYTHON} {paths.GUARD}"
    return {
        "permissions": {"allow": allow, "deny": deny},
        "hooks": {"PreToolUse": [{"matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit|Read|Grep|Glob|WebFetch|WebSearch",
                                  "hooks": [{"type": "command", "command": hook}]}]},
        "env": {"BASH_DEFAULT_TIMEOUT_MS": "600000", "BASH_MAX_TIMEOUT_MS": "600000"},
    }


def claude_env(wt: str, sdir: str, sid: str) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env["PATH"] = "/usr/local/bin:/opt/homebrew/bin:" + env.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    env.update(AGENT_WORKTREE=wt, AGENT_SESSION_DIR=sdir, AGENT_MAIN_ROOT=paths.MAIN_ROOT,
               AGENT_SESSION_ID=sid, PYTHONPATH=wt)
    return env


def _known_failures() -> str:
    try:
        from automation.claude_code import baselines
        latest = baselines.latest()
    except Exception:
        latest = None
    if not latest or not latest.get("failed"):
        return "none recorded"
    return ", ".join(latest["failed"])


def session_hours(kind: str, options: Dict[str, Any], s: Dict[str, Any]) -> float:
    """How long the session may run. A generate session asked for many scenarios gets
    ~12 minutes each (a draft, one device run, maybe one fix) — the default 3 hours
    is for the default 3."""
    hours = float((s.get("hours") or {}).get(kind) or 3.0)
    if kind == "generate":
        n = int(options.get("max_new") or len(options.get("features") or []) or 0)
        hours = max(hours, min(8.0, 0.5 + 0.2 * n))
    return hours


def build_prompt(row: Dict[str, Any], ctx: Dict[str, str], s: Dict[str, Any]) -> str:
    from automation.claude_code import prompts
    return prompts.build(row["kind"], row.get("options") or {}, ctx, s)


def continue_prompt(ctx: Dict[str, str], wt: str) -> str:
    """What a continued session is told when its conversation picks up again."""
    lines = [
        "Your session was interrupted: its process ended (the Mac was restarted or the "
        "platform was stopped), and it is now being continued in the same worktree. The "
        "simulators, Appium and the backend were restarted too.",
        "",
        "- The last command you started may not have finished: run it again if you still "
        "need its result.",
        "- Run `preflight --wait 15` before your next run on the simulators.",
        "- `git status` and `git diff` show your edits so far; `runs.jsonl` in your session "
        "folder lists the runs you made.",
        f"- New time limits: stop starting new work at **{ctx['stop_by']}**; the report must "
        f"be written before **{ctx['deadline']}**.",
    ]
    try:
        with open(os.path.join(wt, "scripts", "agent_tools.py"), encoding="utf-8") as f:
            fingerprints = "fingerprint" in f.read()
    except OSError:
        fingerprints = False
    if not fingerprints:
        lines.append(
            "- The checks after this session compare each run with your LAST edit to any file: "
            "every flow you keep must pass on the simulators after that edit. Finish all edits "
            "first, then run each kept flow once more with `--label final`.")
    lines += ["", "Everything else — the task, the rules, the report — is unchanged."]
    return "\n".join(lines)


FALLBACK_MODEL = "sonnet"


def model_unsupported(info: Dict[str, Any]) -> bool:
    """The CLI refused the model before doing anything (MEASURED 2026-10-22: Claude Code
    2.1.205 answers 'does not support this model; version 2.1.280 or newer is
    required' for opus)."""
    return bool(info.get("is_error")) and "does not support this model" in str(info.get("result") or "") \
        and int(info.get("turns") or 0) <= 1


# Service-side interruptions that clear by themselves. MEASURED 2026-10-22: a
# generate session died after 19 turns on "Your organization has disabled Claude
# subscription access for Claude Code", and a call a few minutes later worked.
_TRANSIENT = ("disabled Claude subscription access", "Overloaded", "overloaded_error",
              "API Error: 5", "Connection error", "ECONNRESET", "socket hang up",
              "Request timed out", "fetch failed")
RESUME_WAIT_S = 120
MAX_RESUMES = 3
CONTINUE_PROMPT = (
    "Your session was interrupted by a temporary service error. Continue the task exactly "
    "where you left off: check `git status` and `git diff` for the edits you have made so "
    "far, then carry on. The rules, the time limits and the report are unchanged.")


def transient_error(info: Dict[str, Any]) -> bool:
    return bool(info.get("is_error")) and any(t in str(info.get("result") or "") for t in _TRANSIENT)


def conversation_missing(info: Dict[str, Any]) -> bool:
    return bool(info.get("is_error")) and "No conversation found" in str(info.get("result") or "") \
        and int(info.get("turns") or 0) <= 1


def run_claude(sid: str, sdir: str, wt: str, prompt: str, s: Dict[str, Any],
               deadline: datetime, name: str, resume: Optional[str] = None,
               fresh_prompt: str = "") -> Dict[str, Any]:
    """`resume`: continue that Claude conversation with `prompt`; when it cannot be
    found, start a new one with `fresh_prompt` instead."""
    model = s["model"]
    info = _run_claude_once(sid, sdir, wt, prompt, model, s, deadline, name,
                            "a" if resume else "w", resume=resume)
    if resume and conversation_missing(info) and fresh_prompt:
        phase(sid, sdir, "the earlier Claude conversation is gone — starting a new one on the "
                         "same worktree")
        prompt, resume = fresh_prompt, None
        info = _run_claude_once(sid, sdir, wt, prompt, model, s, deadline, name, "a")
    if model_unsupported(info) and model != FALLBACK_MODEL:
        phase(sid, sdir, f"this Claude Code is too old for '{model}' — continuing with "
                         f"'{FALLBACK_MODEL}' (run `claude update` on the Mac to use {model})")
        first = info
        model = FALLBACK_MODEL
        info = _run_claude_once(sid, sdir, wt, prompt, model, s, deadline, name, "a", resume=resume)
        info["model_fallback"] = {"wanted": s["model"], "used": model,
                                  "why": str(first.get("result") or "")[:300]}
    resumes = []
    while (transient_error(info) and len(resumes) < MAX_RESUMES and not info.get("stopped")
           and not info.get("timed_out")):
        why = str(info.get("result") or "")[:200]
        phase(sid, sdir, f"Claude Code was interrupted ({why}) — resuming the same session in "
                         f"{RESUME_WAIT_S // 60} min")
        t_end = time.time() + RESUME_WAIT_S
        while time.time() < t_end and datetime.now() < deadline and not _stop_requested(sid):
            time.sleep(5)
        if _stop_requested(sid):
            info["stopped"] = True
            break
        if datetime.now() >= deadline:
            info["timed_out"] = True
            break
        resumes.append(why)
        prev = info
        info = _run_claude_once(sid, sdir, wt, CONTINUE_PROMPT, model, s, deadline, name, "a",
                                resume=prev.get("claude_session_id"))
        info.update({k: prev[k] for k in ("model_fallback",) if k in prev})
    if resumes:
        info["resumed_after"] = resumes
    return info


def _run_claude_once(sid: str, sdir: str, wt: str, prompt: str, model: str, s: Dict[str, Any],
                     deadline: datetime, name: str, mode: str,
                     resume: Optional[str] = None) -> Dict[str, Any]:
    from automation.claude_code import events
    claude = shutil.which("claude") or "/usr/local/bin/claude"
    claude_sid = resume or str(uuid.uuid4())
    _set(sid, claude_session_id=claude_sid)
    session_args = ["--resume", claude_sid] if resume else ["--session-id", claude_sid, "--name", name]
    cmd = [claude, "-p", prompt, "--model", model, "--effort", s["effort"],
           "--permission-mode", "dontAsk", "--setting-sources", "project",
           "--settings", json.dumps(session_settings(wt, sdir)),
           "--strict-mcp-config",              # no MCP servers: nothing else may drive the sims
           "--add-dir", sdir,
           "--output-format", "stream-json", "--verbose", *session_args]
    transcript = os.path.join(sdir, "transcript.jsonl")
    info: Dict[str, Any] = {"claude_session_id": claude_sid, "timed_out": False, "stopped": False,
                            "model": model}
    roots = [wt, sdir, paths.MAIN_ROOT]
    with open(transcript, mode) as out:
        proc = subprocess.Popen(cmd, cwd=wt, stdout=out, stderr=subprocess.STDOUT,
                                env=claude_env(wt, sdir, sid), start_new_session=True)
        last_phase = 0.0
        while proc.poll() is None:
            now = time.time()
            if datetime.now() >= deadline:
                info["timed_out"] = True
            elif _stop_requested(sid):
                info["stopped"] = True
            if info["timed_out"] or info["stopped"]:
                _terminate(proc)
                break
            if now - last_phase > 15:
                act = events.last_activity(transcript, roots)
                if act:
                    _set(sid, phase=act[:300])
                last_phase = now
            time.sleep(5)
    info["exit_code"] = proc.returncode
    info.update(events.final_result(transcript))
    return info


def _terminate(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        proc.terminate()
    try:
        proc.wait(60)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()


def stop_flow_children(sdir: str) -> List[str]:
    """Ask every flow this session started that is still running to stop (the tools'
    child process turns SIGTERM into the runner's own stop)."""
    from automation.agentic.runinfo import status_of
    stopped = []
    runs = os.path.join(sdir, "runs")
    for name in os.listdir(runs) if os.path.isdir(runs) else []:
        if not name.endswith(".json"):
            continue
        try:
            meta = json.load(open(os.path.join(runs, name)))
        except (OSError, ValueError):
            continue
        rid, pid = name[:-5], meta.get("pid")
        if pid and status_of(rid) in ("running", "queued"):
            try:
                os.kill(int(pid), signal.SIGTERM)
                stopped.append(rid)
            except OSError:
                pass
    return stopped


# ── 4. harvest + gates ───────────────────────────────────────────────────────

def changed_paths(wt: str) -> List[str]:
    out = git(wt, "status", "--porcelain", "-uall", "--no-renames")
    found = []
    for line in out.splitlines():
        p = line[3:].strip().strip('"')
        if not p:
            continue
        if p in paths.WORKTREE_LINKS or p.split("/", 1)[0] in paths.WORKTREE_LINKS:
            continue
        found.append(p)
    return found


def revert(wt: str, rel: str) -> None:
    tracked = subprocess.run(["git", "-C", wt, "ls-files", "--error-unmatch", rel],
                             capture_output=True).returncode == 0
    if tracked:
        git(wt, "checkout", "HEAD", "--", rel, check=False)
    else:
        p = os.path.join(wt, rel)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.lexists(p):
            os.remove(p)


def numstat(wt: str, rels: List[str]) -> List[Dict[str, Any]]:
    git(wt, "add", "-N", "--", *rels, check=False)            # new files count too
    out = git(wt, "diff", "--numstat", "HEAD", "--", *rels)
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            a, r, p = parts
            rows.append({"path": p, "added": int(a) if a.isdigit() else 0,
                         "removed": int(r) if r.isdigit() else 0})
    return rows


def device_requirements(wt: str, base: str, keep: List[str], before: Dict[str, Any],
                        after: Dict[str, Any]) -> Dict[str, Any]:
    """Which flows must pass on the simulators after the last edit."""
    from automation.claude_code import gates, impact
    changed, new = gates.flow_changes(before["flows"], after["flows"])
    must = sorted(set(changed) | set(new))
    any_of: Optional[List[str]] = None
    why = []
    device_code = [p for p in keep if p.startswith(gates.DEVICE_CODE)]
    if gates.FLOWS_FILE in device_code:
        try:
            old_src = git(paths.MAIN_ROOT, "show", f"{base}:{gates.FLOWS_FILE}")
            new_src = open(os.path.join(wt, gates.FLOWS_FILE), encoding="utf-8").read()
            funcs = impact.changed_functions(old_src, new_src)
        except (RuntimeError, OSError, SyntaxError):
            funcs = {"<unparsed>"}
        if funcs:
            any_of = impact.flows_for_functions(after["flows"], new_src, funcs, after.get("aliases"))
            why.append(f"changed engine functions: {', '.join(sorted(funcs))[:300]}")
    others = [p for p in device_code if p != gates.FLOWS_FILE]
    if others:
        any_of = [impact.ALL]
        why.append(f"changed device code: {', '.join(others)[:300]}")
    return {"must": must, "any_of": any_of, "why": "; ".join(why)}


def current_code_check(wt: str, after: Dict[str, Any]):
    """run -> did it execute the code its flow has now in `wt`? (None: no fingerprint,
    or the final code cannot be read — the gate then falls back to the last edit.)"""
    from automation.claude_code import fingerprint, gates
    try:
        with open(os.path.join(wt, gates.FLOWS_FILE), encoding="utf-8") as f:
            src = f.read()
        units_now, other_now = fingerprint.units(src), fingerprint.other_code(wt)
    except (OSError, SyntaxError):
        return None
    segs = after.get("segments") or {}
    aliases = after.get("aliases") or {}

    def check(run: Dict[str, Any]) -> Optional[bool]:
        fid = run.get("flow_id")
        if fid not in segs:
            return None
        return fingerprint.run_is_current(run.get("run_id") or "", fingerprint.segment_hashes(segs[fid]),
                                          units_now, other_now, after["flows"].get(fid) or [],
                                          src, aliases)
    return check


def harvest(sid: str, sdir: str, wt: str, base: str, branch: str, before: Optional[Dict[str, Any]],
            s: Dict[str, Any]) -> Dict[str, Any]:
    from automation.claude_code import baselines, gates
    out: Dict[str, Any] = {"status": "no_changes", "gates": [], "files": []}
    paths_now = changed_paths(wt)
    noise = [p for p in paths_now if p.startswith("logs/") or "/__pycache__/" in p or p.endswith(".pyc")]
    for p in noise:
        revert(wt, p)
    keep, outside = gates.split_scope([p for p in paths_now if p not in noise])
    for p in outside:
        revert(wt, p)
    if not keep:
        if outside:
            out["gates"] = [gates.gate("scope", False, "reverted (outside automation/, tests/, scripts/ "
                                                       "or protected): " + ", ".join(outside))]
        return out

    files = numstat(wt, keep)
    out["files"] = files
    gl: List[Dict[str, Any]] = [gates.gate(
        "scope", not outside,
        ("reverted (outside the editable folders or protected): " + ", ".join(outside)) if outside
        else "only automation/, tests/ and scripts/ changed")]
    phase(sid, sdir, "checking the change: compile, flows, value checks")
    gl.append(gates.compile_check(wt, keep))
    gl.append(gates.flows_load(wt, paths.TOOLS))
    after = gates.dump_flows(wt)
    if before is not None:
        gl.append(gates.checks_kept(before["flows"], (after or {}).get("flows")))

    phase(sid, sdir, "checking the change: unit tests (waits while a person's run is on the rig)")
    wait_until_idle(sid, sdir, 45)
    phase(sid, sdir, "checking the change: full unit-test suite")
    unit = gates.run_unit_tests(wt)
    base_line = None
    if unit.get("failed"):
        phase(sid, sdir, "unit tests failed — measuring the base commit's failures to compare")
        base_line = baselines.get_or_measure(base, lambda m: phase(sid, sdir, m),
                                             tests=unit["failed"])
    gl.append(gates.unit_gate(unit, base_line))

    if before is not None and after is not None:
        req = device_requirements(wt, base, keep, before, after)
        last_edit = max((os.path.getmtime(os.path.join(wt, p)) for p in keep
                         if os.path.exists(os.path.join(wt, p))), default=0.0)
        runs = gates.confirm_runs(gates.read_runs(sdir))
        if req["must"] or req["any_of"]:
            g = gates.device_gate(runs, last_edit, req["must"], req["any_of"],
                                  current=current_code_check(wt, after))
            if req["why"]:
                g["detail"] = req["why"] + "\n" + g["detail"]
            gl.append(g)
        else:
            gl.append(gates.gate("on device", True, "no flow or device code changed"))
    gl.append(gates.size_gate(files, int(s.get("max_changed_lines") or 800)))
    out["gates"] = gl
    ok = all(g["ok"] for g in gl)

    summary = _read_json(os.path.join(sdir, "summary.json")) or {}
    headline = (summary.get("headline") or f"AI Agent {sid[:8]} changes").strip().splitlines()[0][:120]
    body = "\n".join(f"- {g['name']}: {'ok' if g['ok'] else 'FAILED'} — {g['detail'].splitlines()[0][:160]}"
                     for g in gl)
    git(wt, "add", "--", *keep)
    git(wt, "-c", "user.name=Vyapy AI Agent", "-c", "user.email=noreply@anthropic.com",
        "commit", "-q", "--no-verify", "-m",
        f"agent: {headline}\n\nAI Agent session {sid} (Claude Code). Gates:\n{body}\n\n{_COMMIT_TRAILER}")
    patch = git(paths.MAIN_ROOT, "diff", f"{base}..{branch}")
    with open(os.path.join(sdir, "changes.patch"), "w", encoding="utf-8") as f:
        f.write(patch)
    out["status"] = "review" if ok else "gates_failed"
    return out


def _read_json(p: str) -> Any:
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ── the session ──────────────────────────────────────────────────────────────

def main(sid: str) -> int:
    from automation.config import load_env
    load_env()
    from automation.agentic import settings as agent_settings
    from automation.database.config import initialize_database
    initialize_database()

    sdir = paths.session_dir(sid)
    os.makedirs(sdir, exist_ok=True)
    os.makedirs(paths.SESSIONS_DIR, exist_ok=True)
    lock = open(paths.LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _set(sid, status="failed", error="another AI Agent session is running",
             finished_at=datetime.utcnow())
        return 1

    row = _row(sid)
    s = agent_settings.get()
    kind = row["kind"]
    cont = continuing(row)
    _set(sid, status="preparing", pid=os.getpid(),
         **({} if cont and row.get("started_at") else {"started_at": datetime.utcnow()}))
    wt = ""
    result: Dict[str, Any] = {}
    final = "failed"
    try:
        if kind in DEVICE_KINDS:
            phase(sid, sdir, "checking the rig (Appium, simulators, Metro)")
            if not ensure_rig(sdir):
                log(sdir, "rig still not ready after ./start.sh — the session will wait for it")
            if not wait_until_idle(sid, sdir):
                if _stop_requested(sid):
                    final = "stopped"
                    return 0
                raise RuntimeError(f"a run was still on the simulators after {BUSY_WAIT_MIN} minutes")

        from automation.claude_code import gates
        if cont:
            phase(sid, sdir, "continuing in the session's worktree after the interruption")
            wt, base, branch = row["worktree"], row["base_commit"], row["branch"]
            link_runtime(wt)
            before = flows_at(base)
        else:
            phase(sid, sdir, "creating the session's worktree")
            w = make_worktree(sid, kind)
            wt, base, branch = w["worktree"], w["base"], w["branch"]
            _set(sid, worktree=wt, branch=branch, base_commit=base)
            before = gates.dump_flows(wt)

        hours = session_hours(kind, row.get("options") or {}, s)
        deadline = datetime.now() + timedelta(hours=hours)
        ctx = {"session_dir": sdir, "worktree": wt, "branch": branch,
               "date": f"{datetime.now():%Y-%m-%d}",
               "stop_by": f"{deadline - timedelta(minutes=20):%H:%M}",
               "deadline": f"{deadline:%H:%M}", "known_failing_tests": _known_failures()}
        prompt = build_prompt(row, ctx, s)
        resume, fresh = None, ""
        if cont:
            resume = row.get("claude_session_id")
            note = continue_prompt(ctx, wt)
            fresh = prompt + "\n\n## You are continuing an interrupted session\n\n" + note
            prompt = note
        with open(os.path.join(sdir, "prompt_continue.md" if cont else "prompt.md"), "w",
                  encoding="utf-8") as f:
            f.write(prompt)

        _set(sid, status="running")
        phase(sid, sdir, f"Claude Code is working (until {deadline:%H:%M})")
        result = run_claude(sid, sdir, wt, prompt, s, deadline, f"agent {kind} {sid[:8]}",
                            resume=resume, fresh_prompt=fresh)
        _set(sid, result=result)
        if result.get("stopped"):
            stop_flow_children(sdir)
            diff = git(wt, "diff", check=False)
            if diff:
                with open(os.path.join(sdir, "stopped.patch"), "w", encoding="utf-8") as f:
                    f.write(diff)
            final = "stopped"
            return 0

        _set(sid, status="checking")
        h = harvest(sid, sdir, wt, base, branch, before, s)
        summary = _read_json(os.path.join(sdir, "summary.json"))
        if kind in ("analyze", "nightly"):
            fmap = _read_json(os.path.join(sdir, "feature_map.json"))
            if isinstance(fmap, dict):
                summary = dict(summary or {}, feature_map=fmap)
            if not result.get("is_error") and not result.get("timed_out"):
                subprocess.run([paths.PYTHON, paths.TOOLS, "mark-apps"], cwd=paths.MAIN_ROOT,
                               capture_output=True, timeout=300,
                               env=dict(os.environ, PYTHONPATH=paths.MAIN_ROOT))
        with open(os.path.join(sdir, "gates.json"), "w") as f:
            json.dump(h["gates"], f, indent=1)
        _set(sid, files_changed=h["files"], gates=h["gates"], summary=summary)
        final = h["status"]
        if result.get("is_error") and final == "no_changes":
            final = "failed"
            _set(sid, error=(result.get("result") or "Claude Code stopped with an error")[:2000])
        return 0
    except Exception as e:  # noqa: BLE001 — every failure must end the row, not hang it
        log(sdir, f"session failed: {type(e).__name__}: {e}")
        _set(sid, error=f"{type(e).__name__}: {e}"[:2000])
        final = "failed"
        return 1
    finally:
        if wt:
            remove_worktree(wt)
            r = _row(sid)
            if final not in ("review", "gates_failed") and r.get("branch"):
                git(paths.MAIN_ROOT, "branch", "-D", r["branch"], check=False)
                _set(sid, branch=None)
        _set(sid, status=final, finished_at=datetime.utcnow(),
             phase={"review": "waiting for your approval", "gates_failed": "a gate failed — see the checks",
                    "no_changes": "finished — nothing to change", "stopped": "stopped",
                    "failed": "failed"}.get(final, final))
        try:
            from automation.claude_code import report
            report.maybe_send(sid)
        except Exception as e:  # noqa: BLE001
            log(sdir, f"report not sent: {e}")
        with open(os.path.join(sdir, "result.json"), "w") as f:
            json.dump({"status": final, **{k: v for k, v in result.items() if k != "result"}}, f,
                      indent=1, default=str)
        fcntl.flock(lock, fcntl.LOCK_UN)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
