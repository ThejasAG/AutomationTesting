"""Phase 4E.2 — reservation wired into the queued agent path, and machine-global
WDA ports.

The agent reserves the simulator BEFORE Appium can start and releases it only
after the process tree is gone, so a device is never free while something can
still drive it. WDA ports move from a per-process dict to the registry row, which
is what lets two simulators on one Mac actually run at the same time.
"""
import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from automation.database.models import Base, DeviceRecord
from automation.device_manager import reservation as res
from automation.device_manager import service as dm

MACHINE_A = "machine-a"
MACHINE_B = "machine-b"
UDID_X = "AAAAAAAA-1111-2222-3333-444444444444"
UDID_Y = "BBBBBBBB-5555-6666-7777-888888888888"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SESSION = []   # the active test session, for helpers that need it


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "exec.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    import automation.database.config as cfg
    monkeypatch.setattr(cfg, "SessionLocal", Session)
    monkeypatch.setattr(dm, "_backend_machine_id", None)
    s = Session()
    from automation.database.models import TestProject
    s.add(TestProject(id="proj-1", name="Demo", git_url="https://example.com/x.git"))
    s.commit()
    _SESSION.clear(); _SESSION.append(s)
    yield s, Session, str(path)
    s.close()


def _add(session, machine_id, udid, name="iPhone"):
    row = DeviceRecord(machine_id=machine_id, udid=udid, name=name, platform="iOS")
    session.add(row)
    session.commit()
    return row.id


def _held_by(session, device_id):
    session.expire_all()
    return session.query(DeviceRecord).filter(DeviceRecord.id == device_id).one().reserved_by


# ── Execution-time device resolution (2, 3, 8, 9, 10, 12) ────────────────────

def test_resolves_this_machines_device_only(db):
    session, _, _ = db
    d1 = _add(session, MACHINE_A, UDID_X)
    d2 = _add(session, MACHINE_B, UDID_X)          # same UDID, other machine

    assert dm.device_row_id(MACHINE_A, UDID_X) == d1
    assert dm.device_row_id(MACHINE_B, UDID_X) == d2
    assert d1 != d2


def test_unknown_device_resolves_to_none_not_another_machine(db):
    session, _, _ = db
    _add(session, MACHINE_B, UDID_X)
    assert dm.device_row_id(MACHINE_A, UDID_X) is None, "must not borrow B's device"
    assert dm.device_row_id(MACHINE_A, "NOT-REGISTERED") is None
    assert dm.device_row_id(None, UDID_X) is None
    assert dm.device_row_id(MACHINE_A, None) is None


def test_a_legacy_job_reserves_the_executing_machines_row(db):
    """machine_id=NULL: the AGENT supplies the machine, nothing is backfilled."""
    session, _, _ = db
    d_a = _add(session, MACHINE_A, UDID_X)
    d_b = _add(session, MACHINE_B, UDID_X)

    # Agent A executes a legacy job whose device_name is UDID_X.
    row = dm.device_row_id(MACHINE_A, UDID_X)
    assert row == d_a
    assert res.reserve_device(row, "legacy-run") is True
    assert _held_by(session, d_b) is None, "the other machine's device is untouched"


def test_a_pinned_job_reserves_its_own_machines_row(db):
    session, _, _ = db
    d_a = _add(session, MACHINE_A, UDID_X)
    _add(session, MACHINE_B, UDID_X)
    assert res.reserve_device(dm.device_row_id(MACHINE_A, UDID_X), "run-A") is True
    assert _held_by(session, d_a) == "run-A"


def test_same_udid_two_machines_reserve_concurrently(db):
    session, _, _ = db
    d1 = _add(session, MACHINE_A, UDID_X)
    d2 = _add(session, MACHINE_B, UDID_X)
    assert res.reserve_device(d1, "run-1") is True
    assert res.reserve_device(d2, "run-2") is True


def test_two_devices_one_machine_reserve_concurrently(db):
    """Consumer + Business — no machine-wide lock."""
    session, _, _ = db
    consumer = _add(session, MACHINE_A, UDID_X, "iPhone 16 Pro")
    business = _add(session, MACHINE_A, UDID_Y, "iPad Pro 11-inch")
    assert res.reserve_device(consumer, "run-consumer") is True
    assert res.reserve_device(business, "run-business") is True


# ── The agent's run_job integration (1, 2, 3, 4, 5, 6, 7) ────────────────────

class _Framework:
    """Stands in for AppiumFramework.

    Records whether execution was reached AND who held the device at that moment —
    without which a "released afterwards" assertion passes trivially when the
    device was never reserved in the first place.
    """
    def __init__(self, outcome="passed", raises=None, watch=None):
        self.outcome, self.raises, self.watch = outcome, raises, watch
        self.executed = False
        self.holder_during_execution = None
        self.state_during_execution = None

    def prepare(self, *a, **k):
        return True

    def execute_with_retry(self, *a, **k):
        self.executed = True
        if self.watch:
            held = res.get_reservation(self.watch)
            self.holder_during_execution = held.reserved_by if held else None
            self.state_during_execution = held.reserved_state if held else None
        if self.raises:
            raise self.raises
        return {"status": self.outcome, "logs": [], "attempts": 1,
                "evidence_dir": "/tmp/x", "detail": ""}

    def collect_evidence(self, *a, **k):
        return {}

    def cleanup(self, *a, **k):
        pass


@pytest.fixture
def agent(db, monkeypatch):
    """run_job with its I/O stubbed, but its real reservation logic intact."""
    from automation.agent import main as agent_main

    session, Session, _ = db
    monkeypatch.setattr(agent_main, "AGENT_ID", MACHINE_A)
    monkeypatch.setattr(agent_main, "_PROCESSED_JOB_IDS", set())
    monkeypatch.setattr(agent_main, "report_status", lambda *a, **k: None)
    monkeypatch.setattr(agent_main, "_upload_evidence_with_retry", lambda *a, **k: None)
    monkeypatch.setattr(agent_main, "_start_screen_capture", lambda *a, **k: (None, None))
    monkeypatch.setattr(agent_main, "_stop_screen_capture", lambda *a, **k: None)
    monkeypatch.setattr(agent_main.proctree, "reap_job", lambda *a, **k: 0)

    class _Cfg:                       # minimal automation.yaml stand-in
        class execution:
            command = "pytest tests/"
    monkeypatch.setattr(agent_main.repository_manager, "validate_yaml", lambda *a, **k: _Cfg())
    monkeypatch.setattr(agent_main, "_has_test_suite", lambda *a, **k: True)
    monkeypatch.setattr(agent_main, "_run_planned_scenarios", lambda *a, **k: {"status": "passed"})
    monkeypatch.setattr(agent_main.HTTP, "post", lambda *a, **k: type("R", (), {"status_code": 200})())

    # Phase 4F.2A: the agent reaches devices over HTTP, not the database. Route its
    # calls straight into the REAL router functions against this test DB, so the
    # test still exercises agent -> backend -> reservation end to end — only the
    # transport is short-circuited, never the logic being asserted.
    from automation.api.v1.routers import agents as ar
    from automation.database.models import DeviceRecord

    class _Agent:                     # what require_authenticated_agent would return
        id = MACHINE_A

    def _fake_request(method, url, **kwargs):
        body = kwargs.get("json") or {}
        tail = url.split("/agents/me", 1)[1]
        db = Session()
        try:
            if method == "GET":
                udid = tail.rsplit("/", 1)[1]
                out = ar.get_my_device(udid, db=db, agent=_Agent())
            elif tail.endswith("/reserve"):
                did = tail.split("/devices/")[1].split("/")[0]
                out = ar.reserve_my_device(did, ar.ReservationRequest(**body),
                                           db=db, agent=_Agent())
            elif tail.endswith("/activate"):
                did = tail.split("/devices/")[1].split("/")[0]
                out = ar.activate_my_reservation(did, ar.ReservationRequest(**body),
                                                 db=db, agent=_Agent())
            elif tail.endswith("/reservation"):
                did = tail.split("/devices/")[1].split("/")[0]
                out = ar.release_my_reservation(did, ar.ReservationRequest(**body),
                                                db=db, agent=_Agent())
            else:
                raise AssertionError(f"unrouted device call: {method} {url}")
            return type("R", (), {"status_code": 200, "json": lambda self=None, o=out: o,
                                  "raise_for_status": lambda self=None: None})()
        except HTTPException as e:
            return type("R", (), {"status_code": e.status_code,
                                  "json": lambda self=None, d=e.detail: {"detail": d},
                                  "text": str(e.detail),
                                  "raise_for_status": lambda self=None: None})()
        finally:
            db.close()

    monkeypatch.setattr(agent_main.HTTP, "request", _fake_request)
    return agent_main


def _seed_run(db, job_id):
    """The API verifies the run exists and is this agent's — create it."""
    from automation.database.models import TestRun
    if db.query(TestRun).filter(TestRun.id == job_id).first() is None:
        db.add(TestRun(id=job_id, project_id="proj-1", test_suite="s", test_name=job_id,
                       status="queued", job_state="queued", device_name=UDID_X,
                       agent_id=MACHINE_A))
        db.commit()


def _run(agent_main, framework, monkeypatch, job_id="run-1", device=UDID_X):
    """Drive run_job far enough to exercise reserve → execute → release."""
    monkeypatch.setattr(agent_main, "AppiumFramework", lambda: framework)
    # Short-circuit preparation so the test focuses on the reservation lifecycle.
    monkeypatch.setattr(agent_main.preparation_service, "prepare_for_execution",
                        lambda *a, **k: type("P", (), {
                            "ok": True, "steps": [], "error": None, "branch": "main",
                            "project_type": "ios", "validation": None})())
    _seed_run(_SESSION[0], job_id)
    job = {"job_id": job_id, "project_id": "p1", "git_url": "u", "branch": "main",
           "device_id": device, "platform": "ios", "project_name": "demo",
           "planned_scenarios": [], "is_pr": False}
    agent_main.run_job(job, [device])


def test_01_reservation_is_acquired_before_execution(db, agent, monkeypatch):
    """Ordering: the device must already be held by this run when execution starts."""
    session, _, _ = db
    d = _add(session, MACHINE_A, UDID_X)
    fw = _Framework(watch=d)
    _run(agent, fw, monkeypatch)
    assert fw.executed, "execution should have been reached"
    assert fw.holder_during_execution == "run-1", \
        "the device must be reserved BEFORE Appium/pytest can run"
    assert fw.state_during_execution == res.STATE_ACTIVE, "marked active at execution"


def test_02_a_successful_run_releases_the_device(db, agent, monkeypatch):
    session, _, _ = db
    d = _add(session, MACHINE_A, UDID_X)
    fw = _Framework("passed", watch=d)
    _run(agent, fw, monkeypatch)
    assert fw.holder_during_execution == "run-1", "held during the run…"
    row = session.query(DeviceRecord).filter(DeviceRecord.id == d).one()
    session.refresh(row)
    assert (row.reserved_by, row.reserved_at, row.reserved_state) == (None, None, None)


def test_03_a_failed_test_releases_the_device(db, agent, monkeypatch):
    session, _, _ = db
    d = _add(session, MACHINE_A, UDID_X)
    fw = _Framework("failed", watch=d)
    _run(agent, fw, monkeypatch)
    assert fw.holder_during_execution == "run-1", "held during the run…"
    assert _held_by(session, d) is None, "…and released after it"


@pytest.mark.parametrize("boom", [
    RuntimeError("Appium failed to start on port 4723"),
    RuntimeError("WebDriverAgent did not come up"),
    ValueError("something entirely unexpected"),
])
def test_04_05_06_every_failure_path_releases_the_device(db, agent, monkeypatch, boom):
    session, _, _ = db
    d = _add(session, MACHINE_A, UDID_X)
    fw = _Framework(raises=boom, watch=d)
    _run(agent, fw, monkeypatch)
    assert fw.holder_during_execution == "run-1", "held when the failure occurred…"
    assert _held_by(session, d) is None, "…and still released"


def test_07_a_busy_device_prevents_execution_entirely(db, agent, monkeypatch):
    session, _, _ = db
    d = _add(session, MACHINE_A, UDID_X)
    assert res.reserve_device(d, "other-run") is True

    fw = _Framework()
    _run(agent, fw, monkeypatch, job_id="run-2")

    assert fw.executed is False, "Appium/WDA/pytest must not start"
    assert _held_by(session, d) == "other-run", "the owner keeps the device"


def test_07b_contention_requeues_rather_than_failing_the_test(db, agent, monkeypatch):
    """Device-busy is contention, not an application test failure."""
    session, _, _ = db
    d = _add(session, MACHINE_A, UDID_X)
    res.reserve_device(d, "other-run")

    reported = []
    monkeypatch.setattr(agent, "report_status",
                        lambda jid, status, *a, **k: reported.append(status))
    _run(agent, _Framework(), monkeypatch, job_id="run-2")

    assert "queued" in reported and "failed" not in reported
    assert "run-2" not in agent._PROCESSED_JOB_IDS, "retryable by this agent"


def test_unregistered_device_runs_unreserved(db, agent, monkeypatch):
    """Reservation must never block work that succeeds today."""
    session, _, _ = db
    fw = _Framework()
    _run(agent, fw, monkeypatch, device="DEVICE-WITH-NO-ROW")
    assert fw.executed, "an unknown device still runs, just without a reservation"


# ── 18. Crash boundary ───────────────────────────────────────────────────────

def test_18_a_reservation_survives_an_abnormal_process_exit(db, tmp_path):
    """Stale recovery is deliberately a later phase — nothing may release early."""
    session, _, path = db
    d = _add(session, MACHINE_A, UDID_X)

    code = textwrap.dedent(f"""
        import os
        import automation.database.config as cfg
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        cfg.engine = create_engine("sqlite:///{path}", connect_args={{"check_same_thread": False}})
        cfg.SessionLocal = sessionmaker(bind=cfg.engine)
        from automation.device_manager import reservation as res
        print(res.reserve_device({d!r}, "crashed-run"), flush=True)
        os._exit(9)                      # die without any cleanup
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=REPO_ROOT, env={**os.environ, "PYTHONPATH": REPO_ROOT})
    assert out.returncode == 9 and "True" in out.stdout

    assert _held_by(session, d) == "crashed-run", "reservation must remain held"


# ── 14–17. WDA ports, across real OS processes ───────────────────────────────

def _alloc_in_subprocess(db_path, device_id):
    """Allocate a WDA port from a separate OS process."""
    code = textwrap.dedent(f"""
        import automation.database.config as cfg
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        cfg.engine = create_engine("sqlite:///{db_path}", connect_args={{"check_same_thread": False}})
        cfg.SessionLocal = sessionmaker(bind=cfg.engine)
        from automation.device_manager.reservation import ensure_wda_port
        print("PORT:", ensure_wda_port({device_id!r}))
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=REPO_ROOT, env={**os.environ, "PYTHONPATH": REPO_ROOT})
    assert out.returncode == 0, out.stderr
    return int(next(l for l in out.stdout.splitlines() if l.startswith("PORT:")).split()[1])


def test_14_two_processes_two_devices_get_different_wda_ports(db):
    """The defect this phase fixes: both used to be 8100."""
    session, _, path = db
    dx = _add(session, MACHINE_A, UDID_X, "iPhone 16 Pro")
    dy = _add(session, MACHINE_A, UDID_Y, "iPad Pro 11-inch")

    p1 = _alloc_in_subprocess(path, dx)
    p2 = _alloc_in_subprocess(path, dy)

    assert p1 != p2, f"two simulators on one machine collided on port {p1}"
    assert {p1, p2} == {8100, 8101}


def _port_for_in_subprocess(db_path, machine_id, udid):
    """Call wda.port_for() — the real consumer — from a separate OS process."""
    code = textwrap.dedent(f"""
        import automation.database.config as cfg
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        cfg.engine = create_engine("sqlite:///{db_path}", connect_args={{"check_same_thread": False}})
        cfg.SessionLocal = sessionmaker(bind=cfg.engine)
        import automation.device_manager.service as dm
        dm._backend_machine_id = {machine_id!r}
        from automation.appium_service import wda
        print("PORT:", wda.port_for({udid!r}), flush=True)
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=REPO_ROOT, env={**os.environ, "PYTHONPATH": REPO_ROOT})
    assert out.returncode == 0, out.stderr
    return int(next(l for l in out.stdout.splitlines() if l.startswith("PORT:")).split()[1])


def test_14b_port_for_gives_two_processes_different_ports(db):
    """Through the REAL consumer. Before 4E.2 both processes returned 8100."""
    session, _, path = db
    _add(session, MACHINE_A, UDID_X, "iPhone 16 Pro")
    _add(session, MACHINE_A, UDID_Y, "iPad Pro 11-inch")

    p1 = _port_for_in_subprocess(path, MACHINE_A, UDID_X)
    p2 = _port_for_in_subprocess(path, MACHINE_A, UDID_Y)
    assert p1 != p2, f"two simulators on one Mac both got port {p1}"


def test_15_different_machines_may_reuse_the_same_port_number(db):
    session, _, path = db
    d_a = _add(session, MACHINE_A, UDID_X)
    d_b = _add(session, MACHINE_B, UDID_X)
    assert _alloc_in_subprocess(path, d_a) == _alloc_in_subprocess(path, d_b) == 8100


def test_16_the_port_is_stable_per_device_and_does_not_leak(db):
    """Design choice: a device keeps its port, so there is no pool to leak."""
    session, _, path = db
    d = _add(session, MACHINE_A, UDID_X)
    first = _alloc_in_subprocess(path, d)
    res.reserve_device(d, "run-1")
    res.release_device(d, "run-1")
    assert _alloc_in_subprocess(path, d) == first, "same port after a full cycle"
    assert res.ensure_wda_port(d) == first


def test_16b_allocation_is_idempotent_and_concurrent_safe(db):
    session, _, path = db
    d = _add(session, MACHINE_A, UDID_X)
    ports = {res.ensure_wda_port(d) for _ in range(5)}
    assert len(ports) == 1


def test_17_an_explicit_port_still_wins_for_cross_app(db):
    """cross_app_orchestrator pins 8100/8101 itself — unchanged."""
    from automation.appium_service import wda
    wda._ports.clear()
    try:
        assert wda.port_for(UDID_X, 8100) == 8100
        assert wda.port_for(UDID_Y, 8101) == 8101
        assert wda.port_for(UDID_X) == 8100, "the pinned value is remembered"
    finally:
        wda._ports.clear()


def test_wda_falls_back_when_the_device_is_not_registered(db):
    from automation.appium_service import wda
    wda._ports.clear()
    try:
        assert isinstance(wda.port_for("UNREGISTERED-UDID"), int), "never refuses to run"
    finally:
        wda._ports.clear()


# ── Boundaries ───────────────────────────────────────────────────────────────

def test_stale_recovery_is_not_implemented_yet():
    """Code only — the docstrings legitimately discuss staleness to say it is
    NOT handled here."""
    import ast, inspect, textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(res)))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            body = node.body[1:] if (node.body and isinstance(node.body[0], ast.Expr)
                                     and isinstance(node.body[0].value, ast.Constant)) else node.body
            code = ast.unparse(ast.Module(body=body, type_ignores=[])).lower()
            for forbidden in ("stale", "sweep", "heartbeat", "last_heartbeat"):
                assert forbidden not in code, \
                    f"{node.name} does stale recovery — that is Phase 4E.3"


def test_only_the_agent_path_reserves():
    import inspect
    from automation.api.v1.routers import scenario, inspector, recorder
    from automation.scenarios import service as scenario_service
    for mod in (scenario, scenario_service, inspector, recorder):
        assert "reserve_device" not in inspect.getsource(mod), \
            f"{mod.__name__} reservation is a later phase"
