"""Phase 4F.7A — the orphan reaper asks the backend, not the database.

active_job_ids() ran its own SQLAlchemy query from inside the agent. It now calls
an injected source; the agent registers one that uses its authenticated session.

This is a PURE BOUNDARY MOVE. The endpoint returns the GLOBAL active set, exactly
as the query did — per-agent scoping is 4F.7B and is deliberately not here,
because narrowing the set would let one agent reap another's live work.

The property that matters most: every failure must mean UNKNOWN, never "nothing
is running". An empty set is a licence to kill.
"""
import ast
import inspect
import subprocess
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from automation.agent import main as agent_main
from automation.api.v1.routers import agents as ar
from automation.auth import security
from automation.database.models import Base, TestProject, TestRun
from automation.utils import proctree


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'pt.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    import automation.database.config as cfg
    monkeypatch.setattr(cfg, "SessionLocal", Session)
    import automation.device_manager.service as dm
    monkeypatch.setattr(dm, "_backend_machine_id", None)
    db = Session()
    db.add(TestProject(id="proj-1", name="D", git_url="https://e/x.git"))
    db.commit()
    yield db, Session
    db.close()


def _agent(db, hostname):
    out = ar.register_agent(ar.RegisterAgentRequest(hostname=hostname, os="Darwin",
                                                    capabilities={}, connected_devices=[]), db=db)
    return out["id"], out["agent_credential"]


def _auth(db, cred):
    return security.authenticated_agent(request=None, x_agent_credential=cred, db=db)


def _run(db, run_id, job_state, agent_id=None):
    db.add(TestRun(id=run_id, project_id="proj-1", test_suite="s", test_name=run_id,
                   status="running", job_state=job_state, device_name="UDID-X",
                   agent_id=agent_id))
    db.commit()


class _Res:
    def __init__(self, status=200, body=None, raises=None):
        self.status_code, self._body, self._raises = status, body, raises
    def json(self):
        if self._raises is not None:
            raise self._raises
        return self._body


def _http(monkeypatch, result):
    """Point the agent's session at a scripted result (a _Res or an exception)."""
    def _get(url, timeout=None):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(agent_main.HTTP, "get", _get)


# ── 1. the API returns the same global set the query did ────────────────────

def test_01_the_endpoint_returns_every_active_job_state(env):
    db, _ = env
    a, cred = _agent(db, "mac-a")
    for i, state in enumerate(sorted(proctree.ACTIVE_JOB_STATES)):
        _run(db, f"active-{i}", state, a)
    _run(db, "done-1", "passed", a)
    _run(db, "done-2", "failed", a)

    out = ar.my_active_jobs(db=db, agent=_auth(db, cred))
    assert set(out["job_ids"]) == {f"active-{i}" for i in range(len(proctree.ACTIVE_JOB_STATES))}
    assert "done-1" not in out["job_ids"] and "done-2" not in out["job_ids"]


def test_01b_the_set_is_global_not_per_agent(env):
    """4F.7A preserves global scope on purpose. 4F.7B is where this changes."""
    db, _ = env
    a, ca = _agent(db, "mac-a")
    b, _cb = _agent(db, "mac-b")
    _run(db, "mine", "running", a)
    _run(db, "theirs", "running", b)
    _run(db, "unclaimed", "queued", None)

    out = ar.my_active_jobs(db=db, agent=_auth(db, ca))
    assert set(out["job_ids"]) == {"mine", "theirs", "unclaimed"}, \
        "4F.7A must not narrow the set — that is 4F.7B"


def test_01c_the_endpoint_requires_a_proven_agent():
    dep = inspect.signature(ar.my_active_jobs).parameters["agent"].default
    assert dep.dependency is security.require_authenticated_agent


def test_01d_both_sides_share_one_definition_of_active():
    """The endpoint imports ACTIVE_JOB_STATES rather than restating it."""
    src = inspect.getsource(ar.my_active_jobs)
    assert "ACTIVE_JOB_STATES" in src
    assert proctree.ACTIVE_JOB_STATES == frozenset({
        "queued", "assigned", "downloading", "preparing", "running", "collecting_evidence"})


# ── 2. the agent retrieves them ─────────────────────────────────────────────

def test_02_the_agent_reads_the_job_ids_over_its_session(monkeypatch):
    seen = {}

    def _get(url, timeout=None):
        seen["url"], seen["timeout"] = url, timeout
        return _Res(200, {"job_ids": ["run-1", "run-2"]})
    monkeypatch.setattr(agent_main.HTTP, "get", _get)

    assert agent_main.fetch_active_job_ids() == {"run-1", "run-2"}
    assert seen["url"].endswith("/agents/me/active-jobs")
    assert seen["timeout"] is not None, "an unbounded call would hang the sweep"


def test_02b_the_agent_registers_itself_as_the_source():
    assert proctree._active_jobs_source is agent_main.fetch_active_job_ids


def test_02c_ids_are_normalised_to_strings(monkeypatch):
    _http(monkeypatch, _Res(200, {"job_ids": [1, "two"]}))
    assert agent_main.fetch_active_job_ids() == {"1", "two"}


def test_02d_an_empty_backend_list_is_a_real_empty_set(monkeypatch):
    """Distinct from failure: the backend said so, and it is allowed to say so."""
    _http(monkeypatch, _Res(200, {"job_ids": []}))
    assert agent_main.fetch_active_job_ids() == set()


# ── 3. an active job still protects its processes ───────────────────────────

def _entry(job_id="job-1", pid=None, kind="appium", age=10_000):
    import os as _os, time as _time
    pid = _os.getpid() if pid is None else pid
    return {"pid": pid, "pgid": _os.getpgid(pid), "job_id": job_id, "kind": kind,
            "identity": proctree._proc_identity(pid), "cmd": ["x"],
            "recorded_at": _time.time() - age}


def test_03_an_active_job_is_never_reapable():
    verdict, reason = proctree._classify(_entry("job-1"), {"job-1"}, __import__("time").time())
    assert verdict == "DO_NOT_REAP" and "still active" in reason


def test_03b_the_injection_seam_still_bypasses_the_source(monkeypatch, tmp_path):
    """sweep_orphans(active=...) must keep working — tests and callers rely on it."""
    monkeypatch.setattr(proctree, "REGISTRY_PATH", str(tmp_path / "reg.json"))
    monkeypatch.setattr(proctree, "_active_jobs_source",
                        lambda: pytest.fail("the source must not be consulted"))
    rep = proctree.sweep_orphans(dry_run=True, active={"job-1"})
    assert rep["available"] is True


# ── 4-11. every failure mode is SweepUnavailable, never an empty set ────────

@pytest.mark.parametrize("label,result", [
    ("network failure", ConnectionError("connection refused")),
    ("timeout", TimeoutError("timed out")),
    ("HTTP 500", _Res(500, {})),
    ("HTTP 401", _Res(401, {})),
    ("HTTP 403", _Res(403, {})),
    ("HTTP 404", _Res(404, {})),
    ("malformed JSON", _Res(200, None, raises=ValueError("no json"))),
    ("missing job_ids", _Res(200, {"other": []})),
    ("job_ids not a list", _Res(200, {"job_ids": "run-1"})),
    ("job_ids is a dict", _Res(200, {"job_ids": {"a": 1}})),
    ("body is not an object", _Res(200, ["run-1"])),
])
def test_04_to_11_every_failure_is_unavailable_not_empty(monkeypatch, label, result):
    _http(monkeypatch, result)
    with pytest.raises(proctree.SweepUnavailable):
        agent_main.fetch_active_job_ids()


def test_11b_an_unregistered_source_is_also_unavailable(monkeypatch):
    monkeypatch.setattr(proctree, "_active_jobs_source", None)
    with pytest.raises(proctree.SweepUnavailable):
        proctree.active_job_ids()


def test_11c_a_source_returning_a_non_collection_is_unavailable(monkeypatch):
    for bad in ("run-1", 7, None, object()):
        monkeypatch.setattr(proctree, "_active_jobs_source", lambda b=bad: b)
        with pytest.raises(proctree.SweepUnavailable):
            proctree.active_job_ids()


def test_11d_an_unexpected_client_error_is_wrapped_not_leaked(monkeypatch):
    monkeypatch.setattr(proctree, "_active_jobs_source",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(proctree.SweepUnavailable):
        proctree.active_job_ids()


# ── 12. fail-closed reaches the verdict, not just the exception ─────────────

def test_12_a_failing_source_makes_every_entry_unknown(monkeypatch, tmp_path):
    reg = tmp_path / "reg.json"
    monkeypatch.setattr(proctree, "REGISTRY_PATH", str(reg))
    import json as _json
    e = _entry("job-1")
    reg.write_text(_json.dumps({str(e["pid"]): e}))

    monkeypatch.setattr(proctree, "_active_jobs_source",
                        lambda: (_ for _ in ()).throw(proctree.SweepUnavailable("down")))

    rep = proctree.sweep_orphans(dry_run=True)
    assert rep["available"] is False
    assert rep["would_reap"] == [] and rep["signals_sent"] == 0
    assert [r["verdict"] for r in rep["entries"]] == ["UNKNOWN"]
    assert rep["entries"][0]["job_active"] is None, "unknown must not look like inactive"


def test_12b_a_backend_outage_never_produces_an_empty_active_set(monkeypatch, tmp_path):
    """The negative-control target: silently returning set() would reap live work."""
    monkeypatch.setattr(proctree, "REGISTRY_PATH", str(tmp_path / "reg.json"))
    _http(monkeypatch, ConnectionError("refused"))
    monkeypatch.setattr(proctree, "_active_jobs_source", agent_main.fetch_active_job_ids)

    rep = proctree.sweep_orphans(dry_run=True)
    assert rep["available"] is False
    assert "unavailable_reason" in rep and rep["unavailable_reason"]


# ── 13. no database dependency ──────────────────────────────────────────────

def test_13_proctree_import_graph_has_no_database_or_web_framework():
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys, automation.utils.proctree; print(chr(10).join(sys.modules))"],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-2000:]
    g = set(out.stdout.split())
    offenders = sorted(m for m in g
                       if m.split(".")[0] in ("fastapi", "sqlalchemy", "requests")
                       or m.startswith("automation.database")
                       or m.startswith("automation.api"))
    assert not offenders, f"proctree pulls in {offenders}"


def test_13b_proctree_names_no_session_or_model():
    tree = ast.parse(inspect.getsource(proctree))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body = node.body[1:] or [ast.Pass()]
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for banned in ("SessionLocal", "TestRun", "get_db"):
        assert banned not in names, f"proctree still references {banned}"
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("automation.database")


# ── 15/16. dry-run and the reaping defaults are untouched ───────────────────

def test_15_dry_run_still_sends_no_signals(monkeypatch, tmp_path):
    reg = tmp_path / "reg.json"
    monkeypatch.setattr(proctree, "REGISTRY_PATH", str(reg))
    import json as _json
    e = _entry("gone-job")
    reg.write_text(_json.dumps({str(e["pid"]): e}))
    monkeypatch.setattr(proctree, "_active_jobs_source", lambda: set())

    rep = proctree.sweep_orphans(dry_run=True)
    assert rep["signals_sent"] == 0 and rep["reaped"] == []


def test_16_reaping_is_still_disabled_by_default(monkeypatch):
    for var in ("PROC_SWEEP_ENABLED", "PROC_SWEEP_ALLOW_REAP", "PROC_SWEEP_DRY_RUN"):
        monkeypatch.delenv(var, raising=False)
    allowed = proctree.reaping_allowed()
    assert not (allowed[0] if isinstance(allowed, tuple) else allowed), \
        "reaping must stay off out of the box"


def test_16b_the_tuning_constants_are_unchanged():
    assert proctree.SWEEP_MIN_AGE_SEC == 900.0
    assert proctree.NEVER_REAP == frozenset({"metro"})
