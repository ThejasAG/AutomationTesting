"""Guard for the regression Phase 4F.4's audit found in Phase 4F.3's code.

4F.3 removed the `with SessionLocal() as db:` that wrapped _run_planned_scenarios,
because the ScenarioResult writes lived inside it. That session was ALSO the one
passed to run_scenario_headless(req, db) — so `db` became an undefined name.

It sat inside `except Exception`, so nothing crashed: every planned scenario would
have been silently recorded as FAIL with "name 'db' is not defined". The existing
tests missed it because they monkeypatch _run_planned_scenarios wholesale.

These tests execute the real function.
"""
import ast
import inspect

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from automation.agent import main as agent_main
from automation.database.models import Base, TestProject


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'p.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    import automation.database.config as cfg
    monkeypatch.setattr(cfg, "SessionLocal", Session)
    db = Session()
    db.add(TestProject(id="proj-1", name="Demo", git_url="https://example.com/x.git",
                       app_bundle_id="com.example.app"))
    db.commit()
    yield db
    db.close()


def test_every_name_used_in_run_planned_scenarios_is_defined():
    """Static guard: the exact defect, caught without executing anything."""
    fn = ast.parse(inspect.getsource(agent_main._run_planned_scenarios)).body[0]

    bound = {a.arg for a in fn.args.args}
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.withitem) and isinstance(node.optional_vars, ast.Name):
            bound.add(node.optional_vars.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for al in node.names:
                bound.add(al.asname or al.name.split(".")[0])
        elif isinstance(node, ast.comprehension) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)

    import builtins
    module_level = set(dir(agent_main)) | set(dir(builtins))
    used = {n.id for n in ast.walk(fn)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    undefined = used - bound - module_level
    assert not undefined, f"_run_planned_scenarios uses undefined name(s): {undefined}"


def test_planned_scenarios_actually_execute(env, monkeypatch):
    """Run the REAL function: a passing scenario must be reported as PASS."""
    calls = []
    monkeypatch.setattr(
        agent_main, "report_scenario_results",
        lambda run_id, results: calls.append((run_id, results)) or len(results))

    import automation.scenarios.service as scenario
    monkeypatch.setattr(scenario, "run_scenario_headless",
                        lambda req, db=None, run_id=None: {"ok": True, "steps": [{"step": "tap", "ok": True}],
                                              "error": None})

    out = agent_main._run_planned_scenarios(
        "run-1", "proj-1", "UDID-X",
        [{"name": "Login", "steps": ["tap Login"]},
         {"name": "Checkout", "steps": ["tap Pay"]}],
        app_bundle_id="com.example.app")

    assert out["status"] == "passed", f"a passing plan must not report failure: {out}"
    assert calls, "results were reported"
    run_id, results = calls[0]
    assert run_id == "run-1" and len(results) == 2
    assert {r["status"] for r in results} == {"PASS"}
    for r in results:
        assert r.get("error") is None, \
            f"a passing scenario recorded an error — something was swallowed: {r.get('error')}"


def test_a_failing_scenario_is_still_reported_as_failed(env, monkeypatch):
    """The guard must not mask genuine failures."""
    calls = []
    monkeypatch.setattr(
        agent_main, "report_scenario_results",
        lambda run_id, results: calls.append(results) or len(results))
    import automation.scenarios.service as scenario
    monkeypatch.setattr(scenario, "run_scenario_headless",
                        lambda req, db=None, run_id=None: {"ok": False, "steps": [], "error": "element not found"})

    out = agent_main._run_planned_scenarios(
        "run-2", "proj-1", "UDID-X", [{"name": "Login", "steps": ["tap Login"]}],
        app_bundle_id="com.example.app")

    assert out["status"] == "failed"
    assert calls[0][0]["status"] == "FAIL"
    assert "element not found" in (calls[0][0]["error"] or "")
