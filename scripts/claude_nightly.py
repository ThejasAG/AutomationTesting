"""Nightly regression run driven by Claude Code, on the Claude plan (no API key).

  scripts/claude_nightly.sh [--mode auto|delta|full]   one full night, started now
  .venv/bin/python scripts/claude_nightly.py merge [BRANCH]   approve a night's fixes
  .venv/bin/python scripts/claude_nightly.py status    the last nights at a glance

What one night does:
 1. makes sure the rig is up (runs ./start.sh if Appium or a simulator is down) and
    waits for any flow already running;
 2. snapshots automation/scenarios/cross_app_flows.py, the one file the session may edit;
 3. runs `claude -p` with automation/claude_code/nightly_prompt.md: it runs every flow,
    retries, diagnoses and fixes failures, verifies each fix by re-running, and drafts
    new scenarios for app features no flow covers (full analysis on Sundays);
 4. moves the session's edits onto a review branch `claude-nightly/<date>` and puts the
    working tree back as it was, so nothing changes until `merge` is run;
 5. emails + Slacks the report (SMTP_* / SLACK_WEBHOOK_URL in .env).

ANTHROPIC_API_KEY is removed from the session's environment, so Claude Code always
uses the logged-in Claude subscription. The session may run only
scripts/claude_nightly_tools.py, edit only cross_app_flows.py and write only in the
night's log folder (`session_settings`). Logs: logs/claude_nightly/<date>/.
"""
from __future__ import annotations

import argparse
import difflib
import fcntl
import html
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.dirname(os.path.abspath(__file__))):   # + scripts/, for the tools module
    if _p not in sys.path:
        sys.path.insert(0, _p)

LOGS = os.path.join(ROOT, "logs", "claude_nightly")
EDITABLE = "automation/scenarios/cross_app_flows.py"
PROMPT = os.path.join(ROOT, "automation", "claude_code", "nightly_prompt.md")
TOOLS = "scripts/claude_nightly_tools.py"
SESSION_HOURS = 4.5            # Claude's working time; the report follows
BUSY_WAIT_MIN = 30

MODE_TEXT = {
    "delta": (
        "Tonight is a quick pass. Run `app-changes`. For each new commit that adds or "
        "changes a screen, a feature or a testID (look at the changed files under App/), "
        "read the new code with `app-show` / `app-grep`, then run `coverage`. Write "
        "scenarios only for new or changed features that no flow drives yet. If nothing "
        "new landed, write none and say so."),
    "full": (
        "Tonight is the weekly full analysis. Run `coverage` and `app-changes`. Go "
        "through the screens of both apps that no flow drives (coverage lists them), "
        "read their source, and add to the report a table of features rated "
        "covered / partial / missing with a risk (high / medium / low). Then write "
        "scenarios for the highest-risk missing features (payments, bookings, orders "
        "first)."),
}
MAX_NEW = {"delta": 3, "full": 6}

_STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")


def _load_env() -> None:
    # Through the platform's ONE loader (automation.config): every process must
    # resolve .env the same way (tests/test_database_url_resolution.py).
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    try:
        from automation.config import load_env
        load_env()
    except ImportError:
        pass


def log(night: str, msg: str) -> None:
    line = f"{datetime.now():%H:%M:%S} {msg}"
    print(line, flush=True)
    with open(os.path.join(night, "nightly.log"), "a") as f:
        f.write(line + "\n")


def git(repo: str, *args: str, check: bool = True) -> str:
    out = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=120)
    if check and out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"git {' '.join(args)} failed")
    return out.stdout


# ── the Claude Code session ──────────────────────────────────────────────────

def session_settings(root: str, night: str) -> Dict[str, Any]:
    """Permissions for the unattended session (`--permission-mode dontAsk` denies the rest)."""
    r, n = "/" + root.rstrip("/"), "/" + night.rstrip("/")   # '//abs/path' = absolute
    return {
        "permissions": {
            "allow": [
                f"Read({r}/**)", "Grep", "Glob", "TodoWrite",
                f"Bash(.venv/bin/python {TOOLS} *)",
                f"Edit({r}/{EDITABLE})",
                f"Write({n}/**)", f"Edit({n}/**)",
            ],
            "deny": [f"Read({r}/.env)", f"Read({r}/.env.*)", f"Edit({r}/repos/**)",
                     "WebFetch", "WebSearch"],
        },
        "env": {"BASH_DEFAULT_TIMEOUT_MS": "600000", "BASH_MAX_TIMEOUT_MS": "600000"},
    }


def build_prompt(date: str, mode: str, night: str, stop_by: datetime, deadline: datetime) -> str:
    with open(PROMPT, encoding="utf-8") as f:
        text = f.read()
    return text.format(date=date, mode=mode, night_dir=os.path.relpath(night, ROOT),
                       stop_by=f"{stop_by:%H:%M}", deadline=f"{deadline:%H:%M}",
                       mode_instructions=MODE_TEXT[mode], max_new=MAX_NEW[mode])


def claude_env(night: str) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env["CLAUDE_NIGHTLY_DIR"] = night
    return env


def run_session(night: str, prompt: str, deadline: datetime) -> Dict[str, Any]:
    claude = shutil.which("claude") or "/usr/local/bin/claude"
    session_id = str(uuid.uuid4())
    cmd = [claude, "-p", prompt, "--model", "opus", "--effort", "high",
           "--permission-mode", "dontAsk",
           "--settings", json.dumps(session_settings(ROOT, night)),
           "--strict-mcp-config",              # no MCP servers: nothing else may drive the sims
           "--output-format", "stream-json", "--verbose",
           "--session-id", session_id, "--name", f"nightly {os.path.basename(night)}"]
    transcript = os.path.join(night, "transcript.jsonl")
    info: Dict[str, Any] = {"session_id": session_id, "timed_out": False}
    with open(transcript, "w") as out:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT,
                                env=claude_env(night))
        while proc.poll() is None:
            if datetime.now() >= deadline:
                info["timed_out"] = True
                proc.terminate()
                try:
                    proc.wait(60)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            time.sleep(15)
    info["exit_code"] = proc.returncode
    info.update(read_result(transcript))
    return info


def read_result(transcript: str) -> Dict[str, Any]:
    """The stream's final `result` event: did the session finish, and why not."""
    res: Dict[str, Any] = {}
    try:
        lines = open(transcript, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return res
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            res = {"subtype": ev.get("subtype"), "is_error": ev.get("is_error"),
                   "turns": ev.get("num_turns"), "result": (ev.get("result") or "")[:2000]}
    if not res and lines:
        res = {"is_error": True, "result": "\n".join(lines[-5:])[:2000]}
    return res


# ── moving the edits onto a review branch ────────────────────────────────────

def _free_branch(root: str, base: str) -> str:
    name, i = base, 2
    while git(root, "branch", "--list", name).strip():
        name, i = f"{base}-{i}", i + 1
    return name


def harvest(root: str, rel: str, snapshot: str, night: str, branch: str, message: str) -> Dict[str, Any]:
    """Commit the session's edit of `rel` to a new branch, then restore the working tree.

    The patch is taken against the snapshot (so a user's own uncommitted edits are not
    swept in) and applied to HEAD in a throwaway worktree. The working copy is always
    put back, even when the branch cannot be made; the patch file is kept either way."""
    cur = os.path.join(root, rel)
    old = open(snapshot, encoding="utf-8").read()
    new = open(cur, encoding="utf-8").read()
    if old == new:
        return {"changed": False}
    patch = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                         f"a/{rel}", f"b/{rel}"))
    patch_file = os.path.join(night, "test_changes.patch")
    with open(patch_file, "w", encoding="utf-8") as f:
        f.write(patch)
    out: Dict[str, Any] = {"changed": True, "patch_file": patch_file,
                           "lines": sum(1 for l in patch.splitlines()
                                        if l[:1] in "+-" and l[:3] not in ("+++", "---"))}
    tmp = tempfile.mkdtemp(prefix="claude-nightly-")
    wt = os.path.join(tmp, "wt")
    name = _free_branch(root, branch)
    try:
        git(root, "worktree", "add", "-q", "-b", name, wt, "HEAD")
        try:
            git(wt, "apply", "--whitespace=nowarn", patch_file)
            py_compile.compile(os.path.join(wt, rel), doraise=True)
            git(wt, "add", rel)
            git(wt, "commit", "-q", "-m", message)
            out["branch"] = name
        except (RuntimeError, py_compile.PyCompileError) as e:
            out["branch_error"] = str(e)[:500]
        finally:
            git(root, "worktree", "remove", "--force", wt, check=False)
        if "branch" not in out:
            git(root, "branch", "-D", name, check=False)
    except RuntimeError as e:
        out["branch_error"] = str(e)[:500]
    finally:
        with open(cur, "w", encoding="utf-8") as f:
            f.write(old)
        shutil.rmtree(tmp, ignore_errors=True)
    return out


# ── the report ───────────────────────────────────────────────────────────────

def read_runs(night: str) -> List[Dict[str, Any]]:
    rows = []
    try:
        for line in open(os.path.join(night, "runs.jsonl")):
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return rows


def counts(summary: Dict[str, Any], runs: List[Dict[str, Any]]) -> Dict[str, int]:
    """Per-flow final outcome: Claude's summary when it wrote one, else the last run."""
    final: Dict[str, str] = {}
    for f in summary.get("flows") or []:
        if f.get("flow_id"):
            final[f["flow_id"]] = (f.get("final_status") or "").lower()
    if not final:
        for r in runs:
            if not r["flow_id"].startswith("cc_"):
                final[r["flow_id"]] = "passed" if r["status"] == "passed" else "failed"
    c = {"passed": 0, "failed": 0, "fixed": 0, "flaky": 0, "not_run": 0}
    for st in final.values():
        c[st if st in c else "failed"] += 1
    return c


def _runs_table(runs: List[Dict[str, Any]]) -> str:
    if not runs:
        return "<p>No flow was run.</p>"
    rows = "".join(
        f"<tr><td>{html.escape(r['flow_id'])}</td><td>{html.escape(r['label'])}</td>"
        f"<td><b style='color:{'#15803d' if r['status'] == 'passed' else '#b91c1c'}'>"
        f"{html.escape(r['status'])}</b></td><td>{r['seconds']}s</td>"
        f"<td>{html.escape(r.get('category') or '')}</td>"
        f"<td>{html.escape((r.get('summary') or '')[:160])}</td>"
        f"<td><code>{r['run_id'][:8]}</code></td></tr>" for r in runs)
    return ("<table cellpadding='6' style='border-collapse:collapse;font-size:13px' border='1'>"
            "<tr><th>Flow</th><th>Attempt</th><th>Result</th><th>Time</th><th>Sorter</th>"
            f"<th>Error</th><th>Run</th></tr>{rows}</table>")


def build_report(date: str, night: str, info: Dict[str, Any]) -> Dict[str, str]:
    runs = read_runs(night)
    try:
        summary = json.load(open(os.path.join(night, "summary.json")))
    except (OSError, ValueError):
        summary = {}
    if not isinstance(summary, dict):
        summary = {}
    try:
        report_md = open(os.path.join(night, "report.md"), encoding="utf-8").read()
    except OSError:
        report_md = ""
    c = counts(summary, runs)
    branch = (info.get("harvest") or {}).get("branch")
    subject = (f"Nightly tests {date}: {c['passed'] + c['fixed']} passed, {c['failed']} failed"
               + (f", {c['fixed']} fixed" if c["fixed"] else "")
               + (f", {c['not_run']} not run" if c["not_run"] else "")
               + (" — changes waiting for approval" if branch else ""))

    problems = []
    if info.get("skipped"):
        problems.append(f"The night was skipped: {info['skipped']}")
    if info.get("timed_out"):
        problems.append("Claude Code reached the time limit and was stopped; the report may be partial.")
    res = info.get("result") or {}
    if res.get("is_error") or (info.get("exit_code") not in (0, None) and not info.get("timed_out")):
        problems.append("Claude Code stopped early: " + (res.get("result") or f"exit {info.get('exit_code')}")
                        + " (if this mentions a usage limit, the Claude plan's limit was reached).")
    if not report_md and not info.get("skipped"):
        problems.append("Claude Code did not write a report; the run log below is all there is.")

    try:
        import markdown
        body = markdown.markdown(report_md, extensions=["tables", "fenced_code"])
    except ImportError:
        body = f"<pre>{html.escape(report_md)}</pre>"

    hv = info.get("harvest") or {}
    if branch:
        approve = (f"<p><b>Test fixes / new scenarios are on branch <code>{branch}</code></b> "
                   f"({hv.get('lines', 0)} changed lines). Nothing is live yet. To accept them: "
                   f"<code>.venv/bin/python scripts/claude_nightly.py merge {branch}</code>, then "
                   f"<code>./stop.sh &amp;&amp; ./start.sh</code>. To review first: "
                   f"<code>git show {branch}</code>.</p>")
    elif hv.get("changed"):
        approve = (f"<p><b>The session changed tests, but they could not be put on a branch</b> "
                   f"({html.escape(hv.get('branch_error', ''))}). The patch is in "
                   f"<code>{html.escape(os.path.relpath(hv['patch_file'], ROOT))}</code>.</p>")
    else:
        approve = "<p>No test changes tonight.</p>"
    resume = (f"<p>Ask Claude about this night: <code>claude --resume {info['session_id']}</code> "
              f"in the project folder.</p>") if info.get("session_id") else ""
    warn = "".join(f"<p style='color:#b91c1c'><b>⚠ {html.escape(p)}</b></p>" for p in problems)
    page = (f"<div style='font-family:-apple-system,Segoe UI,sans-serif;max-width:900px'>"
            f"<h2>{html.escape(subject)}</h2>{warn}{approve}{body}"
            f"<h3>Every run tonight</h3>{_runs_table(runs)}{resume}"
            f"<p style='color:#6b7280'>Logs: {html.escape(os.path.relpath(night, ROOT))}</p></div>")

    slack = [f"*{subject}*"]
    if summary.get("headline"):
        slack.append(summary["headline"])
    slack += [f"⚠ {p}" for p in problems]
    if branch:
        slack.append(f"Changes waiting for approval on `{branch}`")
    for b in summary.get("app_bugs") or []:
        slack.append(f"🐞 {b.get('app')}: {b.get('summary')}")
    text = "\n".join(problems + [report_md or "(no report)"])
    return {"subject": subject, "html": page, "text": text, "slack": "\n".join(slack)}


def send_report(night: str, rep: Dict[str, str]) -> Dict[str, Any]:
    from automation.notifications import email
    from automation.notifications.dispatch import send_slack_text
    to = [e.strip() for e in (os.getenv("REPORT_EMAIL_TO") or "").split(",") if e.strip()]
    if not to:
        try:
            from automation.agentic import settings
            to = settings.get().get("report_emails") or []
        except Exception:
            to = []
    ok, detail = email.send(to, rep["subject"], rep["html"], rep["text"])
    with open(os.path.join(night, "report.html"), "w", encoding="utf-8") as f:
        f.write(rep["html"])
    return {"email": detail if ok else f"not sent: {detail}", "slack": send_slack_text(rep["slack"])}


# ── one night ────────────────────────────────────────────────────────────────

def ensure_rig(night: str) -> bool:
    from automation.agentic import preflight
    checks = preflight.run("staging")
    rig = [c for c in checks if c["name"] != "Staging backend"]
    if preflight.all_ok(rig):
        return True
    log(night, "rig down (" + "; ".join(f"{c['name']}: {c['detail']}" for c in rig if not c["ok"])
        + ") — running ./start.sh")
    with open(os.path.join(night, "start.log"), "w") as f:
        subprocess.run(["./start.sh"], cwd=ROOT, stdout=f, stderr=subprocess.STDOUT, timeout=1200)
    return preflight.all_ok([c for c in preflight.run("staging") if c["name"] != "Staging backend"])


def wait_until_idle(night: str) -> bool:
    import claude_nightly_tools as tools
    t_end = time.time() + BUSY_WAIT_MIN * 60
    while time.time() < t_end:
        busy = tools.active_runs()
        if not busy:
            return True
        log(night, f"waiting: run {busy[0].id[:8]} '{busy[0].test_name}' is still {busy[0].status}")
        time.sleep(60)
    return not tools.active_runs()


def cmd_run(a) -> int:
    _load_env()
    os.makedirs(LOGS, exist_ok=True)
    lock = open(os.path.join(LOGS, ".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another nightly run is in progress")
        return 1

    start = datetime.now()
    date = f"{start:%Y-%m-%d}"
    night = os.path.join(LOGS, date)
    i = 2
    while os.path.exists(night):
        night, i = os.path.join(LOGS, f"{date}-{i}"), i + 1
    os.makedirs(night)
    mode = a.mode if a.mode != "auto" else ("full" if start.weekday() == 6 else "delta")
    log(night, f"nightly run {os.path.basename(night)} ({mode}) starting")
    info: Dict[str, Any] = {"mode": mode, "started": start.isoformat(timespec="seconds")}

    if not ensure_rig(night):
        log(night, "rig still not ready after ./start.sh — Claude will wait for it and report")
    if not wait_until_idle(night):
        info["skipped"] = f"a flow was still running after {BUSY_WAIT_MIN} minutes"
    else:
        snapshot = os.path.join(night, "snapshot_cross_app_flows.py")
        shutil.copyfile(os.path.join(ROOT, EDITABLE), snapshot)
        deadline = datetime.now() + timedelta(hours=a.hours)
        stop_by = deadline - timedelta(minutes=25)
        prompt = build_prompt(date, mode, night, stop_by, deadline)
        log(night, f"Claude Code session until {deadline:%H:%M} (Claude plan, no API key)")
        info.update(run_session(night, prompt, deadline))
        log(night, f"session ended: exit {info.get('exit_code')}, "
                   f"{(info.get('result') or {}).get('subtype') or 'no result'}"
                   + (" (time limit)" if info.get("timed_out") else ""))
        import claude_nightly_tools as tools
        check = subprocess.run([sys.executable, os.path.join(ROOT, TOOLS), "check-flows"], cwd=ROOT,
                               capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=ROOT))
        info["check_flows"] = (check.stdout or check.stderr).strip()[:1000]
        info["harvest"] = harvest(
            ROOT, EDITABLE, snapshot, night, f"claude-nightly/{date}",
            f"test: Claude Code nightly fixes and draft scenarios ({date})\n\n"
            f"Verified by re-running on the simulators; see {os.path.relpath(night, ROOT)}/report.md.\n"
            f"check-flows: {info['check_flows'][:300]}\n\n"
            "Co-Authored-By: Claude Code (nightly) <noreply@anthropic.com>")
        log(night, f"edits: {info['harvest']}")
        if not (info.get("result") or {}).get("is_error") and not info.get("timed_out"):
            tools.mark_apps()       # next night's app-changes starts from here

    rep = build_report(date, night, info)
    info["sent"] = send_report(night, rep)
    info["finished"] = datetime.now().isoformat(timespec="seconds")
    with open(os.path.join(night, "result.json"), "w") as f:
        json.dump(info, f, indent=1, default=str)
    log(night, f"report: {rep['subject']} — email {info['sent']['email']}, slack {info['sent']['slack']}")
    return 0


def cmd_merge(a) -> int:
    branch = a.branch or git(ROOT, "for-each-ref", "--sort=-committerdate", "--count=1",
                             "--format=%(refname:short)", "refs/heads/claude-nightly/").strip()
    if not branch:
        print("no claude-nightly/* branch to merge")
        return 1
    print(git(ROOT, "log", "--oneline", f"HEAD..{branch}") or f"{branch} is already merged")
    try:
        print(git(ROOT, "merge", "--no-ff", "-m", f"Merge {branch} (approved nightly changes)", branch))
    except RuntimeError as e:
        print(f"merge failed: {e}")
        return 1
    check = subprocess.run([sys.executable, os.path.join(ROOT, TOOLS), "check-flows"], cwd=ROOT,
                           capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=ROOT))
    print(check.stdout or check.stderr)
    print("Restart to load them: ./stop.sh && ./start.sh")
    return 0 if check.returncode == 0 else 1


def cmd_status(_a) -> int:
    nights = sorted((d for d in os.listdir(LOGS) if os.path.isdir(os.path.join(LOGS, d))),
                    reverse=True)[:7] if os.path.isdir(LOGS) else []
    if not nights:
        print("no nightly runs yet")
    for d in nights:
        try:
            r = json.load(open(os.path.join(LOGS, d, "result.json")))
        except (OSError, ValueError):
            print(f"{d}: unfinished (see logs/claude_nightly/{d}/nightly.log)")
            continue
        branch = (r.get("harvest") or {}).get("branch") or "no changes"
        print(f"{d} [{r.get('mode')}] {r.get('started', '')[11:16]}–{r.get('finished', '')[11:16]}  "
              f"{branch}  email: {(r.get('sent') or {}).get('email')}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("run", help="run one night now")
    s.add_argument("--mode", choices=["auto", "delta", "full"], default="auto",
                   help="auto = full analysis on Sunday, quick pass otherwise")
    s.add_argument("--hours", type=float, default=SESSION_HOURS, help="Claude Code's working time")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("merge", help="approve a night's changes (default: the newest branch)")
    s.add_argument("branch", nargs="?")
    s.set_defaults(fn=cmd_merge)
    sub.add_parser("status", help="the last nights").set_defaults(fn=cmd_status)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
