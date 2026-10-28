"""The commands the AI Agent (Claude Code) uses to work on the rig.

Every platform action an agent session takes goes through this one script, so its
permission rules allow exactly one Bash command. A person can use it too, from the
project folder. Output is plain text.

  busy                              is a flow running on the simulators?
  preflight [--wait MIN]            Appium, simulators, staging; waits up to MIN for the rig
  flows                             every flow with its last results
  green [--hours H]                 flows that passed in the last H h (8) on unchanged flow code — skip them
  show-flow FLOW                    the flow's segments and steps as they run now
  steps                             every step form, @token (+ its handler) and shared block
  history FLOW                      the flow's last runs and where each failed
  run FLOW [--resume RUN] [--label L] [--max SEC]
                                    run one flow on the simulators, with THIS checkout's
                                    code, and wait for it (one at a time)
  wait RUN [--max SEC]              keep waiting for a run `run` returned early on
  run-info RUN                      segments, errors, notes, rule verdict, screenshot files
  check-flows                       does cross_app_flows.py import, is every @token handled?
  check-ids FLOW                    is every id the flow taps/types into in the app source?
  impacted FUNC [FUNC ...]          which flows reach these engine functions
  unit-tests [PATH ...]             pytest (default: every test) — new failures vs known ones
  coverage                          screens / element ids of both apps that no flow drives
  apps                              which checkout under repos/ is which app
  app-changes                       new upstream commits in both app repos since the last analysis
  app-show APP PATH [--start N --end M]   a file as it is upstream (new code not checked out)
  app-grep APP PATTERN              git grep over the upstream code
  screen consumer|waiter|kitchen    what the simulator shows now: element list + screenshot
  recover wda|rig                   restart WebDriverAgent on both simulators / start the rig

Flows run in a detached child process: if the calling command is killed (a Bash
timeout), the flow still finishes with a real status instead of being left
'running'. A Stop pressed on the run in the dashboard, or the session being
stopped, ends it cleanly. The app checkouts are only ever read.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Whose code runs: this checkout's, unless told otherwise (the session gates check a
# worktree's code with the main checkout's copy of this script).
CODE_ROOT = os.environ.get("AGENT_CODE_ROOT") or ROOT
MAIN_ROOT = os.environ.get("AGENT_MAIN_ROOT") or ROOT
if CODE_ROOT not in sys.path:
    sys.path.insert(0, CODE_ROOT)

STATE_FILE = os.path.join(MAIN_ROOT, "logs", "agent_sessions", "state.json")
ENV = "staging"
TERMINAL = ("passed", "failed", "error", "stopped", "cancelled", "canceled")
DEFAULT_WAIT_S = 540          # under Claude Code's 10-minute Bash cap
ROLE_DEVICE = {"consumer": "consumer", "waiter": "waiter", "kitchen": "kitchen"}


def session_dir() -> str:
    d = (os.environ.get("AGENT_SESSION_DIR")
         or os.path.join(MAIN_ROOT, "logs", "agent_sessions", "manual"))
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
        who = "this session" if (r.triggered_by or "").startswith("claude-agent") else "a person"
        print(f"busy: run {r.id} '{r.test_name}' is {r.status} (started {r.started_at}, by {who})")
    print("wait a minute and call `busy` again; never interrupt a person's run")
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
    from automation.agentic import runinfo
    from automation.scenarios.cross_app_flows import list_flows
    for f in list_flows():
        hist = runinfo.flow_history(f["id"], limit=3).splitlines()
        last = ", ".join(h.split()[2] for h in hist if len(h.split()) > 2) or "never run"
        print(f"{f['id']}  [{len(f['segments'])} segments]  {f['name']}  — last: {last}")
    return 0


def cmd_green(a) -> int:
    """Flows already proven: their latest run passed within the last `--hours` AND after
    the newest commit to the flow code, so running them again would only repeat it.
    A run-all skips these (a full pass, when asked for, runs everything)."""
    import calendar
    from automation.database.config import SessionLocal
    from automation.database.models import TestRun
    from automation.scenarios.cross_app_flows import list_flows
    try:
        code_ts = int(_git(CODE_ROOT, "log", "-1", "--format=%ct", "--", "automation").strip() or 0)
    except (RuntimeError, subprocess.SubprocessError, ValueError):
        code_ts = int(time.time())          # unknown → nothing counts as proven
    since = max(time.time() - a.hours * 3600, code_ts)
    green, rest = [], []
    with SessionLocal() as db:
        for f in list_flows():
            last = (db.query(TestRun)
                    .filter(TestRun.bot_type == "ios-crossapp-flow",
                            TestRun.test_name.like(f"%{f['name']}"),
                            TestRun.status.in_(TERMINAL))
                    .order_by(TestRun.created_at.desc()).first())
            done = last.completed_at if last else None
            if last and last.status == "passed" and done and calendar.timegm(done.timetuple()) >= since:
                ago = (time.time() - calendar.timegm(done.timetuple())) / 3600
                green.append(f"{f['id']}  passed {ago:.1f} h ago (run {last.id[:8]})")
            else:
                rest.append(f["id"])
    print(f"Proven (passed in the last {a.hours:g} h, after the last change to the flow code) — "
          f"skip these and report them as passed earlier:")
    print("\n".join("  " + g for g in green) or "  none")
    print(f"Run these: {' '.join(rest) or 'none'}")
    return 0


def cmd_show_flow(a) -> int:
    from automation.agentic import runinfo
    print(runinfo.flow_text(a.flow))
    return 0


_BLOCK_ROLE = {"_C_": "consumer", "_W_": "waiter", "_K_": "kitchen"}


def _token_methods(src: str) -> Dict[str, str]:
    """{'@token' or '@prefix:': the FlowRunner method(s) its branch returns}, read
    from _handle_special's source; '(inline)' when the branch does the work itself."""
    lines = src.splitlines()
    found: Dict[str, List[str]] = {}
    for i, line in enumerate(lines):
        toks = re.findall(r'"(@[a-z_]+:?)"', line)
        if not toks:
            continue
        body = []
        for nxt in lines[i + 1:]:
            if nxt.strip().startswith("if step"):
                break
            body.append(nxt)
        names = re.findall(r"return self\.(_\w+)\(", "\n".join(body))
        for tok in toks:
            found.setdefault(tok, [])
            found[tok] += [n for n in names if n not in found[tok]]
    return {tok: " / ".join(names) or "(inline)" for tok, names in found.items()}


def _block_users(block: List[str], flows: Dict[str, Any]) -> List[str]:
    n = len(block)
    return [fid for fid, f in flows.items()
            if any(s["steps"][i:i + n] == block for s in f.get("segments") or []
                   for i in range(len(s["steps"]) - n + 1))]


def cmd_steps(_a) -> int:
    """What a flow step can be — read from the engine itself, so it is never stale:
    the step forms, every @token with the handler method behind it, the plain steps
    that are rewritten to a token, and the shared step blocks with the flows using
    them. MEASURED 2026-10-27: a 'Create scenarios' session spent 23 of its 57
    minutes in helper agents rediscovering exactly this from the 10k-line file."""
    import inspect
    from automation.agentic.runinfo import known_tokens
    from automation.scenarios import cross_app_flows as caf
    print("STEP FORMS — one string per step, run in order; a failing step ends the segment")
    for row in caf.STEP_CATALOG.get("Common", []):
        print(f"  {row['step']:<34} {row['help']}")
    print(f"  {'?<step>':<34} optional: done when its target shows within ~6s, else skipped")
    exact, prefixes = known_tokens()
    methods = _token_methods(inspect.getsource(caf.FlowRunner._handle_special))
    helps = {}
    for rows in caf.STEP_CATALOG.values():
        for row in rows:
            tok = row["step"].split(":", 1)[0] + (":" if ":" in row["step"] else "")
            helps.setdefault(tok, (row["step"], row["help"]))
    print("\n@TOKENS — `_handle_special` dispatches these (method = the FlowRunner handler)")
    for tok in sorted(exact | prefixes):
        shown, help_ = helps.get(tok, (tok + ("<arg>" if tok.endswith(":") else ""), ""))
        print(f"  {shown:<34} {methods.get(tok, '?'):<26} {help_}")
    print("\nPLAIN STEPS rewritten to a token before they run")
    for plain, tok in caf.FlowRunner._PLAIN_STEP_TOKENS.items():
        print(f"  {plain:<34} -> {tok}")
    print("\nSHARED STEP BLOCKS — reuse them (`_C_BOOK_PREFIX + [...]`); never edit one for a new flow")
    flows = caf.FLOWS
    for name, block in vars(caf).items():
        role = next((r for p, r in _BLOCK_ROLE.items() if name.startswith(p)), None)
        if not role or not isinstance(block, list) or not block or \
                not all(isinstance(s, str) for s in block):
            continue
        users = _block_users(block, flows)
        print(f"\n  {name}  ({role}; used by {len(users)} flow(s): {', '.join(users[:8]) or '-'})")
        for st in block:
            print(f"      {st}")
    return 0


def cmd_history(a) -> int:
    from automation.agentic import runinfo
    print(runinfo.flow_history(a.flow))
    return 0


def cmd_check_flows(_a) -> int:
    try:
        from automation.scenarios.cross_app_flows import FLOWS, list_flows
        from automation.agentic.runinfo import token_ok
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


_ID_STEP = re.compile(r"^\??(?:click|tap|select|long press|wait for|verify|scroll to)\s+(.+?)\s*$", re.I)
_TYPE_STEP = re.compile(r"^type\s+.+?\s+in\s+(\S+)\s*$", re.I)
_SRC_EXT = (".js", ".jsx", ".ts", ".tsx")


def _app_text() -> Dict[str, str]:
    from automation.agentic import sources
    out = {}
    for app in ("consumer", "business"):
        repo = sources.app_repo(app)
        if not repo:
            continue
        parts = []
        for base, dirs, files in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "ios", "android", "Pods",
                                                    "build", "dist", "__tests__")]
            for n in files:
                if n.endswith(_SRC_EXT):
                    try:
                        with open(os.path.join(base, n), encoding="utf-8", errors="ignore") as f:
                            parts.append(f.read())
                    except OSError:
                        pass
        out[app] = "\n".join(parts)
    return out


def cmd_check_ids(a) -> int:
    from automation.scenarios.cross_app_flows import resolve_flow
    flow = resolve_flow(a.flow)
    if not flow:
        print(f"no flow {a.flow!r}")
        return 1
    kn = os.path.join(CODE_ROOT, "automation", "knowledge")
    known = ""
    for n in ("locator_map.md", "learned_locators.json"):
        try:
            with open(os.path.join(kn, n), encoding="utf-8") as f:
                known += f.read()
        except OSError:
            pass
    texts = _app_text()
    unknown = 0
    for s in flow.get("segments") or []:
        for st in s.get("steps") or []:
            if st.startswith("@"):
                continue
            m = _TYPE_STEP.match(st) or _ID_STEP.match(st)
            if not m:
                continue
            ident = m.group(1).strip().strip("'\"")
            where = []
            for app, t in texts.items():
                if re.search(r"(?:testID|accessibilityLabel)\s*=\s*\{?\s*[\"']" + re.escape(ident) + r"[\"']", t):
                    where.append(f"{app}: testID")
                elif re.search(r"[\"'>`]" + re.escape(ident) + r"[\"'<`]", t):
                    where.append(f"{app}: text/literal")
            if ident in known:
                where.append("locator_map")
            if not where:
                unknown += 1
            print(f"{'OK     ' if where else 'UNKNOWN'} [{s.get('role')}] {st!r} -> {ident!r}"
                  + (f"  ({', '.join(where)})" if where else
                     "  — not in the app source or locator_map: a template id (like <User>Card) "
                     "or invented? Check the source."))
    print(f"\n{unknown} id(s) not found literally" if unknown else "\nevery id was found")
    return 1 if unknown else 0


def cmd_impacted(a) -> int:
    from automation.claude_code import impact
    from automation.scenarios.cross_app_flows import FLOWS, FlowRunner
    path = os.path.join(CODE_ROOT, impact.FLOWS_FILE)
    with open(path, encoding="utf-8") as f:
        src = f.read()
    flows = {k: [st for seg in (v.get("segments") or []) for st in (seg.get("steps") or [])]
             for k, v in FLOWS.items()}
    aliases = dict(getattr(FlowRunner, "_PLAIN_STEP_TOKENS", {}))
    for fn in a.funcs:
        hit = impact.flows_for_functions(flows, src, [fn], aliases)
        if hit == [impact.ALL]:
            print(f"{fn}: engine core — every flow depends on it (any flow passing counts)")
        elif not hit:
            print(f"{fn}: no flow reaches it (unknown name, or not used by any token handler)")
        else:
            print(f"{fn}: {', '.join(hit)}")
    return 0


def cmd_unit_tests(a) -> int:
    from automation.claude_code import baselines, gates
    res = gates.run_unit_tests(CODE_ROOT, a.paths or None, timeout=1500)
    print(res["tail"])
    known = set((baselines.latest() or {}).get("failed") or [])
    new = [t for t in res["failed"] if t not in known]
    if res["failed"]:
        print("\nfailing: " + ", ".join(res["failed"]))
        print("NEW (not failing on the base commit): " + (", ".join(new) or "none"))
    return 0 if not new and res.get("returncode") in (0, 1) else 1


# ── running a flow ───────────────────────────────────────────────────────────

def _meta_path(run_id: str) -> str:
    return os.path.join(session_dir(), "runs", f"{run_id}.json")


def _child(a) -> int:
    """Runs in the detached process: start the flow, publish its run id, wait.
    SIGTERM (the session being stopped) and a Stop pressed on the run in the
    dashboard both become the flow runner's own stop."""
    from automation.agentic import runinfo
    from automation.database.config import SessionLocal
    from automation.database.models import ScenarioResult, TestRun
    from automation.scenarios.cross_app_flows import (list_flows, request_flow_stop, resume_plan,
                                                      start_flow_run)
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
    try:        # what this run executes, for the session's on-device gate
        from automation.claude_code import fingerprint
        from automation.scenarios.cross_app_flows import resolve_flow
        fp = fingerprint.take(CODE_ROOT, (resolve_flow(a.flow) or {}).get("segments") or [],
                              start=(plan or {}).get("start_at") or 0,
                              resumed_from=a.resume if plan else None)
    except Exception:
        fp = None
    rid = start_flow_run(a.flow, env=ENV, resume=plan)
    if fp is not None:
        try:
            fingerprint.record(rid, fp)
        except OSError:
            pass
    tag = "claude-agent:" + (os.environ.get("AGENT_SESSION_ID") or "manual")[:36]
    with SessionLocal() as db:
        r = db.get(TestRun, rid)
        if r is not None:
            r.triggered_by = tag
            db.commit()
    signal.signal(signal.SIGTERM, lambda *_: request_flow_stop(rid))

    def watch_db_stop():
        while True:
            st = runinfo.status_of(rid)
            if st in ("stopped", "cancelled"):
                request_flow_stop(rid)
                return
            if st in TERMINAL:
                return
            time.sleep(5)

    threading.Thread(target=watch_db_stop, daemon=True).start()
    with open(a.id_file, "w") as f:
        f.write(rid)
    runinfo.wait_for(rid)
    return 0


def _session_runs() -> List[Dict[str, Any]]:
    out = []
    try:
        for line in open(os.path.join(session_dir(), "runs.jsonl"), encoding="utf-8"):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def _max_runs_per_flow() -> int:
    """first + retry + the fix attempts + final. A flow that has not settled by then
    only burns simulator time (one draft took 6 runs / 36 min on 2026-10-28)."""
    try:
        from automation.agentic import settings
        fixes = int(settings.get().get("max_fix_attempts") or 2)
    except Exception:
        fixes = 2
    return fixes + 3


def _finish(run_id: str) -> None:
    """Record the attempt (runs.jsonl is the ledger the session's gates read) and summarise it."""
    from automation.agentic import classifier, runinfo
    meta = json.load(open(_meta_path(run_id)))
    run, rows = runinfo.load(run_id)
    status = (run.status if run else "missing") or ""
    verdict = classifier.classify(run, rows) if status != "passed" else {}
    rec = {"flow_id": meta["flow_id"], "label": meta["label"], "run_id": run_id,
           "resumed_from": meta.get("resume"), "status": status, "code_root": CODE_ROOT,
           "category": verdict.get("category"), "failed_segment": verdict.get("failed_segment"),
           "failed_step": verdict.get("failed_step"), "summary": verdict.get("summary"),
           "started": meta["started"], "t0": meta["t0"],
           "finished": datetime.now().isoformat(timespec="seconds"),
           "seconds": int(time.time() - meta["t0"])}
    with open(os.path.join(session_dir(), "runs.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"RUN {run_id} {meta['flow_id']} -> {status.upper()} after {rec['seconds']}s")
    if status != "passed":
        print(f"rule sorter: {verdict.get('category')} (matched: {verdict.get('matched') or '-'})")
        print(f"failed segment {verdict.get('failed_segment')} "
              f"{verdict.get('failed_segment_name') or ''}, step {verdict.get('failed_step') or '-'}")
        print(f"error: {verdict.get('summary') or '-'}")
        print(f"next: run-info {run_id}")


def _wait(run_id: str, max_s: int) -> int:
    from automation.agentic.runinfo import status_of
    t_end = time.time() + max_s
    while time.time() < t_end:
        st = status_of(run_id)
        if st in TERMINAL:
            _finish(run_id)
            return 0 if st == "passed" else 2
        time.sleep(10)
    print(f"STILL RUNNING: run {run_id} ({status_of(run_id)}). Call: wait {run_id}")
    return 3


def cmd_run(a) -> int:
    busy = active_runs()
    if busy:
        print(f"REFUSED: run {busy[0].id} '{busy[0].test_name}' is still {busy[0].status}. "
              f"Wait with `busy` until it ends (never interrupt a person's run).")
        return 1
    from automation.scenarios.cross_app_flows import resolve_flow
    if not resolve_flow(a.flow):
        print(f"REFUSED: no flow {a.flow!r} (see: flows)")
        return 1
    limit = _max_runs_per_flow()
    tried = sum(1 for r in _session_runs() if r.get("flow_id") == a.flow)
    if tried >= limit:
        print(f"REFUSED: {a.flow} already ran {tried} times in this session (limit {limit}: first, "
              f"retry, the fix attempts, final). Stop on it: report a regression flow as "
              f"needs_human, drop a draft — with what the runs showed.")
        return 1
    runs_dir = os.path.join(session_dir(), "runs")
    os.makedirs(runs_dir, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    id_file = os.path.join(runs_dir, f"{a.flow}-{stamp}.id")
    log = open(os.path.join(runs_dir, f"{a.flow}-{stamp}.log"), "w")
    cmd = [sys.executable, os.path.abspath(__file__), "_child", a.flow, "--id-file", id_file]
    if a.resume:
        cmd += ["--resume", a.resume]
    env = dict(os.environ, PYTHONPATH=CODE_ROOT)
    t0 = time.time()
    proc = subprocess.Popen(cmd, cwd=CODE_ROOT, stdout=log, stderr=subprocess.STDOUT, env=env,
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
        json.dump({"flow_id": a.flow, "label": a.label, "resume": a.resume, "t0": t0, "pid": proc.pid,
                   "started": datetime.now().isoformat(timespec="seconds")}, f)
    print(f"started run {rid} of {a.flow}" + (f" (resuming {a.resume})" if a.resume else ""))
    return _wait(rid, a.max)


def cmd_wait(a) -> int:
    if not os.path.exists(_meta_path(a.run_id)):
        print(f"unknown run {a.run_id} (only runs started with `run` in this session can be waited on)")
        return 1
    return _wait(a.run_id, a.max)


def cmd_run_info(a) -> int:
    from automation.agentic import classifier, runinfo
    print(runinfo.run_text(a.run_id))
    run, rows = runinfo.load(a.run_id)
    v = classifier.classify(run, rows)
    print(f"\nrule sorter: {v['category']} (matched: {v['matched'] or '-'})")
    shots = os.path.join(session_dir(), "shots")
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


def cmd_apps(_a) -> int:
    from automation.agentic import sources
    for app in ("consumer", "business"):
        repo = sources.app_repo(app)
        print(f"{app}: {os.path.relpath(repo, CODE_ROOT) if repo else 'no checkout under repos/'}")
    print("The business app has two roles on the iPad: waiter (bookings, tables, orders, payment) "
          "and kitchen (marks orders ready).")
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
    """Remember each app's upstream head, so the next analysis shows only newer commits."""
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


def cmd_mark_apps(_a) -> int:
    print(json.dumps(mark_apps()))
    return 0


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
            print(f"(first analysis: last 14 days of {up})")
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


# ── the simulators ───────────────────────────────────────────────────────────

def _udid(role: str) -> str:
    from automation.scenarios import cross_app_config as cfgmod
    devices = (cfgmod.load_config() or {}).get("devices") or {}
    udid = devices.get(ROLE_DEVICE[role]) or (devices.get("waiter") if role == "kitchen" else "")
    if not udid:
        raise SystemExit(f"no simulator configured for the {role} role")
    return udid


def cmd_screen(a) -> int:
    busy = active_runs()
    if busy:
        print(f"REFUSED: run {busy[0].id[:8]} is on the simulators — reading the screen now could "
              f"disturb it. Wait with `busy`.")
        return 1
    udid = _udid(a.role)
    idb = shutil.which("idb") or "/usr/local/bin/idb"
    out = subprocess.run([idb, "ui", "describe-all", "--udid", udid, "--json"], capture_output=True,
                         text=True, timeout=60)
    try:
        els = json.loads(out.stdout or "[]")
    except ValueError:
        els = []
    shown = 0
    for e in els:
        label = (e.get("AXLabel") or "").strip()
        ident = (e.get("AXIdentifier") or "").strip()
        if not (label or ident):
            continue
        fr = e.get("frame") or {}
        print(f"{e.get('type', ''):14} id={ident or '-':32} label={label[:60]!r} "
              f"at ({int(fr.get('x', 0))},{int(fr.get('y', 0))}) {int(fr.get('width', 0))}x{int(fr.get('height', 0))}")
        shown += 1
        if shown >= 250:
            print("… more elements omitted")
            break
    if not els:
        print(f"idb returned no elements ({(out.stderr or '').strip()[:200]})")
    shot = os.path.join(session_dir(), "shots", f"screen-{a.role}-{datetime.now():%H%M%S}.png")
    os.makedirs(os.path.dirname(shot), exist_ok=True)
    subprocess.run(["xcrun", "simctl", "io", udid, "screenshot", shot], capture_output=True, timeout=60)
    if os.path.exists(shot):
        print(f"screenshot: {shot}")
    print("(idb coordinates are content-space; in scroll views they are not screen positions)")
    return 0


def cmd_recover(a) -> int:
    busy = active_runs()
    if busy:
        print(f"REFUSED: run {busy[0].id[:8]} is on the simulators. Wait with `busy`.")
        return 1
    if a.what == "wda":
        for role in ("consumer", "waiter"):
            udid = _udid(role)
            subprocess.run(["xcrun", "simctl", "terminate", udid, "com.facebook.WebDriverAgentRunner.xctrunner"],
                           capture_output=True, timeout=60)
            print(f"WebDriverAgent stopped on the {role} simulator ({udid[:8]}); the next run starts it again")
        return 0
    with open(os.path.join(session_dir(), "start.log"), "a") as f:
        subprocess.run(["./start.sh"], cwd=MAIN_ROOT, stdout=f, stderr=subprocess.STDOUT, timeout=1200)
    print("./start.sh done — now: preflight")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("busy").set_defaults(fn=cmd_busy)
    s = sub.add_parser("preflight")
    s.add_argument("--wait", type=float, default=0)
    s.set_defaults(fn=cmd_preflight)
    sub.add_parser("flows").set_defaults(fn=cmd_flows)
    s = sub.add_parser("green")
    s.add_argument("--hours", type=float, default=8)
    s.set_defaults(fn=cmd_green)
    sub.add_parser("steps").set_defaults(fn=cmd_steps)
    for name, fn in (("show-flow", cmd_show_flow), ("history", cmd_history), ("check-ids", cmd_check_ids)):
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
    s = sub.add_parser("impacted")
    s.add_argument("funcs", nargs="+")
    s.set_defaults(fn=cmd_impacted)
    s = sub.add_parser("unit-tests")
    s.add_argument("paths", nargs="*")
    s.set_defaults(fn=cmd_unit_tests)
    sub.add_parser("coverage").set_defaults(fn=cmd_coverage)
    sub.add_parser("apps").set_defaults(fn=cmd_apps)
    sub.add_parser("app-changes").set_defaults(fn=cmd_app_changes)
    sub.add_parser("mark-apps").set_defaults(fn=cmd_mark_apps)
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
    s = sub.add_parser("screen")
    s.add_argument("role", choices=["consumer", "waiter", "kitchen"])
    s.set_defaults(fn=cmd_screen)
    s = sub.add_parser("recover")
    s.add_argument("what", choices=["wda", "rig"])
    s.set_defaults(fn=cmd_recover)
    s = sub.add_parser("_child")
    s.add_argument("flow")
    s.add_argument("--id-file", required=True)
    s.add_argument("--resume")
    s.set_defaults(fn=_child)
    a = p.parse_args(argv)
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
