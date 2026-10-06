"""Phase 4F.5 — the agent's scenario path no longer reads TestProject.

_resolve_run() read exactly ONE project field, app_bundle_id. The job payload now
carries it, so run_scenario_headless() needs no Session and the agent queries
nothing. Backend callers still pass a session and behave exactly as before.
"""
import ast
import inspect

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from automation.agent import main as agent_main
from automation.api.v1.routers import jobs as jr
from automation.scenarios import run_records  # noqa: F401 — backend run bookkeeping
from automation.scenarios import service as sc
from automation.database.models import (Base, ExecutionAgent, TestProject, TestRun)


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'pp.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    import automation.database.config as cfg
    monkeypatch.setattr(cfg, "SessionLocal", Session)
    db = Session()
    db.add(TestProject(id="proj-1", name="Demo", git_url="https://example.com/x.git",
                       default_branch="main", platform="ios",
                       app_bundle_id="com.example.app"))
    db.add(ExecutionAgent(id="mac-a", hostname="mac-a", os="Darwin", status="online"))
    db.commit()
    yield db, Session
    db.close()


# ── The payload carries the field (1) ────────────────────────────────────────

def test_01_the_poll_payload_carries_the_app_bundle_id(env):
    db, _ = env
    db.add(TestRun(id="run-1", project_id="proj-1", test_suite="s", test_name="t",
                   status="queued", job_state="queued", device_name="UDID-X"))
    db.commit()

    req = jr.PollJobRequest(agent_id="mac-a", connected_devices=["UDID-X"])
    payload = jr.poll_job(req, db=db, _agent="agent")
    assert payload["job_id"] == "run-1"
    assert payload["app_bundle_id"] == "com.example.app"


def test_01b_a_project_without_a_bundle_id_sends_none_not_a_guess(env):
    db, _ = env
    db.add(TestProject(id="proj-2", name="NoBundle", git_url="u"))
    db.add(TestRun(id="run-2", project_id="proj-2", test_suite="s", test_name="t",
                   status="queued", job_state="queued", device_name="UDID-X"))
    db.commit()
    payload = jr.poll_job(jr.PollJobRequest(agent_id="mac-a", connected_devices=["UDID-X"]),
                          db=db, _agent="agent")
    assert payload["app_bundle_id"] is None


# ── The real path runs with no session (2, 3, 5, 8) ─────────────────────────

def test_02_planned_scenarios_execute_without_a_database_session(env, monkeypatch):
    """The real function, with SessionLocal made to explode if anything opens one."""
    db, _ = env
    import automation.database.config as cfg

    def _no_sessions():
        raise AssertionError("the agent opened a database session")
    monkeypatch.setattr(cfg, "SessionLocal", _no_sessions)

    reported = []
    monkeypatch.setattr(agent_main, "report_scenario_results",
                        lambda run_id, results: reported.append(results) or len(results))
    seen = {}
    monkeypatch.setattr(sc, "run_scenario_headless",
                        lambda req, db=None, run_id=None: seen.update(bundle=req.bundle_id, db=db)
                        or {"ok": True, "steps": [], "error": None})

    out = agent_main._run_planned_scenarios(
        "run-1", "proj-1", "UDID-X", [{"name": "Login", "steps": ["tap"]}],
        app_bundle_id="com.example.app")

    assert out["status"] == "passed"
    assert seen["bundle"] == "com.example.app", "the payload bundle id was passed through"
    assert seen["db"] is None, "no session was handed to the runner"
    assert reported and reported[0][0]["status"] == "PASS"


def test_03_no_testproject_query_happens_in_the_agent_path():
    """Structural: the agent must not reference TestProject at all."""
    tree = ast.parse(inspect.getsource(agent_main))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for al in node.names:
                imported.add(f"{node.module}.{al.name}")
    assert "automation.database.models.TestProject" not in imported
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "TestProject" not in names, "the agent still references TestProject"


def test_03b_the_planned_scenario_function_opens_no_session():
    fn_src = inspect.getsource(agent_main._run_planned_scenarios)
    tree = ast.parse(fn_src).body[0]
    called = {getattr(n.func, "id", None) or getattr(n.func, "attr", None)
              for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert "SessionLocal" not in called, "_run_planned_scenarios opened a session"


def test_05_results_still_go_through_the_http_sink(env, monkeypatch):
    db, _ = env
    posted = []
    monkeypatch.setattr(agent_main, "report_scenario_results",
                        lambda run_id, results: posted.append((run_id, results)) or len(results))
    monkeypatch.setattr(sc, "run_scenario_headless",
                        lambda req, db=None, run_id=None: {"ok": True, "steps": [], "error": None})
    agent_main._run_planned_scenarios("run-1", "proj-1", "UDID-X",
                                      [{"name": "S", "steps": ["tap"]}],
                                      app_bundle_id="com.example.app")
    assert posted and posted[0][0] == "run-1", "4F.3's HTTP path is still used"


# ── Deterministic behaviour without a bundle id (4) ─────────────────────────

def test_04_a_missing_bundle_id_fails_loudly_and_never_silently_passes(env, monkeypatch):
    db, _ = env
    posted = []
    monkeypatch.setattr(agent_main, "report_scenario_results",
                        lambda run_id, results: posted.append(results) or len(results))
    called = []
    monkeypatch.setattr(sc, "run_scenario_headless",
                        lambda req, db=None, run_id=None: called.append(1) or {"ok": True})

    out = agent_main._run_planned_scenarios(
        "run-1", "proj-1", "UDID-X",
        [{"name": "A", "steps": ["x"]}, {"name": "B", "steps": ["y"]}],
        app_bundle_id=None)

    assert out["status"] == "failed", "must never report a pass without an app"
    assert called == [], "no scenario was driven"
    assert "bundle id" in out["detail"].lower()
    assert len(posted[0]) == 2 and all(r["status"] == "FAIL" for r in posted[0])
    assert all("bundle id" in (r["error"] or "").lower() for r in posted[0]), \
        "the reason is explicit, not a mystery failure"


# ── Backend callers unchanged (6, 7) ────────────────────────────────────────

def test_06_backend_callers_still_resolve_through_the_database(env):
    """A backend caller passes a session and still gets the project's bundle id."""
    db, _ = env
    req = sc.ScenarioRequest(project_id="proj-1", steps=["tap Login"], device_id="UDID-X")
    bundle_id, steps, repo_path = sc.resolve_run(req, db)
    assert bundle_id == "com.example.app", "resolved from TestProject, as before"
    assert steps == ["tap Login"] and repo_path


def test_06b_a_backend_caller_with_an_unknown_project_still_gets_404(env):
    """The service raises a domain error; the router still turns it into the
    same HTTP 404 the API has always returned (Phase 4F.4)."""
    from automation.api.v1.routers import scenario as router

    db, _ = env
    req = sc.ScenarioRequest(project_id="nope", steps=["tap"], device_id="UDID-X")
    with pytest.raises(sc.ScenarioError) as e:
        sc.resolve_run(req, db)
    assert e.value.status == 404

    with pytest.raises(HTTPException) as h:
        router._resolve_run(req, db)
    assert h.value.status_code == 404


def test_07_resolving_without_a_session_uses_the_supplied_bundle_id(env):
    req = sc.ScenarioRequest(project_id="proj-1", steps=["tap"], device_id="UDID-X",
                             bundle_id="com.supplied.app")
    bundle_id, steps, _ = sc.resolve_run(req)          # no db at all
    assert bundle_id == "com.supplied.app"


def test_07b_no_session_and_no_bundle_id_is_a_clear_error(env):
    req = sc.ScenarioRequest(project_id="proj-1", steps=["tap"], device_id="UDID-X")
    with pytest.raises(sc.ScenarioError) as e:
        sc.resolve_run(req)
    assert e.value.status == 400
    assert "bundle id" in e.value.detail.lower()


def test_07c_backend_router_callers_are_untouched():
    """tickets/workflow/pr_autotest still pass a session — Phase 5 of the brief."""
    from automation.api.v1.routers import tickets, workflow
    from automation.intelligence import pr_autotest
    for mod in (tickets, workflow, pr_autotest):
        src = inspect.getsource(mod)
        assert "run_scenario_headless(req, db)" in src or "run_scenario_headless(" in src


# ── Boundaries this phase must not cross ────────────────────────────────────

def test_the_4f3_uniqueness_defect_is_still_recorded():
    import tests.test_scenario_results_api as t
    assert hasattr(t, "test_24b_the_missing_unique_constraint_is_a_known_gap")
    from automation.database.models import ScenarioResult
    pairs = [tuple(c.name for c in con.columns)
             for con in ScenarioResult.__table__.constraints
             if con.__class__.__name__ == "UniqueConstraint"]
    assert ("run_id", "scenario_num") not in pairs, "no constraint was added in 4F.5"


def test_the_router_extraction_happened_in_4f4():
    """4F.4 done: the agent imports the execution service, never the router."""
    src = inspect.getsource(agent_main)
    assert "automation.api.v1.routers.scenario" not in src, "the agent still reaches the router"
    assert "automation.scenarios.service" in src


def test_preparation_still_reads_testproject_and_is_out_of_scope():
    """Documented, not fixed: the other agent-side project dependency."""
    from automation.projects import preparation
    src = inspect.getsource(preparation)
    assert "TestProject" in src and "SessionLocal" in src


# ── Phase 4F.4a: the agent path no longer opens sessions for run bookkeeping ──

def test_the_agent_run_opens_no_session_for_run_bookkeeping(env, monkeypatch):
    """The flip side of the dependency Phase 4F.4's inventory found.

    4F.5 removed the Session the agent *passed in* for project resolution, but
    _scenario_events() still opened its OWN for TestRun bookkeeping:

        _persist_run_start()     inserts a second TestRun
        _persist_run_finish()    updates it

    4F.4a gives _scenario_events() the caller's run id. The agent passes its
    job_id — it already owns that TestRun — so neither of those runs, and the
    agent process opens no session at all. Finish is reported over the
    authenticated status API instead.

    Drives the REAL runner with SessionLocal booby-trapped.
    """
    import automation.database.config as cfg

    opened = []

    def _tracking():
        opened.append(True)
        raise AssertionError("session opened")

    monkeypatch.setattr(cfg, "SessionLocal", _tracking)
    monkeypatch.setattr(agent_main, "report_scenario_results", lambda r, res: len(res))

    # The real runner — no stub on run_scenario_headless this time.
    agent_main._run_planned_scenarios(
        "run-x", "proj-1", "UDID-X", [{"name": "Login", "steps": ["tap Login"]}],
        app_bundle_id="com.example.app")

    assert not opened, (
        "the agent opened a database session while running planned scenarios — "
        "4F.4a's whole point is that it reuses the job's TestRun instead")


def test_02_scope_note_project_resolution_only():
    """What 4F.5 actually guarantees, stated precisely.

    The agent supplies bundle_id and passes no Session for project resolution;
    resolve_run() queries no TestProject. It does NOT mean the execution path
    opens no session at all — see the test above.
    """
    import inspect
    src = inspect.getsource(sc.resolve_run)
    assert "db is not None" in src, "the project lookup is conditional"
    # 4F.4 moved the lookup itself into the backend-only store, so the service
    # names no model at all — it delegates only when a session was passed.
    assert "TestProject" not in src
    assert "_run_store.project_bundle_id" in src
