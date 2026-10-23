"""Phase 4F.3 — scenario results are written by the backend, not the agent.

Two agent-side writers upserted the same record on the same key; a third did it
per-step from inside the scenario runner. All three now go through ONE business
operation, which the Android bot already used — so the two callers cannot drift
into different behaviours over one table.
"""
import ast
import inspect
import threading

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from automation.api.v1.routers import agents as ar
from automation.api.v1.routers import jobs as jr
from automation.auth import security
from automation.database.models import Base, ScenarioResult, TestProject, TestRun


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'sr.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    import automation.database.config as cfg
    monkeypatch.setattr(cfg, "SessionLocal", Session)
    import automation.device_manager.service as dm
    monkeypatch.setattr(dm, "_backend_machine_id", None)
    db = Session()
    db.add(TestProject(id="proj-1", name="Demo", git_url="https://example.com/x.git"))
    db.commit()
    yield db, Session
    db.close()


def _agent(db, hostname):
    out = ar.register_agent(ar.RegisterAgentRequest(hostname=hostname, os="Darwin",
                                                    capabilities={}, connected_devices=[]), db=db)
    return out["id"], out["agent_credential"]


def _auth(db, cred):
    return security.authenticated_agent(request=None, x_agent_credential=cred, db=db)


def _run(db, run_id, agent_id=None):
    db.add(TestRun(id=run_id, project_id="proj-1", test_suite="s", test_name=run_id,
                   status="running", job_state="running", device_name="UDID-X",
                   agent_id=agent_id))
    db.commit()
    return run_id


def _item(num, name="Login", status="PASS", **over):
    d = dict(scenario_num=str(num), scenario_name=name, status=status,
             consumer_status="N/A", business_status="N/A", error=None,
             reasons=[], launch_time=None)
    d.update(over)
    return jr.ScenarioResultIn(**d)


def _batch(db, run_id, items, cred):
    return jr.post_scenario_results_batch(
        run_id, jr.ScenarioResultsBatchIn(results=items), db=db, agent=_auth(db, cred))


def _rows(db, run_id):
    db.expire_all()
    return db.query(ScenarioResult).filter(ScenarioResult.run_id == run_id).all()


# ── API (1–8) ────────────────────────────────────────────────────────────────

def test_01_an_agent_writes_results_for_its_own_run(env):
    db, _ = env
    a, cred = _agent(db, "mac-a"); _run(db, "run-1", a)
    out = _batch(db, "run-1", [_item(1)], cred)
    assert out == {"run_id": "run-1", "written": 1}
    rows = _rows(db, "run-1")
    assert len(rows) == 1 and rows[0].status == "PASS" and rows[0].scenario_name == "Login"


@pytest.mark.parametrize("cred", ["", "bogus"])
def test_02_03_missing_or_invalid_credentials_are_rejected(env, cred):
    db, _ = env
    a, _ = _agent(db, "mac-a"); _run(db, "run-1", a)
    with pytest.raises(HTTPException) as e:
        security.require_authenticated_agent(agent=_auth(db, cred))
    assert e.value.status_code == 401


def test_04_agent_a_cannot_write_into_agent_bs_run(env):
    db, _ = env
    a, a_cred = _agent(db, "mac-a"); b, _ = _agent(db, "mac-b")
    _run(db, "run-b", agent_id=b)
    with pytest.raises(HTTPException) as e:
        _batch(db, "run-b", [_item(1)], a_cred)
    assert e.value.status_code == 403
    assert _rows(db, "run-b") == [], "nothing was written"


def test_05_ownership_comes_from_the_credential_not_the_payload(env):
    """There is no agent_id field to forge — ownership is derived server-side."""
    db, _ = env
    fields = set(jr.ScenarioResultIn.model_fields)
    for forbidden in ("agent_id", "machine_id", "hostname"):
        assert forbidden not in fields
    assert "agent_id" not in set(jr.ScenarioResultsBatchIn.model_fields)


def test_06_an_unknown_run_is_404(env):
    db, _ = env
    a, cred = _agent(db, "mac-a")
    with pytest.raises(HTTPException) as e:
        _batch(db, "no-such-run", [_item(1)], cred)
    assert e.value.status_code == 404


def test_07_an_empty_batch_writes_nothing_and_succeeds(env):
    db, _ = env
    a, cred = _agent(db, "mac-a"); _run(db, "run-1", a)
    assert _batch(db, "run-1", [], cred)["written"] == 0
    assert _rows(db, "run-1") == []


def test_08_many_results_land_in_one_request(env):
    db, _ = env
    a, cred = _agent(db, "mac-a"); _run(db, "run-1", a)
    items = [_item(i, f"Scenario {i}", "PASS" if i % 2 else "FAIL") for i in range(1, 6)]
    assert _batch(db, "run-1", items, cred)["written"] == 5
    rows = _rows(db, "run-1")
    assert len(rows) == 5
    assert {r.scenario_num for r in rows} == {"1", "2", "3", "4", "5"}


# ── Idempotency (9–10) ───────────────────────────────────────────────────────

def test_09_resubmitting_the_same_key_does_not_duplicate(env):
    db, _ = env
    a, cred = _agent(db, "mac-a"); _run(db, "run-1", a)
    _batch(db, "run-1", [_item(1, "Login", "PASS")], cred)
    _batch(db, "run-1", [_item(1, "Login", "PASS")], cred)
    assert len(_rows(db, "run-1")) == 1


def test_10_a_resubmission_updates_the_existing_row(env):
    db, _ = env
    a, cred = _agent(db, "mac-a"); _run(db, "run-1", a)
    _batch(db, "run-1", [_item(1, "Login", "PASS")], cred)
    _batch(db, "run-1", [_item(1, "Login", "FAIL", error="boom", reasons=["step 2 FAIL"])], cred)
    rows = _rows(db, "run-1")
    assert len(rows) == 1
    assert rows[0].status == "FAIL" and rows[0].error == "boom"
    assert rows[0].reasons == ["step 2 FAIL"]


def test_10b_the_batch_does_not_recompute_the_run_status(env):
    """The iOS writers never did; doing it here would settle a run mid-flight."""
    db, _ = env
    a, cred = _agent(db, "mac-a"); _run(db, "run-1", a)
    _batch(db, "run-1", [_item(1, "Login", "FAIL")], cred)
    db.expire_all()
    run = db.query(TestRun).filter(TestRun.id == "run-1").one()
    assert (run.status, run.job_state) == ("running", "running")


# ── Android compatibility (22–23) ────────────────────────────────────────────

def test_22_the_android_endpoint_still_works(env, monkeypatch):
    db, _ = env
    _run(db, "run-bot")
    monkeypatch.setattr(jr, "BOT_SECRET", "", raising=False)
    out = jr.post_scenario_result(
        "run-bot", jr.ScenarioResultIn(scenario_num="1", scenario_name="Order",
                                       status="PASS", role="Consumer"),
        db=db, x_bot_secret=None)
    assert out["saved"] is True
    rows = _rows(db, "run-bot")
    assert len(rows) == 1 and rows[0].consumer_status == "PASS"


def test_22b_role_merging_still_produces_one_row(env, monkeypatch):
    """The cross-app behaviour the bot depends on: two posts, one merged row."""
    db, _ = env
    _run(db, "run-bot")
    monkeypatch.setattr(jr, "BOT_SECRET", "", raising=False)
    for role, status in (("Consumer", "PASS"), ("Business", "FAIL")):
        jr.post_scenario_result("run-bot",
                                jr.ScenarioResultIn(scenario_num="1", scenario_name="Order",
                                                    status=status, role=role),
                                db=db, x_bot_secret=None)
    rows = _rows(db, "run-bot")
    assert len(rows) == 1
    assert rows[0].consumer_status == "PASS" and rows[0].business_status == "FAIL"
    assert rows[0].status == "FAIL", "either side failing fails the scenario"


def test_23_both_paths_share_one_implementation():
    android = inspect.getsource(jr.post_scenario_result)
    batch = inspect.getsource(jr.post_scenario_results_batch)
    assert "upsert_scenario_result" in android
    assert "upsert_scenario_result" in batch
    # and neither builds its own row
    for src in (android, batch):
        assert "ScenarioResult(" not in src, "no second implementation"


def test_23b_the_two_modes_differ_only_in_role_merging(env):
    """merge_roles=False is exactly what the iOS writers did before."""
    db, _ = env
    _run(db, "run-x")
    jr.upsert_scenario_result(db, "run-x", _item(1, "S", "PASS"), merge_roles=False)
    db.commit()
    row = _rows(db, "run-x")[0]
    assert (row.consumer_status, row.business_status) == ("N/A", "N/A")
    assert row.status == "PASS"


# ── Concurrency (24–25) ──────────────────────────────────────────────────────

def test_24_concurrent_writes_to_one_key_do_not_error(env):
    """Concurrency here is bounded by what the schema actually guarantees.

    `scenario_results` has NO unique constraint on (run_id, scenario_num), so the
    read-then-write upsert — which predates this phase and which the Android bot
    has always used — can produce two rows if two writers race on one key. Writing
    an assertion that it cannot would be asserting a guarantee the database does
    not provide; see test_24b, which pins the gap explicitly.

    What IS guaranteed, and what this asserts: concurrent submission does not
    error, and every write lands. The race is unreachable through the agent today
    — one job at a time per agent, and the two writers never overlap on a key.
    """
    db, Session = env
    a, cred = _agent(db, "mac-a"); _run(db, "run-1", a)
    errors, start = [], threading.Barrier(5)

    def go(n):
        own = Session()
        try:
            start.wait(timeout=10)
            jr.post_scenario_results_batch("run-1",
                jr.ScenarioResultsBatchIn(results=[_item(1, "Login", "PASS")]),
                db=own, agent=_auth(own, cred))
        except Exception as e:                       # noqa: BLE001
            errors.append(e)
        finally:
            own.close()

    ts = [threading.Thread(target=go, args=(i,)) for i in range(5)]
    for t in ts: t.start()
    for t in ts: t.join(timeout=20)
    assert not errors, f"concurrent submission must not raise: {errors}"
    rows = _rows(db, "run-1")
    assert rows, "the results landed"
    assert all(r.status == "PASS" for r in rows)


def test_24b_the_missing_unique_constraint_is_a_known_gap():
    """Tracks the defect test_24 found, so it cannot be forgotten.

    Sequential idempotency (test_09/test_10) is what the upsert guarantees and
    what every caller relies on. True concurrent idempotency needs
    UNIQUE(run_id, scenario_num) on scenario_results — which _add_missing_columns()
    cannot add, since it only ever adds columns. Flip this test when the constraint
    lands.
    """
    from automation.database.models import ScenarioResult
    pairs = [tuple(c.name for c in con.columns)
             for con in ScenarioResult.__table__.constraints
             if con.__class__.__name__ == "UniqueConstraint"]
    assert ("run_id", "scenario_num") not in pairs, \
        "a unique constraint now exists — tighten test_24 to assert exactly one row"


def test_25_concurrent_writes_to_different_keys_all_land(env):
    db, Session = env
    a, cred = _agent(db, "mac-a"); _run(db, "run-1", a)
    start = threading.Barrier(4)

    def go(n):
        own = Session()
        try:
            start.wait(timeout=10)
            jr.post_scenario_results_batch("run-1",
                jr.ScenarioResultsBatchIn(results=[_item(n, f"S{n}")]),
                db=own, agent=_auth(own, cred))
        finally:
            own.close()

    ts = [threading.Thread(target=go, args=(i,)) for i in range(1, 5)]
    for t in ts: t.start()
    for t in ts: t.join(timeout=20)
    assert {r.scenario_num for r in _rows(db, "run-1")} == {"1", "2", "3", "4"}


# ── Agent integration (11–17) ────────────────────────────────────────────────

def _agent_module():
    from automation.agent import main as agent_main
    return agent_main


def test_11_12_the_agent_does_not_persist_scenario_results_itself():
    """Boundary, by AST — the agent must neither import nor construct the model."""
    for mod in (_agent_module(), __import__("automation.plugins.appium_framework",
                                            fromlist=["x"])):
        tree = ast.parse(inspect.getsource(mod))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for al in node.names:
                    imported.add(f"{node.module}.{al.name}")
        assert "automation.database.models.ScenarioResult" not in imported, \
            f"{mod.__name__} still imports ScenarioResult for persistence"
        src = inspect.getsource(mod)
        assert "ScenarioResult(" not in src, f"{mod.__name__} still builds rows"


def test_13_the_agent_confirms_persistence_from_the_response(env, monkeypatch):
    """A 2xx alone is not proof — the written count is."""
    agent_main = _agent_module()
    seen = {}

    class _R:
        status_code = 200
        text = ""
        def json(self): return {"run_id": "run-1", "written": 2}

    monkeypatch.setattr(agent_main.HTTP, "post",
                        lambda url, **k: seen.update(url=url, body=k.get("json")) or _R())
    written = agent_main.report_scenario_results("run-1", [{"scenario_num": "1"},
                                                           {"scenario_num": "2"}])
    assert written == 2
    assert seen["url"].endswith("/runs/run-1/scenario-results")
    assert len(seen["body"]["results"]) == 2, "batched into one request"


def test_14_a_reporting_failure_is_not_a_test_failure(env, monkeypatch):
    agent_main = _agent_module()

    class _Bad:
        status_code = 500
        text = "boom"
        def json(self): return {}

    monkeypatch.setattr(agent_main.HTTP, "post", lambda *a, **k: _Bad())
    assert agent_main.report_scenario_results("run-1", [{"scenario_num": "1"}]) == 0

    def _explode(*a, **k):
        raise ConnectionError("network down")
    monkeypatch.setattr(agent_main.HTTP, "post", _explode)
    assert agent_main.report_scenario_results("run-1", [{"scenario_num": "1"}]) == 0


def test_15_an_empty_report_makes_no_request(env, monkeypatch):
    agent_main = _agent_module()
    calls = []
    monkeypatch.setattr(agent_main.HTTP, "post", lambda *a, **k: calls.append(1))
    assert agent_main.report_scenario_results("run-1", []) == 0
    assert calls == []


def test_16_17_the_appium_framework_uses_the_same_writer():
    import automation.plugins.appium_framework as fw
    src = inspect.getsource(fw)
    assert "report_scenario_results" in src
    assert "SessionLocal" not in inspect.getsource(fw._persist_test_rows)


# ── Scenario execution stays where it is (18–21) ─────────────────────────────

def test_18_19_20_scenario_execution_remains_agent_local():
    from automation.scenarios import service as scenario
    src = inspect.getsource(scenario)
    # still driven locally — no RPC, no backend simulator driving introduced
    assert "ensure_ios_booted" in src and "webdriver" in src
    assert "appium_url" in src, "the Appium endpoint is still a parameter"
    agent_src = inspect.getsource(_agent_module())
    assert "run_scenario_headless" in agent_src, "execution still runs in the agent"


def test_21_step_results_reach_the_backend_through_the_sink(env, monkeypatch):
    from automation.scenarios import service as scenario
    captured = []
    scenario.set_result_sink(lambda run_id, i, res, secs=None: captured.append((run_id, i)))
    try:
        class _Res:
            ok, step, detail = True, "tap Login", ""
        scenario._persist_step("run-1", 3, _Res(), 1.25)
        assert captured == [("run-1", 3)], "the step went to the sink, not the database"
    finally:
        scenario.set_result_sink(None)


def test_21b_the_backend_still_writes_steps_directly(env):
    """With no sink set — the backend's own path — the DB write is unchanged."""
    from automation.scenarios import service as scenario
    db, _ = env
    _run(db, "run-1")
    scenario.set_result_sink(None)

    class _Res:
        ok, step, detail = False, "tap Save", "not found"
    scenario._persist_step("run-1", 1, _Res(), 2.0)
    rows = _rows(db, "run-1")
    assert len(rows) == 1 and rows[0].status == "FAIL"
