"""The AI Agent on Claude Code (automation/claude_code/).

Asked 2026-10-22: "creating an agent in that application to analyze the vyapy
business and consumer application, based on that creating new scenarios, and the
agent must work with claude code … if any failure happens claude code should fix the
automation platform". These pin what makes that safe to leave running unattended:

  * a session can touch only automation/, tests/, scripts/ of its own worktree —
    not its guardrails, the app checkouts or secrets — and run only the tools;
  * its change reaches the person's checkout only through approval, and only when
    every gate passed: no value check removed, no new unit-test failure, and the
    flows it touched passed on the simulators AFTER the last edit;
  * a Run all with failures hands exactly those runs to a heal session.
"""
import json
import os
import subprocess
import time
import uuid
from datetime import datetime

import pytest

from automation.claude_code import events, gates, guard, impact, paths, runner, session
from automation.database.config import Base, SessionLocal, engine
from automation.database import models  # noqa: F401
from automation.database.models import AgentSession, TestRun


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.create_all(bind=engine)
    yield
    with SessionLocal() as db:
        for m in (AgentSession, TestRun, models.AgentSetting):
            db.query(m).delete()
        db.commit()


# ── guard (PreToolUse hook) ──────────────────────────────────────────────────

@pytest.fixture
def box(tmp_path):
    main = tmp_path / "main"
    wt = tmp_path / "wt"
    sdir = main / "logs" / "agent_sessions" / "s1"
    for d in (main / "repos" / "app" / "App", wt / "automation" / "scenarios", wt / "tests",
              wt / "automation" / "claude_code", sdir):
        d.mkdir(parents=True, exist_ok=True)
    (main / ".env").write_text("SECRET=1\n")
    os.symlink(main / "repos", wt / "repos")
    os.symlink(main / ".env", wt / ".env")
    env = {"AGENT_WORKTREE": str(wt), "AGENT_SESSION_DIR": str(sdir), "AGENT_MAIN_ROOT": str(main)}
    return {"main": main, "wt": wt, "sdir": sdir, "env": env}


def _check(box, tool, **inp):
    return guard.check(tool, inp, str(box["wt"]), box["env"])


def test_guard_lets_the_session_edit_the_automation_and_its_own_folder(box):
    assert _check(box, "Edit", file_path=str(box["wt"] / "automation" / "scenarios" / "cross_app_flows.py")) == ""
    assert _check(box, "Write", file_path="tests/test_new.py") == ""          # relative to the worktree
    assert _check(box, "Write", file_path=str(box["sdir"] / "report.md")) == ""


def test_guard_blocks_the_app_checkouts_even_through_the_worktree_link(box):
    why = _check(box, "Edit", file_path=str(box["wt"] / "repos" / "app" / "App" / "Screen.js"))
    assert "app checkouts are never modified" in why


def test_guard_blocks_secrets_its_own_guardrails_and_the_run_ledger(box):
    assert "secrets" in _check(box, "Read", file_path=str(box["wt"] / ".env"))
    assert "secrets" in _check(box, "Edit", file_path=str(box["main"] / ".env"))
    assert "guardrails" in _check(box, "Edit", file_path=str(box["wt"] / "automation" / "claude_code" / "gates.py"))
    assert "guardrails" in _check(box, "Write", file_path=str(box["wt"] / "scripts" / "agent_tools.py"))
    assert "ledger" in _check(box, "Write", file_path=str(box["sdir"] / "runs.jsonl"))
    assert "edit only" in _check(box, "Edit", file_path=str(box["main"] / "AGENTS.md"))
    assert "edit only" in _check(box, "Edit", file_path=str(box["wt"] / "start.sh"))


@pytest.mark.parametrize("cmd,ok", [
    (".venv/bin/python scripts/agent_tools.py run flow1 --label fix", True),
    (".venv/bin/python scripts/agent_tools.py --help", True),
    ("git diff", True),
    ("git log --oneline -5", True),
    (".venv/bin/python scripts/agent_tools.py run flow1 && rm -rf /", False),
    (".venv/bin/python scripts/agent_tools.py flows | head", False),
    (".venv/bin/python scripts/agent_tools.py flows > /tmp/x", False),
    ("git push origin HEAD", False),
    ("git commit -am x", False),
    ("cd .. ; ls", False),
    ("curl https://example.com", False),
    (".venv/bin/python -c 'import os'", False),
])
def test_guard_allows_only_the_tools_and_read_only_git(box, cmd, ok):
    assert (_check(box, "Bash", command=cmd) == "") is ok


def test_guard_is_a_working_hook_script(box):
    """Run as Claude Code runs it: JSON on stdin, exit 2 + stderr to block."""
    d = {"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": str(box["wt"])}
    out = subprocess.run(["python3", guard.__file__], input=json.dumps(d), capture_output=True,
                         text=True, env={**os.environ, **box["env"]})
    assert out.returncode == 2 and "Blocked by the agent guard" in out.stderr
    d["tool_input"]["command"] = "git status"
    out = subprocess.run(["python3", guard.__file__], input=json.dumps(d), capture_output=True,
                         text=True, env={**os.environ, **box["env"]})
    assert out.returncode == 0


def test_session_settings_wire_the_guard_and_deny_secrets(tmp_path):
    s = runner.session_settings(str(tmp_path / "wt"), str(tmp_path / "s"))
    hook = s["hooks"]["PreToolUse"][0]
    assert "Bash" in hook["matcher"] and "Edit" in hook["matcher"]
    assert hook["hooks"][0]["command"].endswith("automation/claude_code/guard.py")
    assert any(".env" in d for d in s["permissions"]["deny"])
    assert "WebFetch" in s["permissions"]["deny"]
    assert not any(a.startswith("Bash(") and "agent_tools" not in a and "git " not in a
                   for a in s["permissions"]["allow"])


# ── gates ────────────────────────────────────────────────────────────────────

def test_scope_keeps_only_the_editable_folders_and_never_the_guardrails():
    keep, out = gates.split_scope(["automation/scenarios/cross_app_flows.py", "tests/test_x.py",
                                   "scripts/dryrun.py", "AGENTS.md", "start.sh",
                                   "automation/claude_code/gates.py", "scripts/agent_tools.py",
                                   ".claude/skills/x/SKILL.md"])
    assert keep == ["automation/scenarios/cross_app_flows.py", "tests/test_x.py", "scripts/dryrun.py"]
    assert set(out) == {"AGENTS.md", "start.sh", "automation/claude_code/gates.py",
                        "scripts/agent_tools.py", ".claude/skills/x/SKILL.md"}


def test_a_removed_value_check_or_a_deleted_flow_fails_the_gate():
    before = {"flow1": ["@book_appointment", "verify the total is €12.00"], "flow2": ["@x"]}
    assert gates.checks_kept(before, {"flow1": ["@book_appointment", "verify the total is €12.00"],
                                      "flow2": ["@x"], "cc_new": ["@y"]})["ok"]
    g = gates.checks_kept(before, {"flow1": ["@book_appointment"], "flow2": ["@x"]})
    assert not g["ok"] and "value check" in g["detail"]
    g = gates.checks_kept(before, {"flow1": before["flow1"]})
    assert not g["ok"] and "flow2 was deleted" in g["detail"]
    assert not gates.checks_kept(before, None)["ok"]


def test_flow_changes_names_edited_and_new_flows():
    changed, new = gates.flow_changes({"a": ["1"], "b": ["2"]}, {"a": ["1"], "b": ["2", "3"], "cc_c": ["4"]})
    assert changed == ["b"] and new == ["cc_c"]


def test_only_new_unit_test_failures_fail_the_gate():
    base = {"failed": ["tests/test_consumer_checkout.py::test_old"]}
    same = {"failed": ["tests/test_consumer_checkout.py::test_old"], "counts": {"passed": 10},
            "returncode": 1, "tail": ""}
    assert gates.unit_gate(same, base)["ok"]
    worse = {**same, "failed": same["failed"] + ["tests/test_pay_business.py::test_bill"]}
    g = gates.unit_gate(worse, base)
    assert not g["ok"] and "test_pay_business.py::test_bill" in g["detail"]
    assert not gates.unit_gate({**same, "returncode": 2, "failed": []}, base)["ok"]   # collection error


def test_the_unit_test_runner_parses_pytest_output(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_one.py").write_text(
        "def test_ok():\n    assert True\n\ndef test_bad():\n    assert False\n")
    res = gates.run_unit_tests(str(tmp_path), timeout=120)
    assert res["failed"] == ["tests/test_one.py::test_bad"]
    assert res["counts"].get("passed") == 1 and res["returncode"] == 1


def test_a_device_run_counts_only_after_the_last_edit():
    runs = [{"flow_id": "flow5", "status": "passed", "t0": 100.0},
            {"flow_id": "flow6", "status": "failed", "t0": 300.0},
            {"flow_id": "flow6", "status": "passed", "t0": 150.0}]
    assert gates.device_gate(runs, 90.0, ["flow5"], None)["ok"]
    g = gates.device_gate(runs, 200.0, ["flow5"], None)              # edited after the pass
    assert not g["ok"] and "flow5: no passing run after the last edit" in g["detail"]
    assert gates.device_gate(runs, 90.0, [], ["flow6", "flow5"])["ok"]
    assert not gates.device_gate(runs, 200.0, [], ["flow6"])["ok"]
    assert gates.device_gate(runs, 90.0, [], [impact.ALL])["ok"]
    assert not gates.device_gate(runs, 500.0, [], [impact.ALL])["ok"]


def test_the_ledger_is_checked_against_the_database():
    """A ledger line saying 'passed' does not make a run pass: the test_runs row does."""
    rid = str(uuid.uuid4())
    with SessionLocal() as db:
        db.add(TestRun(id=rid, test_name="[Staging] x", status="failed", started_at=datetime.utcnow()))
        db.commit()
    [r] = gates.confirm_runs([{"flow_id": "flow1", "run_id": rid, "status": "passed", "t0": 1}])
    assert r["status"] == "failed" and r["t0"] > 1_000_000_000
    assert gates.confirm_runs([{"flow_id": "flow1", "run_id": "nope", "status": "passed"}]) == []


def test_size_gate():
    assert gates.size_gate([{"added": 300, "removed": 100}], 800)["ok"]
    assert not gates.size_gate([{"added": 700, "removed": 200}], 800)["ok"]


# ── impact ───────────────────────────────────────────────────────────────────

_SRC = '''
def _bounded_call(fn):
    return fn()

class FlowRunner:
    _PLAIN_STEP_TOKENS = {"accept the appointment": "@accept_appointment"}

    def _handle_special(self, r, step, notes):
        if step == "@pay_cash" or step.startswith("@pay:"):
            return self._pay(notes)
        if step == "@accept_appointment":
            return self._accept(notes)
        return False

    def _pay(self, notes):
        return self._read_bill() and _bounded_call(lambda: 1)

    def _read_bill(self):
        return 1

    def _accept(self, notes):
        return True

    def _run_segment(self, seg):
        return self._smart_click()

    def _smart_click(self):
        return True
'''


def test_a_change_reaches_the_flows_whose_tokens_call_it():
    flows = {"pay": ["@pay:cash"], "invite": ["accept the appointment"], "plain": ["click x"]}
    al = {"accept the appointment": "@accept_appointment"}
    assert impact.flows_for_functions(flows, _SRC, ["_read_bill"], al) == ["pay"]
    assert impact.flows_for_functions(flows, _SRC, ["_accept"], al) == ["invite"]
    assert impact.flows_for_functions(flows, _SRC, ["_bounded_call"], al) == ["pay"]


def test_engine_core_reaches_every_flow():
    assert impact.flows_for_functions({"a": ["click x"]}, _SRC, ["_smart_click"]) == [impact.ALL]


def test_changed_functions_ignores_comments_and_finds_edits():
    after = _SRC.replace("return 1\n", "# a comment\n        return 1\n", 1)
    assert impact.changed_functions(_SRC, after) == set()
    after = _SRC.replace("def _read_bill(self):\n        return 1", "def _read_bill(self):\n        return 2")
    assert impact.changed_functions(_SRC, after) == {"_read_bill"}


def test_impact_on_the_real_engine():
    from automation.scenarios.cross_app_flows import FLOWS, FlowRunner
    src = open(os.path.join(paths.MAIN_ROOT, impact.FLOWS_FILE), encoding="utf-8").read()
    flows = {k: [st for s in v["segments"] for st in s["steps"]] for k, v in FLOWS.items()}
    hit = impact.flows_for_functions(flows, src, ["_accept_appointment"], FlowRunner._PLAIN_STEP_TOKENS)
    assert set(hit) == {"flow5", "flow6"}


# ── transcript events ────────────────────────────────────────────────────────

def test_the_transcript_reads_as_short_lines(tmp_path):
    t = tmp_path / "transcript.jsonl"
    lines = [
        {"type": "system", "subtype": "init", "model": "claude-opus-5-5"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "flow3 failed at the panel search; reading the run."},
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": ".venv/bin/python scripts/agent_tools.py run flow3 --label fix"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "RUN abc flow3 -> PASSED after 300s"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t2", "name": "Edit",
             "input": {"file_path": "/wt/automation/scenarios/cross_app_flows.py"}}]}},
        {"type": "result", "subtype": "success", "num_turns": 12, "is_error": False},
    ]
    t.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    items, last = events.condense(str(t), roots=["/wt"])
    texts = [i["text"] for i in items]
    assert "Run flow3 on the simulators (fix)" in texts
    assert "Edited automation/scenarios/cross_app_flows.py" in texts
    assert any("PASSED" in x for x in texts)
    assert last == len(items) == 6
    more, _ = events.condense(str(t), after=4, roots=["/wt"])
    assert [i["i"] for i in more] == [5, 6]
    assert events.last_activity(str(t), ["/wt"]) == "Edited automation/scenarios/cross_app_flows.py"
    assert events.final_result(str(t))["turns"] == 12


# ── prompts ──────────────────────────────────────────────────────────────────

def _ctx():
    return {"session_dir": "/s", "worktree": "/wt", "branch": "agent/x", "date": "2026-10-22",
            "stop_by": "03:40", "deadline": "04:00", "known_failing_tests": "none recorded"}


@pytest.mark.parametrize("kind", session.KINDS)
def test_every_kind_has_a_complete_prompt(kind):
    from automation.agentic import settings
    from automation.claude_code import prompts
    opts = {"run_ids": ["r1"]} if kind == "fix_run" else {}
    text = prompts.build(kind, opts, _ctx(), settings.get())
    for name in ("$worktree", "$session_dir", "$task", "$max_fix_attempts", "$run_scope", "$mode",
                 "$focus", "$max_new", "$stop_by"):
        assert name not in text, f"{name} left unfilled in the {kind} prompt"
    assert "/wt" in text and "summary.json" in text and "Never make a test pass by weakening it" in text


def test_a_run_all_heal_names_exactly_the_failed_runs():
    from automation.agentic import settings
    from automation.claude_code import prompts
    from automation.scenarios.cross_app_flows import list_flows
    f = list_flows()[2]
    rid = str(uuid.uuid4())
    with SessionLocal() as db:
        db.add(TestRun(id=rid, test_name=f"[Staging] {f['name']}", status="failed",
                       error_message="[FAIL] @open_order — timed out", started_at=datetime.utcnow()))
        db.commit()
    text = prompts.build("heal", {"run_ids": [rid]}, _ctx(), settings.get())
    assert rid in text and f"`{f['id']}`" in text and "do not re-run the flows that passed" in text


# ── sessions ─────────────────────────────────────────────────────────────────

@pytest.fixture
def logged_in(monkeypatch):
    monkeypatch.setattr(session, "claude_status",
                        lambda *a, **k: {"installed": True, "logged_in": True, "plan": "max"})


def test_a_session_needs_claude_code_logged_in(monkeypatch):
    monkeypatch.setattr(session, "claude_status", lambda *a, **k: {"installed": True, "logged_in": False})
    with pytest.raises(session.Refused, match="not installed or not logged in"):
        session.create("analyze", spawn=False)


def test_one_session_at_a_time(logged_in):
    sid = session.create("analyze", spawn=False)
    with SessionLocal() as db:
        db.get(AgentSession, sid).pid = os.getpid()          # a live runner
        db.commit()
    with pytest.raises(session.Busy):
        session.create("heal", spawn=False)
    assert session.active()["id"] == sid


def test_a_session_whose_runner_died_is_closed(logged_in):
    sid = session.create("analyze", spawn=False)
    with SessionLocal() as db:
        r = db.get(AgentSession, sid)
        r.status, r.pid = "running", 999_999
        db.commit()
    assert session.active() is None
    assert session.get(sid)["status"] == "interrupted"


def test_fix_run_needs_a_run_and_kinds_are_checked(logged_in):
    with pytest.raises(ValueError):
        session.create("fix_run", spawn=False)
    with pytest.raises(ValueError):
        session.create("delete_everything", spawn=False)


# ── harvest + approve on a scratch repository ────────────────────────────────

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    main = tmp_path / "main"
    (main / "automation" / "scenarios").mkdir(parents=True)
    (main / "tests").mkdir()
    (main / "automation" / "scenarios" / "cross_app_flows.py").write_text("X = 1\n")
    (main / "automation" / "util.py").write_text("Y = 1\n")
    (main / "README.md").write_text("hello\n")
    _git(main.parent, "init", "-q", "-b", "work", str(main))
    _git(main, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(main, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    tools = tmp_path / "tools.py"
    tools.write_text("print('OK: flows load')\n")
    monkeypatch.setattr(paths, "MAIN_ROOT", str(main))
    monkeypatch.setattr(paths, "WORKTREES_DIR", str(tmp_path / "wts"))
    monkeypatch.setattr(paths, "SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(paths, "TOOLS", str(tools))
    monkeypatch.setattr(runner, "phase", lambda *a, **k: None)
    monkeypatch.setattr(runner, "wait_until_idle", lambda *a, **k: True)
    monkeypatch.setattr(gates, "flows_load", lambda *a: gates.gate("flows load", True, "ok"))
    monkeypatch.setattr(gates, "dump_flows", lambda root: {"flows": {"flow1": ["verify the total"]},
                                                           "aliases": {}})
    monkeypatch.setattr(gates, "run_unit_tests", lambda *a, **k: {
        "ok": True, "failed": [], "counts": {"passed": 3}, "tail": "", "returncode": 0})
    return main


def _harvest(repo, sid="s-1"):
    w = runner.make_worktree(sid + "-" + uuid.uuid4().hex[:6], "heal")
    sdir = os.path.join(paths.SESSIONS_DIR, sid)
    os.makedirs(sdir, exist_ok=True)
    return w, sdir


def test_harvest_keeps_the_automation_change_and_reverts_the_rest(repo):
    w, sdir = _harvest(repo)
    wt = w["worktree"]
    open(os.path.join(wt, "automation", "util.py"), "w").write("Y = 2\n")
    open(os.path.join(wt, "README.md"), "w").write("changed by the agent\n")
    os.makedirs(os.path.join(wt, "logs"), exist_ok=True)
    open(os.path.join(wt, "logs", "run.log"), "w").write("noise\n")
    out = runner.harvest("s-1", sdir, wt, w["base"], w["branch"], {"flows": {"flow1": ["verify the total"]},
                                                                  "aliases": {}}, {"max_changed_lines": 800})
    assert out["status"] == "gates_failed"                    # the scope gate: README was touched
    scope = next(g for g in out["gates"] if g["name"] == "scope")
    assert not scope["ok"] and "README.md" in scope["detail"]
    files = _git(repo, "show", "--name-only", "--format=", w["branch"]).split()
    assert files == ["automation/util.py"]
    assert open(os.path.join(wt, "README.md")).read() == "hello\n"
    assert "Y = 2" in open(os.path.join(sdir, "changes.patch")).read()


def test_a_clean_verified_change_waits_for_review(repo):
    w, sdir = _harvest(repo)
    open(os.path.join(w["worktree"], "automation", "util.py"), "w").write("Y = 3\n")
    out = runner.harvest("s-1", sdir, w["worktree"], w["base"], w["branch"],
                         {"flows": {"flow1": ["verify the total"]}, "aliases": {}}, {"max_changed_lines": 800})
    assert out["status"] == "review", out["gates"]
    assert [g["name"] for g in out["gates"]] == ["scope", "compiles", "flows load", "checks kept",
                                                 "unit tests", "on device", "size"]


def test_an_engine_change_never_run_on_the_simulators_is_not_approvable(repo):
    w, sdir = _harvest(repo)
    open(os.path.join(w["worktree"], "automation", "scenarios", "cross_app_flows.py"), "w").write(
        "X = 1\n\ndef helper():\n    return 2\n")
    out = runner.harvest("s-1", sdir, w["worktree"], w["base"], w["branch"],
                         {"flows": {"flow1": ["verify the total"]}, "aliases": {}}, {"max_changed_lines": 800})
    dev = next(g for g in out["gates"] if g["name"] == "on device")
    assert out["status"] == "gates_failed" and not dev["ok"]
    assert "helper" in dev["detail"]


def test_nothing_changed_means_no_branch_work(repo):
    w, sdir = _harvest(repo)
    out = runner.harvest("s-1", sdir, w["worktree"], w["base"], w["branch"], None, {})
    assert out["status"] == "no_changes" and out["files"] == []


def _review_session(repo, status="review"):
    w, sdir = _harvest(repo)
    open(os.path.join(w["worktree"], "automation", "util.py"), "w").write("Y = 9\n")
    runner.harvest("s-1", sdir, w["worktree"], w["base"], w["branch"],
                   {"flows": {"flow1": ["verify the total"]}, "aliases": {}}, {"max_changed_lines": 800})
    runner.remove_worktree(w["worktree"])
    sid = str(uuid.uuid4())
    with SessionLocal() as db:
        db.add(AgentSession(id=sid, kind="heal", status=status, branch=w["branch"], base_commit=w["base"]))
        db.commit()
    return sid, w


def test_approve_merges_into_the_working_branch(repo):
    sid, w = _review_session(repo)
    out = session.approve(sid, "admin@x")
    assert out["merged"] and out["into"] == "work" and out["restart_needed"]
    assert (repo / "automation" / "util.py").read_text() == "Y = 9\n"
    assert session.get(sid)["status"] == "approved"


def test_never_merged_into_main(repo):
    sid, w = _review_session(repo)
    _git(repo, "checkout", "-q", "-b", "main")
    with pytest.raises(session.Refused, match="never merged"):
        session.approve(sid)
    assert (repo / "automation" / "util.py").read_text() == "Y = 1\n"


def test_a_session_with_a_failed_gate_cannot_be_approved_only_rejected(repo):
    sid, w = _review_session(repo, status="gates_failed")
    with pytest.raises(session.Refused, match="checks all passed"):
        session.approve(sid)
    session.reject(sid, "admin@x")
    assert session.get(sid)["status"] == "rejected"
    assert not _git(repo, "branch", "--list", w["branch"]).strip()


# ── Run all → heal ───────────────────────────────────────────────────────────

def test_a_run_all_with_failures_starts_a_heal_on_exactly_those_runs(monkeypatch):
    from automation.api.v1.routers import jobs
    got = {}
    monkeypatch.setattr(session, "create", lambda kind, **kw: got.update(kind=kind, **kw) or "sid-1")
    done = [("flow1", "r1", "passed"), ("flow2", "r2", "failed"), ("flow3", "r3", "failed")]
    assert jobs.heal_after_run_all(done, "me") == "sid-1"
    assert got["kind"] == "heal" and got["trigger"] == "run_all"
    assert got["options"] == {"run_ids": ["r2", "r3"]}


def test_no_heal_when_all_passed_switched_off_or_the_user_stopped(monkeypatch):
    from automation.agentic import settings
    from automation.api.v1.routers import jobs
    called = []
    monkeypatch.setattr(session, "create", lambda *a, **k: called.append(1) or "x")
    assert jobs.heal_after_run_all([("flow1", "r1", "passed")]) is None
    assert jobs.heal_after_run_all([("flow1", "r1", "failed"), ("flow2", "r2", "cancelled")]) is None
    rid = str(uuid.uuid4())
    with SessionLocal() as db:
        db.add(TestRun(id=rid, test_name="x", status="stopped", error_message="Stopped by user"))
        db.commit()
    assert jobs.heal_after_run_all([("flow1", "r1", "failed"), ("flow2", rid, "stopped")]) is None
    settings.update({"auto_heal_after_run_all": False})
    assert jobs.heal_after_run_all([("flow1", "r1", "failed")]) is None
    assert called == []


def test_an_old_claude_code_falls_back_to_a_model_it_supports(monkeypatch):
    """MEASURED 2026-10-22: Claude Code 2.1.205 refuses opus ('does not support this
    model; version 2.1.280 or newer is required') — the session must not die of it."""
    calls = []

    def once(sid, sdir, wt, prompt, model, s, deadline, name, mode):
        calls.append((model, mode))
        if model == "opus":
            return {"is_error": True, "turns": 1, "result": "API Error: 400 Claude Code 2.1.205 does not "
                    "support this model; version 2.1.280 or newer is required."}
        return {"is_error": False, "turns": 30, "result": "done"}

    monkeypatch.setattr(runner, "_run_claude_once", once)
    monkeypatch.setattr(runner, "phase", lambda *a, **k: None)
    info = runner.run_claude("s", "/s", "/wt", "p", {"model": "opus", "effort": "high"}, None, "n")
    assert calls == [("opus", "w"), ("sonnet", "a")]
    assert info["turns"] == 30 and info["model_fallback"]["used"] == "sonnet"
    monkeypatch.setattr(runner, "_run_claude_once",
                        lambda *a: {"is_error": True, "turns": 40, "result": "does not support this model"})
    assert "model_fallback" not in runner.run_claude("s", "/s", "/wt", "p", {"model": "opus", "effort": "high"},
                                                     None, "n")


def test_untracked_checkouts_inside_a_partly_tracked_folder_reach_the_worktree(tmp_path, monkeypatch):
    """MEASURED 2026-10-22: repos/ holds tracked gitlinks, so the worktree got a real
    repos/ folder, the link was skipped, and the analysis found no app at all."""
    main, wt = tmp_path / "main", tmp_path / "wt"
    (main / "repos" / "app-checkout" / ".git").mkdir(parents=True)
    (main / ".venv").mkdir()
    (wt / "repos" / "old-gitlink").mkdir(parents=True)
    monkeypatch.setattr(paths, "MAIN_ROOT", str(main))
    runner.link_runtime(str(wt))
    assert os.path.islink(wt / "repos" / "app-checkout")
    assert (wt / "repos" / "app-checkout" / ".git").is_dir()
    assert os.path.islink(wt / ".venv") and not os.path.islink(wt / "repos")


def test_a_temporary_service_error_resumes_the_same_claude_session(monkeypatch):
    """MEASURED 2026-10-22: a generate session died after 19 turns on 'Your organization
    has disabled Claude subscription access for Claude Code'; minutes later it worked.
    The session must pick up where it was (same Claude session, its edits kept)."""
    from datetime import datetime, timedelta
    calls = []

    def once(sid, sdir, wt, prompt, model, s, deadline, name, mode, resume=None):
        calls.append((prompt[:30], mode, resume))
        if len(calls) == 1:
            return {"is_error": True, "turns": 19, "claude_session_id": "c-1",
                    "result": "Your organization has disabled Claude subscription access for Claude Code"}
        return {"is_error": False, "turns": 40, "claude_session_id": resume, "result": "done"}

    monkeypatch.setattr(runner, "_run_claude_once", once)
    monkeypatch.setattr(runner, "phase", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_stop_requested", lambda sid: False)
    monkeypatch.setattr(runner, "RESUME_WAIT_S", 0)
    info = runner.run_claude("s", "/s", "/wt", "the task", {"model": "sonnet", "effort": "high"},
                             datetime.now() + timedelta(hours=1), "n")
    assert calls[1] == (runner.CONTINUE_PROMPT[:30], "a", "c-1")
    assert info["turns"] == 40 and len(info["resumed_after"]) == 1


def test_a_usage_limit_is_not_retried_in_a_loop(monkeypatch):
    from datetime import datetime, timedelta
    calls = []
    monkeypatch.setattr(runner, "_run_claude_once", lambda *a, **k: calls.append(1) or {
        "is_error": True, "turns": 30, "result": "Claude usage limit reached. Your limit will reset at 7pm"})
    monkeypatch.setattr(runner, "phase", lambda *a, **k: None)
    info = runner.run_claude("s", "/s", "/wt", "t", {"model": "sonnet", "effort": "high"},
                             datetime.now() + timedelta(hours=1), "n")
    assert calls == [1] and "resumed_after" not in info


# ── automatic triggers leave a trace; the nightly clock ──────────────────────

@pytest.fixture(autouse=True)
def _private_sessions_dir(tmp_path, monkeypatch):
    """No test writes into the real logs/agent_sessions (hooks.jsonl, baselines…)."""
    monkeypatch.setattr(paths, "SESSIONS_DIR", str(tmp_path / "agent_sessions"))


def test_every_run_all_hook_outcome_is_recorded(monkeypatch):
    """MEASURED 2026-10-26: a Run all ended with 9 failures, no heal session started,
    and nothing anywhere said why."""
    from automation.api.v1.routers import jobs
    monkeypatch.setattr(session, "create", lambda kind, **kw: "sid-12345678")
    jobs.heal_after_run_all([("flow1", "r1", "passed")])
    jobs.heal_after_run_all([("flow1", "r1", "failed")])

    def busy(kind, **kw):
        raise session.Busy("an AI Agent session is already running (heal, abcd1234)")

    monkeypatch.setattr(session, "create", busy)
    jobs.heal_after_run_all([("flow1", "r1", "failed")])
    got = [(h["outcome"], h["detail"]) for h in session.recent_hooks()]
    assert got == [("refused", "an AI Agent session is already running (heal, abcd1234)"),
                   ("started", "heal session sid-1234"),
                   ("skipped", "every flow passed")]


def _nightly_settings(**kw):
    from automation.agentic import settings
    settings.update({"nightly_enabled": True, "nightly_time": "13:15", **kw})


def test_the_nightly_is_due_once_inside_its_window():
    from datetime import datetime
    from automation.claude_code import schedule
    from automation.agentic import settings
    _nightly_settings()
    s = settings.get()
    day = datetime(2026, 10, 26)
    assert schedule.due(s, day.replace(hour=13, minute=10)) is None          # too early
    assert schedule.due(s, day.replace(hour=13, minute=20)) is not None
    assert schedule.due(s, day.replace(hour=16, minute=20)) is None          # window over
    settings.update({"nightly_enabled": False})
    assert schedule.due(settings.get(), day.replace(hour=13, minute=20)) is None


def test_the_clock_starts_one_nightly_and_waits_while_busy(monkeypatch):
    from datetime import datetime
    from automation.claude_code import schedule
    _nightly_settings(nightly_time=datetime.now().strftime("%H:%M"))
    monkeypatch.setitem(schedule._state, "last_try", 0.0)
    monkeypatch.setitem(schedule._state, "last_error", "")

    def busy(kind, **kw):
        raise session.Busy("an AI Agent session is already running")

    monkeypatch.setattr(session, "create", busy)
    assert schedule.tick() is None
    assert session.recent_hooks()[0]["outcome"] == "waiting"
    assert schedule.tick() is None                     # retried only after RETRY_S
    started = []
    monkeypatch.setitem(schedule._state, "last_try", 0.0)
    monkeypatch.setattr(session, "create", lambda kind, **kw: started.append((kind, kw)) or "sid-nightly1")
    assert schedule.tick() == "sid-nightly1"
    assert started[0][0] == "nightly" and started[0][1]["trigger"] == "nightly"


def test_the_clock_never_touches_launchd_from_tests(monkeypatch):
    from automation.claude_code import schedule
    called = []
    monkeypatch.setattr(schedule, "remove_legacy_launchd", lambda: called.append(1))
    schedule.start()
    assert called == [] and schedule._state["thread"] is None
