"""Phase 4C — persistent, machine-aware device registry.

The registry used to be a dict in the backend process: it died on restart and had
no way to say which machine a device belonged to. The `devices` table is now the
source of truth, keyed (machine_id, udid); the cache is the layer in front of it.

Allocation is deliberately NOT touched by this phase.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from automation.database.models import Base, DeviceRecord, ExecutionAgent
from automation.device_manager import service as dm
from automation.device_manager.models import DeviceStatus


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A DeviceDiscoveryService backed by a throwaway database."""
    engine = create_engine(f"sqlite:///{tmp_path/'devices.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    import automation.database.config as cfg
    monkeypatch.setattr(cfg, "SessionLocal", Session)

    svc = dm.DeviceDiscoveryService()
    return svc, Session


def _rows(Session):
    db = Session()
    try:
        return db.query(DeviceRecord).all()
    finally:
        db.close()


def _dev(udid, **over):
    d = {"id": udid, "name": "iPhone 16 Pro", "platform": "iOS",
         "platform_version": "18.3", "model": "iPhone 16 Pro"}
    d.update(over)
    return d


# ── Persistence ──────────────────────────────────────────────────────────────

def test_agent_sync_writes_a_database_row(registry):
    svc, Session = registry
    svc.sync_agent_devices("agent-a", [_dev("UDID-1")], "mac-a")

    rows = _rows(Session)
    assert len(rows) == 1
    r = rows[0]
    assert r.machine_id == "agent-a"
    assert r.udid == "UDID-1"
    assert r.hostname == "mac-a"
    assert r.provider == "agent-a"
    assert r.platform == "iOS"
    assert r.status == "ONLINE"
    assert r.last_seen is not None
    assert r.id and len(r.id) == 36, "surrogate uuid primary key"


def test_sync_still_populates_the_cache(registry):
    """The cache is a layer in front of the table, not a replacement."""
    svc, _ = registry
    svc.sync_agent_devices("agent-a", [_dev("UDID-1")], "mac-a")
    assert svc.get_device("UDID-1").id == "UDID-1"
    assert [d.id for d in svc.get_all_devices()] == ["UDID-1"]


# ── Upsert / duplicate protection ────────────────────────────────────────────

def test_repeated_heartbeats_update_one_row(registry):
    """Heartbeats repeat every few seconds — they must not accumulate rows."""
    svc, Session = registry
    for _ in range(3):
        svc.sync_agent_devices("agent-a", [_dev("UDID-1")], "mac-a")
    assert len(_rows(Session)) == 1


def test_heartbeat_updates_metadata_and_last_seen(registry):
    svc, Session = registry
    svc.sync_agent_devices("agent-a", [_dev("UDID-1", name="old name")], "mac-a")
    first = _rows(Session)[0].last_seen

    svc.sync_agent_devices("agent-a", [_dev("UDID-1", name="new name")], "mac-a")
    rows = _rows(Session)
    assert len(rows) == 1
    assert rows[0].name == "new name"
    assert rows[0].last_seen >= first


# ── Cross-machine identity ───────────────────────────────────────────────────

def test_same_udid_on_two_machines_is_two_rows(registry):
    """A UDID is unique within one Mac, not globally."""
    svc, Session = registry
    svc.sync_agent_devices("agent-a", [_dev("UDID-X")], "mac-a")
    svc.sync_agent_devices("agent-b", [_dev("UDID-X")], "mac-b")

    rows = _rows(Session)
    assert len(rows) == 2
    assert {r.machine_id for r in rows} == {"agent-a", "agent-b"}
    assert {r.hostname for r in rows} == {"mac-a", "mac-b"}
    assert {r.udid for r in rows} == {"UDID-X"}


def test_uniqueness_is_machine_plus_udid_not_udid(registry):
    """The constraint must reject a duplicate pair, not a duplicate udid."""
    svc, Session = registry
    db = Session()
    try:
        db.add(DeviceRecord(machine_id="m1", udid="U"))
        db.add(DeviceRecord(machine_id="m2", udid="U"))
        db.commit()                       # different machines: fine
        db.add(DeviceRecord(machine_id="m1", udid="U"))
        with pytest.raises(Exception):    # same pair: rejected
            db.commit()
    finally:
        db.rollback()
        db.close()


def test_devices_for_machine_separates_the_two_macs(registry):
    svc, _ = registry
    svc.sync_agent_devices("agent-a", [_dev("UDID-1"), _dev("UDID-2")], "mac-a")
    svc.sync_agent_devices("agent-b", [_dev("UDID-3")], "mac-b")

    assert {d.id for d in svc.devices_for_machine("agent-a")} == {"UDID-1", "UDID-2"}
    assert {d.id for d in svc.devices_for_machine("agent-b")} == {"UDID-3"}
    assert svc.devices_for_machine("agent-c") == []


# ── Restart / hydration ──────────────────────────────────────────────────────

def test_devices_survive_a_backend_restart(registry):
    """The reason this phase exists."""
    svc, Session = registry
    svc.sync_agent_devices("agent-a", [_dev("UDID-1"), _dev("UDID-2")], "mac-a")

    restarted = dm.DeviceDiscoveryService()          # cold process, empty cache
    assert restarted._devices_cache == {}
    assert restarted.hydrate_from_db() == 2
    assert {d.id for d in restarted.get_all_devices()} == {"UDID-1", "UDID-2"}
    assert restarted.get_device("UDID-1").hostname == "mac-a"


def test_hydration_does_not_resurrect_devices_as_online(registry):
    """A device whose agent vanished while the backend was down is NOT online."""
    svc, Session = registry
    svc.sync_agent_devices("agent-a", [_dev("UDID-1")], "mac-a")

    db = Session()                                    # age the row past staleness
    row = db.query(DeviceRecord).first()
    row.last_seen = datetime.utcnow() - timedelta(seconds=600)
    db.commit()
    db.close()

    restarted = dm.DeviceDiscoveryService()
    restarted.hydrate_from_db()
    assert restarted.get_device("UDID-1").status == DeviceStatus.ONLINE, "stored as-is"
    # ...but the freshness rule demotes it the moment anything reads the registry.
    assert restarted.get_all_devices()[0].status == DeviceStatus.DISCONNECTED
    assert restarted.get_online_devices() == []


def test_hydration_of_an_empty_database_is_harmless(registry):
    svc, _ = registry
    assert dm.DeviceDiscoveryService().hydrate_from_db() == 0


# ── Staleness (existing mechanism, unchanged) ────────────────────────────────

def test_existing_staleness_window_is_unchanged():
    assert dm.DEVICE_STALE_AFTER == timedelta(seconds=90)


def test_fresh_device_stays_online(registry):
    svc, _ = registry
    svc.sync_agent_devices("agent-a", [_dev("UDID-1")], "mac-a")
    assert svc.get_online_devices()[0].id == "UDID-1"


def test_device_goes_offline_when_its_agent_stops_heartbeating(registry):
    svc, _ = registry
    svc.sync_agent_devices("agent-a", [_dev("UDID-1")], "mac-a")
    svc._devices_cache["UDID-1"].last_seen = datetime.utcnow() - timedelta(seconds=120)
    assert svc.get_all_devices()[0].status == DeviceStatus.DISCONNECTED
    assert svc.get_online_devices() == []


# ── Backend-local devices ────────────────────────────────────────────────────

def test_local_machine_id_is_this_machines_agent_row(registry, monkeypatch):
    """Phase 4D.1 replaced the "local:<hostname>" scheme: the backend's simulators
    are now stored under this machine's execution_agents.id, so a co-located agent
    resolves to the same machine. (This test asserted the 4C scheme before.)"""
    monkeypatch.setattr(dm, "_backend_machine_id", None)
    a, b = dm.local_machine_id(), dm.local_machine_id()
    assert a == b, "stable across calls, and so across restarts"
    assert not a.startswith("local:"), "the parallel identity scheme is retired"
    assert len(a) == 36, "it is an execution_agents uuid"


def test_local_machine_id_falls_back_when_the_database_is_unreachable(monkeypatch):
    """Device discovery must never break over identity resolution."""
    monkeypatch.setattr(dm, "_backend_machine_id", None)
    import automation.database.config as cfg
    monkeypatch.setattr(cfg, "SessionLocal",
                        lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert dm.local_machine_id().startswith("local:")


def test_backend_local_device_persists_under_the_local_machine(registry):
    svc, Session = registry
    svc.register_local_device("UDID-LOCAL", "iPhone 16 Pro")
    row = _rows(Session)[0]
    assert row.machine_id == dm.local_machine_id()
    assert row.provider == "local", "existing provider semantics unchanged"
    assert row.hostname


def test_backend_local_and_agent_device_with_the_same_udid_are_distinct(registry):
    """The explicitly required case: backend-local iPhone X vs agent-Mac-B iPhone X."""
    svc, Session = registry
    svc.register_local_device("UDID-SHARED", "iPhone X")
    svc.sync_agent_devices("agent-mac-b", [_dev("UDID-SHARED", name="iPhone X")], "mac-b")

    rows = _rows(Session)
    assert len(rows) == 2, "one physical device per machine, not one merged row"
    by_machine = {r.machine_id: r for r in rows}
    assert dm.local_machine_id() in by_machine
    assert "agent-mac-b" in by_machine
    assert by_machine[dm.local_machine_id()].provider == "local"
    assert by_machine["agent-mac-b"].provider == "agent-mac-b"


# ── Machine identity stability ───────────────────────────────────────────────

def test_agent_registration_reuses_one_row_per_machine(registry):
    """execution_agents.id must survive a restart — the devices table keys on it."""
    from automation.api.v1.routers import agents as agents_router

    _, Session = registry
    db = Session()
    try:
        req = agents_router.RegisterAgentRequest(
            hostname="mac-a", os="Darwin", capabilities={}, connected_devices=[])
        first = agents_router.register_agent(req, db=db)
        second = agents_router.register_agent(req, db=db)
        assert first["id"] == second["id"], "same machine, same id across restarts"
        assert db.query(ExecutionAgent).count() == 1

        other = agents_router.RegisterAgentRequest(
            hostname="mac-b", os="Darwin", capabilities={}, connected_devices=[])
        assert agents_router.register_agent(other, db=db)["id"] != first["id"]
        assert db.query(ExecutionAgent).count() == 2
    finally:
        db.close()


# ── Failure isolation ────────────────────────────────────────────────────────

def test_a_database_failure_does_not_break_the_heartbeat(registry, monkeypatch):
    """Persistence is best-effort: devices must keep working if the DB is down."""
    svc, _ = registry
    import automation.database.config as cfg
    monkeypatch.setattr(cfg, "SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("db down")))

    svc.sync_agent_devices("agent-a", [_dev("UDID-1")], "mac-a")   # must not raise
    assert svc.get_device("UDID-1") is not None, "cache still updated"


# ── Allocation is NOT changed by this phase ──────────────────────────────────

def test_device_matching_survived_machine_aware_allocation():
    """Phase 4D.3 added a machine predicate; the DEVICE match it sits alongside
    must be untouched. (Before 4D.3 this asserted machine_id was absent.)"""
    import inspect
    from automation.api.v1.routers import jobs
    src = inspect.getsource(jobs.poll_job)
    assert "job.device_name in req.connected_devices" in src


# ── Phase 4D.1: one machine identity per physical Mac ────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Admins-MacBook-Pro.local", "admins-macbook-pro"),
    ("Admins-MacBook-Pro", "admins-macbook-pro"),
    ("admins-macbook-pro", "admins-macbook-pro"),
    ("THEJAS", "thejas"),                      # real row in the live database
    ("eu-central-node-01", "eu-central-node-01"),   # dotless: untouched
    ("mac.example.com", "mac"),                # not every host ends in .local
    ("  Host.Local. ", "host"),                # whitespace + trailing dot
    ("", ""),
    (None, ""),
])
def test_hostname_normalization(raw, expected):
    assert dm.normalize_hostname(raw) == expected


@pytest.mark.parametrize("raw", ["Admins-MacBook-Pro.local", "THEJAS", "", None,
                                 "mac.example.com", "  Host.Local. "])
def test_hostname_normalization_is_idempotent(raw):
    once = dm.normalize_hostname(raw)
    assert dm.normalize_hostname(once) == once


def test_the_two_real_spellings_of_this_mac_agree():
    """The exact mismatch Phase 4D.0 found: agent vs backend spelling."""
    assert dm.normalize_hostname("Admins-MacBook-Pro.local") == \
           dm.normalize_hostname("Admins-MacBook-Pro")


def test_os_normalization_does_not_alias_different_names():
    assert dm.normalize_os("  Darwin ") == dm.normalize_os("darwin") == "darwin"
    assert dm.normalize_os("Darwin") != dm.normalize_os("macOS")


def _register(db, hostname, os_name="Darwin"):
    from automation.api.v1.routers import agents as agents_router
    return agents_router.register_agent(
        agents_router.RegisterAgentRequest(
            hostname=hostname, os=os_name, capabilities={}, connected_devices=[]),
        db=db)["id"]


def test_registration_reuses_one_row_across_hostname_spellings(registry):
    """A restart that reports the other spelling must NOT create a second machine."""
    _, Session = registry
    db = Session()
    try:
        first = _register(db, "Admins-MacBook-Pro.local")
        second = _register(db, "Admins-MacBook-Pro")
        assert first == second
        assert db.query(ExecutionAgent).count() == 1
    finally:
        db.close()


def test_same_hostname_different_os_is_a_different_machine(registry):
    _, Session = registry
    db = Session()
    try:
        mac = _register(db, "build-01", "Darwin")
        win = _register(db, "build-01", "Windows")
        assert mac != win
        assert db.query(ExecutionAgent).count() == 2
    finally:
        db.close()


def test_backend_registers_its_own_machine(registry, monkeypatch):
    _, Session = registry
    monkeypatch.setattr(dm, "_backend_machine_id", None)

    machine_id = dm.ensure_backend_machine()
    assert machine_id

    db = Session()
    try:
        row = db.query(ExecutionAgent).filter(ExecutionAgent.id == machine_id).one()
        assert row.is_backend is True, "marked so nothing treats it as a worker"
        assert row.status == "offline", "the backend never polls"
        assert dm.normalize_hostname(row.hostname) == dm.normalize_hostname(dm._local_hostname())
    finally:
        db.close()


def test_backend_machine_is_created_once_not_per_call(registry, monkeypatch):
    _, Session = registry
    monkeypatch.setattr(dm, "_backend_machine_id", None)
    ids = {dm.ensure_backend_machine() for _ in range(3)}
    assert len(ids) == 1
    db = Session()
    try:
        assert db.query(ExecutionAgent).count() == 1
    finally:
        db.close()


def test_a_colocated_agent_adopts_the_backends_machine_row(registry, monkeypatch):
    """THE acceptance test for 4D.1: one Mac, one machine identity."""
    _, Session = registry
    monkeypatch.setattr(dm, "_backend_machine_id", None)

    backend_id = dm.ensure_backend_machine()
    db = Session()
    try:
        # The agent reports the OTHER spelling, exactly as it does in production.
        agent_id = _register(db, dm._local_hostname() + ".local", dm.local_os())
        assert agent_id == backend_id, "backend and agent must be one machine"
        assert db.query(ExecutionAgent).count() == 1, "no second machine row"

        row = db.query(ExecutionAgent).filter(ExecutionAgent.id == agent_id).one()
        assert row.is_backend is False, "a real worker now polls for this machine"
        assert row.status == "online"
    finally:
        db.close()


def test_backend_row_is_never_a_polling_worker(registry, monkeypatch):
    """A backend-only machine must not look like something that can take jobs."""
    _, Session = registry
    monkeypatch.setattr(dm, "_backend_machine_id", None)
    dm.ensure_backend_machine()
    db = Session()
    try:
        row = db.query(ExecutionAgent).filter(ExecutionAgent.is_backend.is_(True)).one()
        assert row.status != "online"
        assert (row.connected_devices or []) == [], "advertises no devices to run on"
        # poll_job hands jobs out by matching an agent's reported devices; a row
        # with none can never match, independent of the is_backend flag.
    finally:
        db.close()


def test_colocated_backend_and_agent_write_one_device_row(registry, monkeypatch):
    """The duplicate this phase exists to stop."""
    svc, Session = registry
    monkeypatch.setattr(dm, "_backend_machine_id", None)

    backend_id = dm.ensure_backend_machine()
    svc.register_local_device("UDID-SHARED", "iPhone 16 Pro")          # backend path
    svc.sync_agent_devices(backend_id, [_dev("UDID-SHARED")],
                           dm._local_hostname() + ".local")            # agent path

    rows = [r for r in _rows(Session) if r.udid == "UDID-SHARED"]
    assert len(rows) == 1, "one physical simulator, one row"
    assert rows[0].machine_id == backend_id


def test_provider_local_is_unchanged(registry, monkeypatch):
    """provider is display/provider info, not machine identity — it must not move."""
    svc, Session = registry
    monkeypatch.setattr(dm, "_backend_machine_id", None)
    dev = svc.register_local_device("UDID-LOCAL2", "iPhone 16 Pro")
    assert dev.provider == "local"
    assert _rows(Session)[0].provider == "local"


def test_existing_local_scheme_rows_are_not_touched(registry, monkeypatch):
    """Historical `local:<hostname>` rows must survive — no destructive cleanup."""
    svc, Session = registry
    db = Session()
    try:
        db.add(DeviceRecord(machine_id="local:Admins-MacBook-Pro", udid="UDID-OLD",
                            provider="local", name="legacy row"))
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(dm, "_backend_machine_id", None)
    dm.ensure_backend_machine()
    svc.register_local_device("UDID-NEW", "iPhone 16 Pro")

    machines = {r.machine_id for r in _rows(Session)}
    assert "local:Admins-MacBook-Pro" in machines, "legacy row still present"
    assert any(m != "local:Admins-MacBook-Pro" for m in machines), "new rows use the agent id"


def test_poll_job_matches_on_the_machine_identity_from_4d1():
    """The machine identity 4D.1 established is what 4D.3 routes on — not the
    hostname, provider or anything parsed out of a udid."""
    import ast, inspect, textwrap
    from automation.api.v1.routers import jobs
    tree = ast.parse(textwrap.dedent(inspect.getsource(jobs.poll_job)))
    fn = tree.body[0]
    if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant):
        fn.body = fn.body[1:]
    code = ast.unparse(fn).lower()
    assert "machine_id" in code
    for forbidden in ("hostname", "provider", "local:", "udid"):
        assert forbidden not in code


def test_agent_id_and_machine_id_are_separate_concepts():
    """Phase 4D.2 added TestRun.machine_id alongside agent_id — they are not
    interchangeable: one is the executing worker, the other the target machine.
    (This test asserted machine_id did not exist yet, before 4D.2.)"""
    from automation.database.models import TestRun
    cols = {c.name for c in TestRun.__table__.columns}
    assert {"agent_id", "machine_id"} <= cols


# ── Phase 4D.2: TestRun.machine_id — durable routing intent ──────────────────

from automation.database.models import TestRun


def test_machine_id_column_exists_and_is_nullable():
    col = {c.name: c for c in TestRun.__table__.columns}["machine_id"]
    assert col.nullable is True, "legacy runs have no machine and must stay valid"
    assert col.index is True


def test_a_new_run_persists_its_machine_id(registry):
    _, Session = registry
    db = Session()
    try:
        db.add(TestRun(id="r-new", test_suite="s", test_name="t", status="queued",
                       job_state="queued", machine_id="machine-X"))
        db.commit()
        assert db.query(TestRun).get("r-new").machine_id == "machine-X"
    finally:
        db.close()


def test_a_legacy_run_has_a_null_machine_id(registry):
    """Rows created without one — every historical row — read back as NULL."""
    _, Session = registry
    db = Session()
    try:
        db.add(TestRun(id="r-legacy", test_suite="s", test_name="t",
                       status="passed", job_state="completed"))
        db.commit()
        assert db.query(TestRun).get("r-legacy").machine_id is None
    finally:
        db.close()


def test_machine_id_comes_from_the_registry_not_from_a_bare_udid(registry, monkeypatch):
    """The helper confirms ownership against `devices` before answering."""
    svc, _ = registry
    monkeypatch.setattr(dm, "_backend_machine_id", None)
    machine = dm.ensure_backend_machine()

    # Unknown to the registry -> no machine, rather than a guess.
    assert dm.machine_for_local_device("UDID-NEVER-SEEN") is None
    assert dm.machine_for_local_device(None) is None
    assert dm.machine_for_local_device("") is None

    # Registered on this host -> the machine that owns it.
    svc.register_local_device("UDID-KNOWN", "iPhone 16 Pro")
    assert dm.machine_for_local_device("UDID-KNOWN") == machine


def test_machine_lookup_ignores_provider(registry, monkeypatch):
    """4D.1 showed provider can read "local" on an agent-owned machine, so the
    answer must not be derived from it."""
    svc, Session = registry
    monkeypatch.setattr(dm, "_backend_machine_id", None)
    machine = dm.ensure_backend_machine()
    svc.sync_agent_devices(machine, [_dev("UDID-VIA-AGENT")], "mac-a")

    db = Session()
    try:
        row = db.query(DeviceRecord).filter(DeviceRecord.udid == "UDID-VIA-AGENT").one()
        assert row.provider == machine, "provider is the agent id here, not 'local'"
    finally:
        db.close()
    assert dm.machine_for_local_device("UDID-VIA-AGENT") == machine


def test_a_device_owned_by_another_machine_is_not_claimed(registry, monkeypatch):
    """A UDID is unique per machine, not globally — another machine's device
    must not resolve to this one."""
    svc, _ = registry
    monkeypatch.setattr(dm, "_backend_machine_id", None)
    dm.ensure_backend_machine()
    svc.sync_agent_devices("some-other-mac", [_dev("UDID-ELSEWHERE")], "mac-b")
    assert dm.machine_for_local_device("UDID-ELSEWHERE") is None


def test_helper_returns_none_when_the_database_is_unreachable(monkeypatch):
    monkeypatch.setattr(dm, "_backend_machine_id", None)
    import automation.database.config as cfg
    monkeypatch.setattr(cfg, "SessionLocal",
                        lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert dm.machine_for_local_device("ANY") is None


def test_poll_job_reads_machine_id_but_adds_no_reservation():
    """4D.3 consumes the column 4D.2 added. The boundary that still holds is that
    no device reservation or capability matching came with it."""
    import ast, inspect, textwrap
    from automation.api.v1.routers import jobs
    tree = ast.parse(textwrap.dedent(inspect.getsource(jobs.poll_job)))
    fn = tree.body[0]
    if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant):
        fn.body = fn.body[1:]
    code = ast.unparse(fn).lower()
    assert "machine_id" in code, "4D.3 routes on it"
    for forbidden in ("reserve", "capabilit", "busy", "in_use"):
        assert forbidden not in code, "reservation belongs to Phase 4E"


def test_backend_local_run_paths_do_not_get_a_machine_id():
    """Scenario / cross-app / bot runs are executed in-process by the backend and
    never reach the agent queue — they must not be given routing intent."""
    import inspect
    from automation.api.v1.routers import scenario
    from automation.scenarios import cross_app_orchestrator
    for mod in (scenario, cross_app_orchestrator):
        src = inspect.getsource(mod)
        assert "machine_for_local_device" not in src, f"{mod.__name__} must stay NULL"


def test_the_suite_never_targets_the_real_database():
    """Guard for the leak that put `agent-a` / `UDID-X` rows in production."""
    import os
    from automation.database.config import DATABASE_URL
    for forbidden in (".vya-platform", "platform.db"):
        assert forbidden not in DATABASE_URL, f"tests are pointed at {DATABASE_URL}"
        assert forbidden not in os.environ.get("DATABASE_URL", "")
