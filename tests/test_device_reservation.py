"""Phase 4E.1 — the per-device reservation primitive.

The resource is one `devices` row, whose id encodes (machine_id, udid). Locking it
locks exactly one simulator on one machine — never a machine, so two simulators on
the same Mac stay independently usable.

The race test uses genuinely separate SQLAlchemy sessions with separate
connections, not two sequential calls on one session.
"""
import threading
from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect as sa_inspect, text
from sqlalchemy.orm import sessionmaker

from automation.database.models import Base, DeviceRecord
from automation.device_manager import reservation as res

MACHINE_A = "machine-a"
MACHINE_B = "machine-b"
UDID_X = "AAAAAAAA-1111-2222-3333-444444444444"
UDID_Y = "BBBBBBBB-5555-6666-7777-888888888888"


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A file-backed SQLite DB so separate sessions get separate connections."""
    engine = create_engine(f"sqlite:///{tmp_path/'res.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    import automation.database.config as cfg
    monkeypatch.setattr(cfg, "SessionLocal", Session)
    session = Session()
    yield session, Session, engine
    session.close()


def _add(session, machine_id, udid, *, provider=None, hostname=None, name="iPhone"):
    row = DeviceRecord(machine_id=machine_id, udid=udid, name=name, platform="iOS",
                       provider=provider, hostname=hostname)
    session.add(row)
    session.commit()
    return row.id


def _holder(session, device_id):
    session.expire_all()
    return session.query(DeviceRecord).filter(DeviceRecord.id == device_id).one().reserved_by


# ── 1–3. Acquire ─────────────────────────────────────────────────────────────

def test_01_acquire_a_free_device(db):
    session, _, _ = db
    d = _add(session, MACHINE_A, UDID_X)
    assert res.get_reservation(d).reserved_by is None

    assert res.reserve_device(d, "run-1") is True

    r = res.get_reservation(d)
    assert r.reserved_by == "run-1"
    assert r.reserved_state == res.STATE_RESERVED
    assert isinstance(r.reserved_at, datetime)
    assert r.is_held


def test_02_a_second_run_is_blocked(db):
    session, _, _ = db
    d = _add(session, MACHINE_A, UDID_X)
    assert res.reserve_device(d, "run-1") is True
    assert res.reserve_device(d, "run-2") is False
    assert _holder(session, d) == "run-1", "the original owner is untouched"


def test_03_the_same_run_may_re_enter(db):
    """Execution retries Appium/WDA startup; a run must not deadlock on itself."""
    session, _, _ = db
    d = _add(session, MACHINE_A, UDID_X)
    for _ in range(3):
        assert res.reserve_device(d, "run-1") is True
    assert _holder(session, d) == "run-1"


def test_03b_re_entry_refreshes_the_reservation(db):
    session, _, _ = db
    d = _add(session, MACHINE_A, UDID_X)
    res.reserve_device(d, "run-1")
    first = res.get_reservation(d).reserved_at
    res.reserve_device(d, "run-1", state=res.STATE_ACTIVE)
    after = res.get_reservation(d)
    assert after.reserved_at >= first
    assert after.reserved_state == res.STATE_ACTIVE


# ── 4–6. Release ─────────────────────────────────────────────────────────────

def test_04_the_owner_can_release(db):
    session, _, _ = db
    d = _add(session, MACHINE_A, UDID_X)
    res.reserve_device(d, "run-1")
    assert res.release_device(d, "run-1") is True

    r = res.get_reservation(d)
    assert (r.reserved_by, r.reserved_at, r.reserved_state) == (None, None, None)
    assert res.reserve_device(d, "run-2") is True, "now available to anyone"


def test_05_a_non_owner_cannot_release(db):
    """Otherwise a run could free another run's device and recreate the collision."""
    session, _, _ = db
    d = _add(session, MACHINE_A, UDID_X)
    res.reserve_device(d, "run-1")
    assert res.release_device(d, "run-2") is False
    assert _holder(session, d) == "run-1"


def test_06_releasing_a_free_device_is_a_safe_no_op(db):
    session, _, _ = db
    d = _add(session, MACHINE_A, UDID_X)
    assert res.release_device(d, "run-1") is False
    assert res.release_device(d, "run-1") is False
    assert _holder(session, d) is None


def test_06b_unknown_ids_are_handled_safely(db):
    assert res.reserve_device("no-such-device", "run-1") is False
    assert res.release_device("no-such-device", "run-1") is False
    assert res.get_reservation("no-such-device") is None
    assert res.reserve_device("", "run-1") is False
    assert res.reserve_device("d", "") is False


# ── 7–8. The lock is per device, never per machine ───────────────────────────

def test_07_same_udid_on_two_machines_reserves_independently(db):
    session, _, _ = db
    d1 = _add(session, MACHINE_A, UDID_X)
    d2 = _add(session, MACHINE_B, UDID_X)
    assert d1 != d2

    assert res.reserve_device(d1, "run-A") is True
    assert res.reserve_device(d2, "run-B") is True, "different physical simulators"
    assert _holder(session, d1) == "run-A"
    assert _holder(session, d2) == "run-B"

    res.release_device(d1, "run-A")
    assert _holder(session, d2) == "run-B", "releasing one never frees the other"


def test_08_two_devices_on_one_machine_reserve_independently(db):
    """The Consumer + Business requirement: no machine-wide lock."""
    session, _, _ = db
    consumer = _add(session, MACHINE_A, UDID_X, name="iPhone 16 Pro")
    business = _add(session, MACHINE_A, UDID_Y, name="iPad Pro 11-inch")

    assert res.reserve_device(consumer, "run-consumer") is True
    assert res.reserve_device(business, "run-business") is True
    assert _holder(session, consumer) == "run-consumer"
    assert _holder(session, business) == "run-business"


# ── 9–10. Concurrency ────────────────────────────────────────────────────────

def test_09_a_real_cross_session_race_has_exactly_one_winner(db):
    """Two threads, two independent sessions on their own connections, released
    together by a barrier. Not two sequential calls."""
    session, Session, _ = db
    d = _add(session, MACHINE_A, UDID_X)

    winners, errors = [], []
    start = threading.Barrier(8)

    def contend(run_id):
        own = Session()
        try:
            start.wait(timeout=10)
            if res.reserve_device(d, run_id, db=own):
                winners.append(run_id)
        except Exception as e:                      # noqa: BLE001
            errors.append(e)
        finally:
            own.close()

    threads = [threading.Thread(target=contend, args=(f"run-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"contention raised: {errors}"
    assert len(winners) == 1, f"exactly one acquisition must win, got {winners}"
    assert _holder(session, d) == winners[0]


def test_09b_the_race_repeats_cleanly(db):
    """Release and re-contend several times — no ratchet, no lost device."""
    session, Session, _ = db
    d = _add(session, MACHINE_A, UDID_X)
    for round_no in range(5):
        winners = []
        start = threading.Barrier(4)

        def contend(run_id):
            own = Session()
            try:
                start.wait(timeout=10)
                if res.reserve_device(d, run_id, db=own):
                    winners.append(run_id)
            finally:
                own.close()

        ts = [threading.Thread(target=contend, args=(f"r{round_no}-{i}",)) for i in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=20)
        assert len(winners) == 1, f"round {round_no}: {winners}"
        assert res.release_device(d, winners[0]) is True


def test_10_ownership_cannot_be_stolen_by_repeated_attempts(db):
    session, _, _ = db
    d = _add(session, MACHINE_A, UDID_X)
    res.reserve_device(d, "run-1")
    for _ in range(25):
        assert res.reserve_device(d, "run-2") is False
    assert _holder(session, d) == "run-1"


# ── 11–12. Migration and the existing constraint ─────────────────────────────

def test_11_the_columns_are_added_to_a_database_that_lacks_them(tmp_path, monkeypatch):
    """The project's own additive mechanism, on a DB with pre-existing rows."""
    import automation.database.config as cfg

    url = f"sqlite:///{tmp_path/'old.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    # A devices table as it looked before 4E.1, holding a row.
    with engine.begin() as c:
        c.execute(text("CREATE TABLE devices (id VARCHAR(36) PRIMARY KEY, "
                       "machine_id VARCHAR(120), udid VARCHAR(255), provider VARCHAR(120), "
                       "hostname VARCHAR(255), status VARCHAR(50))"))
        c.execute(text("INSERT INTO devices (id, machine_id, udid, provider, hostname, status) "
                       "VALUES ('d-old', 'machine-a', 'UDID-OLD', 'local', 'mac-a', 'ONLINE')"))

    before = {r[1] for r in engine.connect().execute(text("pragma table_info(devices)"))}
    assert "reserved_by" not in before

    monkeypatch.setattr(cfg, "engine", engine)
    monkeypatch.setattr(cfg, "SessionLocal", sessionmaker(bind=engine))
    cfg._add_missing_columns()

    cols = {c["name"] for c in sa_inspect(engine).get_columns("devices")}
    assert {"reserved_by", "reserved_at", "reserved_state"} <= cols

    row = engine.connect().execute(text(
        "SELECT machine_id, udid, provider, hostname, reserved_by, reserved_at, "
        "reserved_state FROM devices WHERE id='d-old'")).one()
    assert row[:4] == ("machine-a", "UDID-OLD", "local", "mac-a"), "existing values intact"
    assert row[4:] == (None, None, None), "existing rows come back unreserved"


def test_12_the_machine_udid_unique_constraint_still_holds(db):
    session, _, _ = db
    _add(session, MACHINE_A, UDID_X)
    _add(session, MACHINE_B, UDID_X)          # different machine: fine
    session.add(DeviceRecord(machine_id=MACHINE_A, udid=UDID_X, platform="iOS"))
    with pytest.raises(Exception):
        session.commit()
    session.rollback()


# ── 13–14. Nothing but the row id decides ownership ──────────────────────────

@pytest.mark.parametrize("provider", ["local", "some-agent-uuid", None, ""])
def test_13_provider_never_affects_reservation(db, provider):
    session, _, _ = db
    d = _add(session, MACHINE_A, UDID_X, provider=provider)
    assert res.reserve_device(d, "run-1") is True
    assert res.reserve_device(d, "run-2") is False

    # Changing provider mid-flight must not disturb ownership.
    session.query(DeviceRecord).filter(DeviceRecord.id == d).update({"provider": "flipped"})
    session.commit()
    assert _holder(session, d) == "run-1"
    assert res.release_device(d, "run-1") is True


def test_14_hostname_never_affects_reservation(db):
    session, _, _ = db
    d1 = _add(session, MACHINE_A, UDID_X, hostname="same-name")
    d2 = _add(session, MACHINE_B, UDID_Y, hostname="same-name")   # identical hostname
    assert res.reserve_device(d1, "run-1") is True
    assert res.reserve_device(d2, "run-2") is True, "hostname is not the key"

    session.query(DeviceRecord).filter(DeviceRecord.id == d1).update({"hostname": "renamed"})
    session.commit()
    assert _holder(session, d1) == "run-1"


def test_14b_the_primitive_never_reads_identity_fields():
    """Guard: reservation must key on the row id alone."""
    import ast, inspect, textwrap
    src = textwrap.dedent(inspect.getsource(res))
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in (
                "reserve_device", "release_device", "get_reservation", "mark_active"):
            body = node.body[1:] if (node.body and isinstance(node.body[0], ast.Expr)
                                     and isinstance(node.body[0].value, ast.Constant)) else node.body
            code = ast.unparse(ast.Module(body=body, type_ignores=[])).lower()
            for forbidden in ("provider", "hostname", "machine_id", "udid", "device_name"):
                assert forbidden not in code, f"{node.name} referenced {forbidden}"


# ── Boundaries this phase must not cross ─────────────────────────────────────

def test_integration_is_limited_to_the_agent_execution_path():
    """Phase 4E.2 wired reservation into the agent's run_job — and nowhere else.
    (Before 4E.2 this asserted no integration existed at all.)"""
    import inspect
    from automation.agent import main as agent_main
    from automation.api.v1.routers import jobs, scenario, inspector, recorder

    # Phase 4F.2A moved the CALLER behind HTTP: the agent asks the backend, and the
    # agents router performs the reservation. (Before 4F.2A the agent called it.)
    from automation.api.v1.routers import agents as agents_router
    assert "reserve_my_device" in inspect.getsource(agent_main), "the agent asks over HTTP"
    assert "reserve_device" not in inspect.getsource(agent_main), "…and not the DB directly"
    assert "reserve_device" in inspect.getsource(agents_router), "the backend performs it"
    for mod in (jobs, scenario, inspector, recorder):
        assert "reserve_device" not in inspect.getsource(mod), \
            f"{mod.__name__} reservation belongs to a later phase"


def test_wda_port_map_is_still_process_local():
    """Recorded as a dependency for a later phase; deliberately NOT fixed here."""
    from automation.appium_service import wda
    assert isinstance(wda._ports, dict), "still a module-level, per-process map"
