"""Phase 4F.2A — the agent's device lifecycle moved behind authenticated HTTP.

Device lookup, reservation, activation, release and WDA port allocation used to
open a SQLAlchemy session inside the agent, which bound it to a local SQLite file
and made a second Mac impossible. They are now backend operations, scoped to the
machine the credential proves — never to a machine the client names.
"""
import ast
import inspect
import pathlib
import threading

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from automation.api.v1.routers import agents as ar
from automation.auth import security
from automation.database.models import (Base, DeviceRecord, ExecutionAgent, TestProject,
                                        TestRun)

UDID_X = "AAAAAAAA-1111-2222-3333-444444444444"
UDID_Y = "BBBBBBBB-5555-6666-7777-888888888888"


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'api.db'}",
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
                                                    capabilities={}, connected_devices=[]),
                            db=db)
    return out["id"], out["agent_credential"]


def _auth(db, cred):
    return security.authenticated_agent(request=None, x_agent_credential=cred, db=db)


def _device(db, machine_id, udid, name="iPhone 16 Pro"):
    row = DeviceRecord(machine_id=machine_id, udid=udid, name=name, platform="iOS")
    db.add(row); db.commit()
    return row.id


def _run(db, run_id, agent_id=None):
    db.add(TestRun(id=run_id, project_id="proj-1", test_suite="s", test_name=run_id,
                   status="queued", job_state="queued", device_name=UDID_X,
                   agent_id=agent_id))
    db.commit()
    return run_id


# ── Authentication (1–5) ─────────────────────────────────────────────────────

def test_01_agent_reaches_its_own_device(env):
    db, _ = env
    a, cred = _agent(db, "mac-a")
    d = _device(db, a, UDID_X)
    out = ar.get_my_device(UDID_X, db=db, agent=_auth(db, cred))
    assert out["device_id"] == d and out["udid"] == UDID_X


def test_02_agent_a_cannot_reach_agent_bs_device(env):
    """The same UDID on two machines: A must get 404, not B's device."""
    db, _ = env
    a, a_cred = _agent(db, "mac-a")
    b, _b_cred = _agent(db, "mac-b")
    d_b = _device(db, b, UDID_X)

    with pytest.raises(HTTPException) as e:
        ar.get_my_device(UDID_X, db=db, agent=_auth(db, a_cred))
    assert e.value.status_code == 404
    assert d_b  # B's device still exists, untouched


def test_03_a_credential_cannot_be_paired_with_another_agents_ids(env):
    """A's credential + B's device_id → 404. Ownership is not a client field."""
    db, _ = env
    a, a_cred = _agent(db, "mac-a")
    b, _ = _agent(db, "mac-b")
    d_b = _device(db, b, UDID_X)
    _run(db, "run-1", agent_id=a)

    with pytest.raises(HTTPException) as e:
        ar.reserve_my_device(d_b, ar.ReservationRequest(run_id="run-1"),
                             db=db, agent=_auth(db, a_cred))
    assert e.value.status_code == 404


@pytest.mark.parametrize("cred", ["", "   ", "bogus-credential"])
def test_04_05_missing_or_invalid_credentials_are_rejected(env, cred):
    db, _ = env
    with pytest.raises(HTTPException) as e:
        security.require_authenticated_agent(agent=_auth(db, cred))
    assert e.value.status_code == 401


# ── Device lookup (6–8) ──────────────────────────────────────────────────────

def test_06_07_08_lookup_is_scoped_to_my_machine(env):
    db, _ = env
    a, a_cred = _agent(db, "mac-a")
    b, _ = _agent(db, "mac-b")
    _device(db, a, UDID_X)
    _device(db, b, UDID_Y)

    assert ar.get_my_device(UDID_X, db=db, agent=_auth(db, a_cred))["udid"] == UDID_X
    for missing in (UDID_Y, "NEVER-REGISTERED"):
        with pytest.raises(HTTPException) as e:
            ar.get_my_device(missing, db=db, agent=_auth(db, a_cred))
        assert e.value.status_code == 404


# ── Reservation (9–15) ───────────────────────────────────────────────────────

def test_09_reservation_succeeds(env):
    db, _ = env
    a, cred = _agent(db, "mac-a"); d = _device(db, a, UDID_X); _run(db, "run-1", a)
    out = ar.reserve_my_device(d, ar.ReservationRequest(run_id="run-1"),
                               db=db, agent=_auth(db, cred))
    assert out["reserved_by"] == "run-1" and out["state"] == "reserved"


def test_10_a_second_run_gets_409(env):
    db, _ = env
    a, cred = _agent(db, "mac-a"); d = _device(db, a, UDID_X)
    _run(db, "run-1", a); _run(db, "run-2", a)
    ar.reserve_my_device(d, ar.ReservationRequest(run_id="run-1"), db=db, agent=_auth(db, cred))
    with pytest.raises(HTTPException) as e:
        ar.reserve_my_device(d, ar.ReservationRequest(run_id="run-2"), db=db, agent=_auth(db, cred))
    assert e.value.status_code == 409, "contention is 409, never a test failure"


def test_11_the_same_run_may_re_enter(env):
    db, _ = env
    a, cred = _agent(db, "mac-a"); d = _device(db, a, UDID_X); _run(db, "run-1", a)
    for _ in range(3):
        ar.reserve_my_device(d, ar.ReservationRequest(run_id="run-1"), db=db, agent=_auth(db, cred))


def test_12_a_foreign_agent_cannot_reserve_my_device(env):
    db, _ = env
    a, a_cred = _agent(db, "mac-a"); b, b_cred = _agent(db, "mac-b")
    d_a = _device(db, a, UDID_X); _run(db, "run-b", b)
    with pytest.raises(HTTPException) as e:
        ar.reserve_my_device(d_a, ar.ReservationRequest(run_id="run-b"),
                             db=db, agent=_auth(db, b_cred))
    assert e.value.status_code == 404


def test_13_cannot_reserve_for_another_agents_run(env):
    db, _ = env
    a, a_cred = _agent(db, "mac-a"); b, _ = _agent(db, "mac-b")
    d_a = _device(db, a, UDID_X)
    _run(db, "run-b", agent_id=b)                  # claimed by B
    with pytest.raises(HTTPException) as e:
        ar.reserve_my_device(d_a, ar.ReservationRequest(run_id="run-b"),
                             db=db, agent=_auth(db, a_cred))
    assert e.value.status_code == 403


def test_14_release_is_owner_only(env):
    db, _ = env
    a, cred = _agent(db, "mac-a"); d = _device(db, a, UDID_X)
    _run(db, "run-1", a); _run(db, "run-2", a)
    ar.reserve_my_device(d, ar.ReservationRequest(run_id="run-1"), db=db, agent=_auth(db, cred))

    with pytest.raises(HTTPException) as e:
        ar.release_my_reservation(d, ar.ReservationRequest(run_id="run-2"),
                                  db=db, agent=_auth(db, cred))
    assert e.value.status_code == 409

    assert ar.release_my_reservation(d, ar.ReservationRequest(run_id="run-1"),
                                     db=db, agent=_auth(db, cred))["released"] is True
    # Releasing an already-free device stays harmless.
    assert ar.release_my_reservation(d, ar.ReservationRequest(run_id="run-1"),
                                     db=db, agent=_auth(db, cred))["released"] is False


def test_15_activation_is_owner_only(env):
    db, _ = env
    a, cred = _agent(db, "mac-a"); d = _device(db, a, UDID_X)
    _run(db, "run-1", a); _run(db, "run-2", a)
    ar.reserve_my_device(d, ar.ReservationRequest(run_id="run-1"), db=db, agent=_auth(db, cred))
    assert ar.activate_my_reservation(d, ar.ReservationRequest(run_id="run-1"),
                                      db=db, agent=_auth(db, cred))["state"] == "active"
    with pytest.raises(HTTPException) as e:
        ar.activate_my_reservation(d, ar.ReservationRequest(run_id="run-2"),
                                   db=db, agent=_auth(db, cred))
    assert e.value.status_code == 409


# ── WDA ports (16–21) ────────────────────────────────────────────────────────

def test_16_17_18_ports_are_machine_scoped_and_stable(env):
    db, _ = env
    a, cred = _agent(db, "mac-a")
    dx = _device(db, a, UDID_X, "iPhone 16 Pro")
    dy = _device(db, a, UDID_Y, "iPad Pro 11-inch")

    px = ar.allocate_my_wda_port(dx, None, db=db, agent=_auth(db, cred))["port"]
    py = ar.allocate_my_wda_port(dy, None, db=db, agent=_auth(db, cred))["port"]
    assert px != py, "two simulators on one Mac must not share a port"
    assert ar.allocate_my_wda_port(dx, None, db=db, agent=_auth(db, cred))["port"] == px


def test_19_the_same_number_may_exist_on_two_machines(env):
    db, _ = env
    a, a_cred = _agent(db, "mac-a"); b, b_cred = _agent(db, "mac-b")
    d_a = _device(db, a, UDID_X); d_b = _device(db, b, UDID_X)
    assert (ar.allocate_my_wda_port(d_a, None, db=db, agent=_auth(db, a_cred))["port"]
            == ar.allocate_my_wda_port(d_b, None, db=db, agent=_auth(db, b_cred))["port"])


def test_20_an_explicit_preferred_port_wins(env):
    db, _ = env
    a, cred = _agent(db, "mac-a"); d = _device(db, a, UDID_X)
    out = ar.allocate_my_wda_port(d, ar.WdaPortRequest(preferred=8100),
                                  db=db, agent=_auth(db, cred))
    assert out["port"] == 8100 and out["preferred"] is True


def test_21_concurrent_allocation_gives_one_port_per_device(env):
    db, Session = env
    a, cred = _agent(db, "mac-a")
    d = _device(db, a, UDID_X)
    from automation.device_manager.reservation import ensure_wda_port
    ports, start = [], threading.Barrier(6)

    def go():
        own = Session()
        try:
            start.wait(timeout=10)
            ports.append(ensure_wda_port(d, db=own))
        finally:
            own.close()

    ts = [threading.Thread(target=go) for _ in range(6)]
    for t in ts: t.start()
    for t in ts: t.join(timeout=20)
    assert len(set(ports)) == 1, f"one device, one port — got {set(ports)}"


# ── Regression: legacy / pinned / cross-machine (31–33) ──────────────────────

def test_31_a_legacy_run_with_no_agent_id_may_reserve(env):
    """machine_id/agent_id NULL: the executing agent supplies the machine."""
    db, _ = env
    a, cred = _agent(db, "mac-a"); d = _device(db, a, UDID_X)
    _run(db, "legacy-run", agent_id=None)
    out = ar.reserve_my_device(d, ar.ReservationRequest(run_id="legacy-run"),
                               db=db, agent=_auth(db, cred))
    assert out["reserved_by"] == "legacy-run"
    db.expire_all()
    assert db.query(TestRun).get("legacy-run").machine_id is None, "not backfilled"


def test_33_two_machines_with_one_udid_reserve_independently(env):
    db, _ = env
    a, a_cred = _agent(db, "mac-a"); b, b_cred = _agent(db, "mac-b")
    d_a = _device(db, a, UDID_X); d_b = _device(db, b, UDID_X)
    _run(db, "run-a", a); _run(db, "run-b", b)
    ar.reserve_my_device(d_a, ar.ReservationRequest(run_id="run-a"), db=db, agent=_auth(db, a_cred))
    ar.reserve_my_device(d_b, ar.ReservationRequest(run_id="run-b"), db=db, agent=_auth(db, b_cred))


def test_34_35_backend_local_wda_behaviour_is_unchanged(env):
    """port_for() keeps its explicit-preferred path for cross-app and the backend."""
    from automation.appium_service import wda
    wda._ports.clear()
    try:
        assert wda.port_for(UDID_X, 8100) == 8100
        assert wda.port_for(UDID_Y, 8101) == 8101
    finally:
        wda._ports.clear()


# ── The boundary guard (22) — import graph, not grep ─────────────────────────

def _agent_lifecycle_calls():
    """Names called inside run_job and its device helpers, via AST."""
    from automation.agent import main as agent_main
    src = inspect.getsource(agent_main)
    tree = ast.parse(src)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            names.add(getattr(f, "attr", None) or getattr(f, "id", None))
    return {n for n in names if n}


def test_22_the_agent_no_longer_calls_the_device_database_functions():
    """Boundary: these are backend operations now."""
    called = _agent_lifecycle_calls()
    for forbidden in ("device_row_id", "reserve_device", "release_device",
                      "mark_active", "ensure_wda_port", "get_reservation"):
        assert forbidden not in called, f"agent still calls {forbidden}() directly"


def test_22b_the_agent_module_does_not_import_the_device_db_layer():
    from automation.agent import main as agent_main
    tree = ast.parse(inspect.getsource(agent_main))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imported.add(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
    banned = {"automation.device_manager.reservation",
              "automation.device_manager.service.device_row_id"}
    assert not (banned & imported), f"agent imports the device DB layer: {banned & imported}"


def test_the_backend_still_owns_the_database_functions():
    """The refactor moved the CALLER, not the capability."""
    from automation.device_manager import reservation
    for fn in ("reserve_device", "release_device", "mark_active", "ensure_wda_port"):
        assert callable(getattr(reservation, fn))
    assert "reserve_device" in inspect.getsource(ar), "the backend router uses them"


def test_the_remaining_db_accesses_are_still_out_of_scope():
    """Phase 4F.3 moved ScenarioResult behind the API, 4F.6 performance and 4F.7A
    the orphan reaper. Preparation remains direct and is a later phase. (Before
    4F.3 this also asserted ScenarioResult was still direct.)"""
    from automation.agent import main as agent_main
    from automation.projects import preparation
    from automation.performance import collector
    from automation.utils import proctree

    # Code, not prose: comments legitimately mention ScenarioResult to explain
    # that the agent no longer persists it.
    agent_tree = ast.parse(inspect.getsource(agent_main))
    agent_imports = set()
    for node in ast.walk(agent_tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for al in node.names:
                agent_imports.add(f"{node.module}.{al.name}")
    assert "automation.database.models.ScenarioResult" not in agent_imports, "4F.3 moved it"
    assert not [n for n in ast.walk(agent_tree)
                if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "ScenarioResult"], \
        "the agent must not construct ScenarioResult rows"
    assert "run_scenario_headless" in inspect.getsource(agent_main), \
        "execution stays on the agent — packaging is Phase 4F.4"
    assert "SessionLocal" in inspect.getsource(preparation), \
        "preparation is still direct — that is Phase 4F.5's other half"
    # 4F.6 moved the collector behind POST /runs/{run_id}/performance;
    # 4F.7A moved the reaper behind GET /agents/me/active-jobs.
    for mod in (collector, proctree):
        assert "SessionLocal" not in inspect.getsource(mod), \
            f"{mod.__name__} must not reach the database from the agent"
