"""Continue an interrupted AI Agent session where it stopped.

MEASURED 2026-10-27: the Mac was force-restarted at 17:58 (shutdown cause -128) in the
middle of a 'New scenarios' session (78c05dec). Two drafts had already passed on the
simulators; the worktree, the branch and the Claude conversation were all still on
disk, but the platform could only show the session as 'interrupted'.
"""
import os
from datetime import datetime, timedelta

import pytest

from automation.claude_code import runner, session
from automation.database.config import Base, SessionLocal, engine
from automation.database.models import AgentSession


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.create_all(bind=engine)
    yield
    with SessionLocal() as db:
        db.query(AgentSession).delete()
        db.commit()


@pytest.fixture
def interrupted(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    (wt / "scripts").mkdir(parents=True)
    with SessionLocal() as db:
        db.add(AgentSession(id="s-int", kind="generate", trigger="manual", status="interrupted",
                            options={}, worktree=str(wt), branch="agent/x-generate",
                            base_commit="abc", claude_session_id="conv-1", pid=999999,
                            started_at=datetime.utcnow() - timedelta(minutes=40),
                            finished_at=datetime.utcnow()))
        db.commit()
    monkeypatch.setattr(session, "claude_status", lambda *a, **k: {"installed": True, "logged_in": True})
    monkeypatch.setattr(session, "_git", lambda *a, **k: "  agent/x-generate\n")
    spawned = []

    def spawn(sid):
        spawned.append(sid)
        with SessionLocal() as db:
            db.get(AgentSession, sid).pid = os.getpid()       # a live runner
            db.commit()
        return os.getpid()

    monkeypatch.setattr(session, "spawn_runner", spawn)
    return wt, spawned


def test_an_interrupted_session_continues_in_its_own_worktree(interrupted):
    wt, spawned = interrupted
    assert session.continue_session("s-int", "roopa") == "s-int"
    s = session.get("s-int")
    assert spawned == ["s-int"] and s["status"] == "queued"
    assert s["options"]["continued"] == 1 and s["finished_at"] is None
    assert session.active()["id"] == "s-int"                  # not reaped again as dead
    assert runner.continuing(runner._row("s-int"))


def test_only_an_interrupted_session_with_its_worktree_can_continue(interrupted, tmp_path):
    wt, spawned = interrupted
    with SessionLocal() as db:
        db.get(AgentSession, "s-int").status = "approved"
        db.commit()
    with pytest.raises(session.Refused, match="only an interrupted session"):
        session.continue_session("s-int")
    with SessionLocal() as db:
        r = db.get(AgentSession, "s-int")
        r.status, r.worktree = "interrupted", str(tmp_path / "gone")
        db.commit()
    with pytest.raises(session.Refused, match="worktree or branch is gone"):
        session.continue_session("s-int")
    assert spawned == []


def test_one_session_at_a_time_still_holds(interrupted):
    _, spawned = interrupted
    with SessionLocal() as db:
        db.add(AgentSession(id="s-run", kind="heal", trigger="manual", status="running",
                            options={}, pid=os.getpid(), created_at=datetime.utcnow()))
        db.commit()
    with pytest.raises(session.Busy):
        session.continue_session("s-int")
    assert spawned == []


def test_the_continue_prompt_says_what_happened_and_what_counts(tmp_path):
    ctx = {"stop_by": "20:40", "deadline": "21:00"}
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "agent_tools.py").write_text("# old tools\n")
    text = runner.continue_prompt(ctx, str(tmp_path))
    assert "preflight --wait 15" in text and "20:40" in text and "git diff" in text
    assert "run each kept flow once more" in text            # no fingerprints in this worktree
    (tmp_path / "scripts" / "agent_tools.py").write_text("from automation.claude_code import fingerprint\n")
    assert "run each kept flow once more" not in runner.continue_prompt(ctx, str(tmp_path))


def test_the_same_claude_conversation_is_resumed_or_a_new_one_started(monkeypatch):
    calls = []

    def once(sid, sdir, wt, prompt, model, s, deadline, name, mode, resume=None):
        calls.append((prompt, mode, resume))
        if resume == "gone":
            return {"is_error": True, "turns": 0, "result": "No conversation found with session ID: gone"}
        return {"is_error": False, "turns": 12, "result": "done", "claude_session_id": resume or "new"}

    monkeypatch.setattr(runner, "_run_claude_once", once)
    monkeypatch.setattr(runner, "phase", lambda *a, **k: None)
    s = {"model": "opus", "effort": "high"}
    runner.run_claude("s", "/s", "/wt", "carry on", s, None, "n", resume="conv-1", fresh_prompt="whole task")
    assert calls == [("carry on", "a", "conv-1")]
    calls.clear()
    info = runner.run_claude("s", "/s", "/wt", "carry on", s, None, "n", resume="gone", fresh_prompt="whole task")
    assert calls == [("carry on", "a", "gone"), ("whole task", "a", None)] and info["turns"] == 12


def test_a_fresh_session_is_not_mistaken_for_a_continued_one(tmp_path):
    assert not runner.continuing({"options": {}, "worktree": str(tmp_path), "branch": "b"})
    assert not runner.continuing({"options": {"continued": 1}, "worktree": str(tmp_path / "x"), "branch": "b"})
