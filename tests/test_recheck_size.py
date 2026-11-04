"""Check the size again when it is a session's only failed check.

MEASURED 2026-10-28: 'New scenarios' session 60f14a6c kept 3 scenarios that passed
every other check (unit tests, on device), but 961 changed lines against a flat 800
left it unapprovable: the page offered only Reject.
"""
import pytest

from automation.agentic import settings
from automation.claude_code import runner, session
from automation.database.config import Base, SessionLocal, engine
from automation.database.models import AgentSession

OK = [{"name": n, "ok": True, "detail": ""} for n in ("scope", "compiles", "flows load", "unit tests")]
SIZE = {"name": "size", "ok": False, "detail": "961 changed lines (limit 800)"}
FILES = [{"path": "automation/scenarios/cross_app_flows.py", "added": 446, "removed": 9},
         {"path": "tests/test_cancel_event.py", "added": 506, "removed": 0}]


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.create_all(bind=engine)
    yield
    with SessionLocal() as db:
        db.query(AgentSession).delete()
        db.commit()


@pytest.fixture
def failed(monkeypatch, tmp_path):
    from automation.claude_code import paths
    monkeypatch.setattr(paths, "session_dir", lambda sid: str(tmp_path))

    def make(gates=OK + [SIZE], status="gates_failed", limit=800):
        with SessionLocal() as db:
            db.add(AgentSession(id="s-big", kind="generate", status=status, gates=gates,
                                files_changed=FILES, base_commit="base1",
                                branch="agent/x-generate"))
            db.commit()
        monkeypatch.setattr(session, "_git", lambda *a, **k: "  agent/x-generate\n")
        monkeypatch.setattr(settings, "get", lambda: {"max_changed_lines": limit})
        flows = {"base1": {"flows": {"flow1": ["a"]}},
                 "agent/x-generate": {"flows": {"flow1": ["a"], "cc_a": [], "cc_b": [], "cc_c": []}}}
        monkeypatch.setattr(runner, "flows_at", lambda c: flows["agent/x-generate" if c.strip().startswith("agent") else c])
        return "s-big"
    return make


def test_three_new_scenarios_make_the_session_approvable(failed):
    out = session.recheck_size(failed(), "admin")
    assert out["ok"] and out["status"] == "review" and out["new_flows"] == ["cc_a", "cc_b", "cc_c"]
    s = session.get("s-big")
    assert s["status"] == "review" and s["phase"] == "waiting for your approval"
    size = [g for g in s["gates"] if g["name"] == "size"][0]
    assert size["ok"] and size["detail"].startswith("961 changed lines (limit 800 + 3 new scenarios")
    assert "checked again by admin" in size["detail"]
    assert [g["name"] for g in s["gates"]] == [g["name"] for g in OK + [SIZE]]


def test_still_too_big_stays_unapprovable(failed):
    out = session.recheck_size(failed(limit=-300), "")
    assert not out["ok"] and session.get("s-big")["status"] == "gates_failed"


def test_any_other_failed_check_is_not_waved_through(failed):
    sid = failed(gates=OK[:3] + [{"name": "unit tests", "ok": False, "detail": "NEW failures"}, SIZE])
    with pytest.raises(session.Refused, match="unit tests"):
        session.recheck_size(sid)
    assert session.get(sid)["status"] == "gates_failed"


def test_only_a_failed_session_is_checked_again(failed):
    with pytest.raises(session.Refused, match="'review'"):
        session.recheck_size(failed(status="review"))
