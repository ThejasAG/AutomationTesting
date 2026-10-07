"""Commands Claude Code calls during a nightly run (see scripts/claude_nightly.py).

Every platform action the nightly session takes goes through this one script, so
its permission list allows exactly one Bash command. Output is plain text.

  busy                              is a flow already running on the rig?
  preflight [--wait MIN]            Appium, simulators, staging; waits up to MIN for the rig
  flows                             every flow with its last results
  show-flow FLOW                    the flow's segments and steps as they run now
  run FLOW [--resume RUN] [--label L] [--max SEC]
                                    start one flow and wait for it (one at a time)
  wait RUN [--max SEC]              keep waiting for a run `run` returned early on
  run-info RUN                      segments, errors, notes, rule verdict, screenshot files
  history FLOW                      the flow's last runs and where each failed
  check-flows                       does cross_app_flows.py import, and is every @token handled?
  coverage                          screens / element ids of both apps that no flow drives
  app-changes                       new upstream commits in both app repos since the last night
  app-show APP PATH [--start N --end M]   a file as it is upstream (new code not checked out)
  app-grep APP PATTERN              git grep over the upstream code

Flows run in a detached child process: if the calling command is killed (a Bash
timeout), the flow still finishes with a real status instead of being left
'running'. The app checkouts are only ever read (fetch updates remote refs only).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

STATE_FILE = os.path.join(ROOT, "logs", "claude_nightly", "state.json")
ENV = "staging"
TERMINAL = ("passed", "failed", "error", "stopped", "cancelled", "canceled")
DEFAULT_WAIT_S = 540          # under Claude Code's 10-minute Bash cap


def night_dir() -> str:
    d = os.environ.get("CLAUDE_NIGHTLY_DIR") or os.path.join(ROOT, "logs", "claude_nightly", "manual")
    os.makedirs(d, exist_ok=True)
    return d


def _git(repo: str, *args: str, timeout: int = 60) -> str:
    out = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"git {' '.join(args)} failed")
    return out.stdout


# ── rig ──────────────────────────────────────────────────────────────────────

def active_runs() -> List[Any]:
    """Runs still in progress, ignoring stale rows a crashed process left 'running'."""
    from automation.database.config import SessionLocal
    from automation.database.models import TestRun
    since = datetime.utcnow() - timedelta(minutes=90)
    with SessionLocal() as db:
        rows = (db.query(TestRun).filter(TestRun.status.in_(["running", "queued"]),
                                         TestRun.created_at >= since).all())
        for r in rows:
            db.expunge(r)
    return rows


def cmd_busy(_a) -> int:
    rows = active_runs()
    if not rows:
        print("idle: no flow is running")
        return 0
    for r in rows:
        print(f"busy: run {r.id} '{r.test_name}' is {r.status} (started {r.started_at})")
    return 1


def cmd_preflight(a) -> int:
    from automation.agentic import preflight
    deadline = time.time() + max(0.0, a.wait) * 60
    while True:
        checks = preflight.run(ENV)
        ok = preflight.all_ok(checks)
        if ok or time.time() >= deadline:
            break
        time.sleep(60)
    for c in checks:
        print(f"{'OK  ' if c['ok'] else 'FAIL'} {c['name']}: {c['detail']}")
    print("rig ready" if ok else "rig NOT ready")
    return 0 if ok else 1


# ── flows ────────────────────────────────────────────────────────────────────

def cmd_flows(_a) -> int:
    from automation.scenarios.cross_app_flows import list_flows
    from automation.agentic.triage import _flow_history
    for f in list_flows():
        hist = _flow_history({"flow_id": f["id"]}).splitlines()[:3]
        last = ", ".join(h.split()[2] for h in hist if len(h.split()) > 2) or "never run"
        print(f"{f['id']}  [{len(f['segments'])} segments]  {f['name']}  — last: {last}")
    return 0


def cmd_show_flow(a) -> int:
    from automation.agentic.triage import _get_flow
    print(_get_flow({"flow_id": a.flow}))
    return 0


def cmd_history(a) -> int:
    from automation.agentic.triage import _flow_history
    print(_flow_history({"flow_id": a.flow}))
    return 0


def cmd_check_flows(_a) -> int:
    try:
        from automation.scenarios.cross_app_flows import FLOWS, list_flows
        from automation.agentic.fixer import token_ok
    except Exception as e:  # a syntax error in an edit lands here
        print(f"BROKEN: cross_app_flows.py does not import: {type(e).__name__}: {e}")
        return 1
    problems = []
    for fid, f in FLOWS.items():
        if f.get("id") != fid:
            problems.append(f"{fid}: its 'id' field says {f.get('id')!r}")
        for s in f.get("segments") or []:
            if s.get("role") not in ("consumer", "waiter", "kitchen"):
                problems.append(f"{fid} segment {s.get('num')}: unknown role {s.get('role')!r}")
            for st in s.get("steps") or []:
                if not token_ok(st):
                    problems.append(f"{fid} segment {s.get('num')}: no handler for {st!r}")
    if problems:
        print("PROBLEMS:\n" + "\n".join("  " + p for p in problems))
        return 1
    print(f"OK: {len(list_flows())} flows import and every @token has a handler")
    return 0


# ── running a flow ───────────────────────────────────────────────────────────

def _meta_path(run_id: str) -> str:
    return os.path.join(night_dir(), "runs", f"{run_id}.json")


def _child(a) -> int:
    """Runs in the detached process: start the flow, publish its run id, wait."""
    from automation.agentic import orchestrator
    from automation.database.config import SessionLocal
    from automation.database.models import ScenarioResult
    from automation.scenarios.cross_app_flows import list_flows, resume_plan, start_flow_run
    plan = None
    if a.resume:
        flow = next((f for f in list_flows() if f["id"] == a.flow), None)
        with SessionLocal() as db:
            rows = db.query(ScenarioResult).filter(ScenarioResult.run_id == a.resume).all()
            for r in rows:
                db.expunge(r)
        try:
            plan = resume_plan(flow, rows) if flow else None
            if plan is not None:
                plan["from_run"] = a.resume
        except ValueError:
            plan = None
    rid = start_flow_run(a.flow, env=ENV, resume=plan)
    with open(a.id_file, "w") as f:
        f.write(rid)
    orchestrator._wait_for(rid)
    return 0


def _status_of(run_id: str) -> str:
    from automation.database.config import SessionLocal
    from automation.database.models import TestRun
    with SessionLocal() as db:
        r = db.get(TestRun, run_id)
        return (r.status or "") if r else "missing"


def _finish(run_id: str) -> None:
    """Record the attempt (runs.jsonl is the report's ground truth) and summarise it."""
    from automation.agentic import classifier, orchestrator
    meta = json.load(open(_meta_path(run_id)))
    run, rows = orchestrator._load(run_id)
    status = (run.status if run else "missing") or ""
    verdict = classifier.classify(run, rows) if status != "passed" else {}
    rec = {"flow_id": meta["flow_id"], "label": meta["label"], "run_id": run_id,
           "resumed_from": meta.get("resume"), "status": status,
           "category": verdict.get("category"), "failed_segment": verdict.get("failed_segment"),
           "failed_step": verdict.get("failed_step"), "summary": verdict.get("summary"),
           "started": meta["started"], "finished": datetime.now().isoformat(timespec="seconds"),
           "seconds": int(time.time() - meta["t0"])}
    with open(os.path.join(night_dir(), "runs.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"RUN {run_id} {meta['flow_id']} -> {status.upper()} after {rec['seconds']}s")
    if status != "passed":
        print(f"rule sorter: {verdict.get('category')} (matched: {verdict.get('matched') or '-'})")
        print(f"failed segment {verdict.get('failed_segment')} "
              f"{verdict.get('failed_segment_name') or ''}, step {verdict.get('failed_step') or '-'}")
        print(f"error: {verdict.get('summary') or '-'}")
        print(f"next: run-info {run_id}")


def _wait(run_id: str, max_s: int) -> int:
    t_end = time.time() + max_s
    while time.time() < t_end:
        st = _status_of(run_id)
        if st in TERMINAL:
            _finish(run_id)
            return 0 if st == "passed" else 2
        time.sleep(10)
    print(f"STILL RUNNING: run {run_id} ({_status_of(run_id)}). Call: wait {run_id}")
    return 3


def cmd_run(a) -> int:
    busy = active_runs()
    if busy:
        print(f"REFUSED: run {busy[0].id} '{busy[0].test_name}' is still {busy[0].status}. "
              f"Finish it first (wait {busy[0].id}).")
        return 1
    from automation.scenarios.cross_app_flows import resolve_flow
    if not resolve_flow(a.flow):
        print(f"REFUSED: no flow {a.flow!r} (see: flows)")
        return 1
    runs_dir = os.path.join(night_dir(), "runs")
    os.makedirs(runs_dir, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    id_file = os.path.join(runs_dir, f"{a.flow}-{stamp}.id")
    log = open(os.path.join(runs_dir, f"{a.flow}-{stamp}.log"), "w")
    cmd = [sys.executable, os.path.abspath(__file__), "_child", a.flow, "--id-file", id_file]
    if a.resume:
        cmd += ["--resume", a.resume]
    env = dict(os.environ, PYTHONPATH=ROOT)
    t0 = time.time()
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=env,
                            start_new_session=True)
    while not os.path.exists(id_file):
        if proc.poll() is not None:
            print(f"FAILED TO START: see {log.name}")
            return 1
        if time.time() - t0 > 120:
            print(f"FAILED TO START within 120s: see {log.name}")
            return 1
        time.sleep(1)
    rid = open(id_file).read().strip()
    with open(_meta_path(rid), "w") as f:
        json.dump({"flow_id": a.flow, "label": a.label, "resume": a.resume, "t0": t0,
                   "started": datetime.now().isoformat(timespec="seconds")}, f)
    print(f"started run {rid} of {a.flow}" + (f" (resuming {a.resume})" if a.resume else ""))
    return _wait(rid, a.max)


def cmd_wait(a) -> int:
    if not os.path.exists(_meta_path(a.run_id)):
        print(f"unknown run {a.run_id} (only runs started with `run` tonight can be waited on)")
        return 1
    return _wait(a.run_id, a.max)


def cmd_run_info(a) -> int:
    from automation.agentic import classifier, orchestrator
    from automation.agentic.triage import _get_run
    print(_get_run({"run_id": a.run_id}))
    run, rows = orchestrator._load(a.run_id)
    v = classifier.classify(run, rows)
    print(f"\nrule sorter: {v['category']} (matched: {v['matched'] or '-'})")
    shots = os.path.join(night_dir(), "shots")
    os.makedirs(shots, exist_ok=True)
    for r in rows:
        m = re.match(r"data:image/([a-z]+);base64,(.*)", r.screenshot or "", re.S)
        if not m:
            continue
        path = os.path.join(shots, f"{a.run_id[:8]}-seg{r.scenario_num}.{m.group(1).replace('jpeg', 'jpg')}")
        with open(path, "wb") as f:
            f.write(base64.b64decode(m.group(2)))
        print(f"screenshot of segment {r.scenario_num} ({r.status}): {path}")
    return 0


# ── the apps ─────────────────────────────────────────────────────────────────

def cmd_coverage(_a) -> int:
    from automation.agentic import inventory
    inv = inventory.build()
    for app, d in inv["apps"].items():
        print(f"{app}: {d['screens_covered']}/{d['screens']} screens, "
              f"{d['ids_covered']}/{d['ids']} element ids driven by a flow")
    print()
    print(inventory.compact(inv))
    return 0


def _upstream(repo: str) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "@{u}").strip()


def _repo(app: str) -> str:
    from automation.agentic import sources
    repo = sources.app_repo(app)
    if not repo:
        raise SystemExit(f"no checkout of the {app} app under repos/")
    return repo


def load_state() -> Dict[str, Any]:
    try:
        return json.load(open(STATE_FILE))
    except (OSError, ValueError):
        return {}


def mark_apps() -> Dict[str, str]:
    """Remember each app's upstream head, so the next night shows only newer commits."""
    state = load_state()
    seen = state.setdefault("apps", {})
    for app in ("consumer", "business"):
        try:
            repo = _repo(app)
            seen[app] = _git(repo, "rev-parse", _upstream(repo)).strip()
        except (SystemExit, RuntimeError, subprocess.SubprocessError):
            continue
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    json.dump(state, open(STATE_FILE, "w"), indent=1)
    return seen


def cmd_app_changes(_a) -> int:
    seen = load_state().get("apps", {})
    for app in ("consumer", "business"):
        print(f"\n=== {app} app ===")
        try:
            repo = _repo(app)
        except SystemExit as e:
            print(e)
            continue
        try:
            _git(repo, "fetch", "--quiet", "origin", timeout=120)
        except (RuntimeError, subprocess.SubprocessError) as e:
            print(f"(fetch failed, showing what is already known: {e})")
        try:
            up = _upstream(repo)
        except RuntimeError as e:
            print(f"no upstream branch: {e}")
            continue
        last = seen.get(app)
        rng = f"{last}..{up}" if last else up
        args = ["log", "--no-merges", "--stat=120", "--format=%n%h %ad %an%n    %s", "--date=short", rng]
        if not last:
            args.insert(1, "--since=14.days")
            print(f"(first night: last 14 days of {up})")
        out = _git(repo, *args).strip()
        print(f"upstream {up}; {'since ' + last[:8] if last else ''}")
        print(out[:20000] or "no new commits")
    return 0


def cmd_app_show(a) -> int:
    repo = _repo(a.app)
    text = _git(repo, "show", f"{_upstream(repo)}:{a.path}")
    lines = text.splitlines()
    end = min(len(lines), a.end or a.start + 299)
    for i in range(a.start, end + 1):
        print(f"{i:5}  {lines[i - 1]}")
    if end < len(lines):
        print(f"… {len(lines) - end} more lines (use --start {end + 1})")
    return 0


def cmd_app_grep(a) -> int:
    repo = _repo(a.app)
    out = subprocess.run(["git", "-C", repo, "grep", "-n", "-E", a.pattern, _upstream(repo), "--", "App"],
                         capture_output=True, text=True, timeout=60).stdout
    lines = out.splitlines()
    print("\n".join(lines[:150]) or "no matches")
    if len(lines) > 150:
        print(f"… {len(lines) - 150} more matches; narrow the pattern")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("busy").set_defaults(fn=cmd_busy)
    s = sub.add_parser("preflight")
    s.add_argument("--wait", type=float, default=0)
    s.set_defaults(fn=cmd_preflight)
    sub.add_parser("flows").set_defaults(fn=cmd_flows)
    for name, fn in (("show-flow", cmd_show_flow), ("history", cmd_history)):
        s = sub.add_parser(name)
        s.add_argument("flow")
        s.set_defaults(fn=fn)
    s = sub.add_parser("run")
    s.add_argument("flow")
    s.add_argument("--resume")
    s.add_argument("--label", default="run")
    s.add_argument("--max", type=int, default=DEFAULT_WAIT_S)
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("wait")
    s.add_argument("run_id")
    s.add_argument("--max", type=int, default=DEFAULT_WAIT_S)
    s.set_defaults(fn=cmd_wait)
    s = sub.add_parser("run-info")
    s.add_argument("run_id")
    s.set_defaults(fn=cmd_run_info)
    sub.add_parser("check-flows").set_defaults(fn=cmd_check_flows)
    sub.add_parser("coverage").set_defaults(fn=cmd_coverage)
    sub.add_parser("app-changes").set_defaults(fn=cmd_app_changes)
    s = sub.add_parser("app-show")
    s.add_argument("app", choices=["consumer", "business"])
    s.add_argument("path")
    s.add_argument("--start", type=int, default=1)
    s.add_argument("--end", type=int, default=0)
    s.set_defaults(fn=cmd_app_show)
    s = sub.add_parser("app-grep")
    s.add_argument("app", choices=["consumer", "business"])
    s.add_argument("pattern")
    s.set_defaults(fn=cmd_app_grep)
    s = sub.add_parser("_child")
    s.add_argument("flow")
    s.add_argument("--id-file", required=True)
    s.add_argument("--resume")
    s.set_defaults(fn=_child)
    a = p.parse_args(argv)
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
