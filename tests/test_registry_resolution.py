"""Phase 4D.4 — queue-time device resolution reads the registry, not simctl.

Deciding which machine executes a queued job cannot be answered by the backend's
own simulator list, so these paths now resolve from the `devices` table and return
(machine_id, udid) together — a UDID identifies a simulator within its machine and
means nothing without it.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from automation.database.models import Base, DeviceRecord, ExecutionAgent
from automation.device_manager import service as dm
from automation.device_manager.service import resolve_ios_device_record as resolve

MACHINE_A = "aaaaaaaa-0000-0000-0000-00000000000a"
MACHINE_B = "bbbbbbbb-0000-0000-0000-00000000000b"
UDID_X = "AAAAAAAA-1111-2222-3333-444444444444"
UDID_Y = "BBBBBBBB-5555-6666-7777-888888888888"


@pytest.fixture
def registry(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'r.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    import automation.database.config as cfg
    monkeypatch.setattr(cfg, "SessionLocal", Session)
    monkeypatch.setattr(dm, "_backend_machine_id", None)
    return Session


def _device(Session, machine_id, udid, *, name="iPhone 16 Pro", status="ONLINE",
            provider=None, age=0, platform="iOS"):
    db = Session()
    try:
        db.add(DeviceRecord(
            machine_id=machine_id, udid=udid, name=name, platform=platform,
            status=status, provider=provider if provider is not None else machine_id,
            last_seen=datetime.utcnow() - timedelta(seconds=age),
        ))
        db.commit()
    finally:
        db.close()


# ── Test 1 & 4: exact UDID ───────────────────────────────────────────────────

def test_exact_udid_resolves_to_its_registry_machine(registry):
    _device(registry, MACHINE_A, UDID_X)
    machine, udid, note = resolve(UDID_X)
    assert (machine, udid) == (MACHINE_A, UDID_X)
    assert note is None, "an exact, unambiguous hit needs no explanation"


def test_unknown_udid_resolves_to_nothing(registry):
    """No silent fallback to backend-local discovery."""
    machine, udid, note = resolve("NOT-REGISTERED-ANYWHERE")
    assert (machine, udid) == (None, None)
    assert "not registered" in note


# ── Test 2 & 3: the same UDID on two machines ────────────────────────────────

def test_same_udid_on_two_machines_is_ambiguous_not_arbitrary(registry):
    """Choosing one would invent a routing decision nobody made."""
    _device(registry, MACHINE_A, UDID_X)
    _device(registry, MACHINE_B, UDID_X)
    machine, udid, note = resolve(UDID_X)
    assert (machine, udid) == (None, None)
    assert "registered on 2 machines" in note
    assert MACHINE_A in note and MACHINE_B in note, "the note names both"


def test_a_known_machine_disambiguates(registry):
    _device(registry, MACHINE_A, UDID_X)
    _device(registry, MACHINE_B, UDID_X)
    assert resolve(UDID_X, machine_id=MACHINE_B)[:2] == (MACHINE_B, UDID_X)
    assert resolve(UDID_X, machine_id=MACHINE_A)[:2] == (MACHINE_A, UDID_X)


def test_a_udid_on_the_wrong_machine_does_not_resolve(registry):
    _device(registry, MACHINE_A, UDID_X)
    machine, udid, note = resolve(UDID_X, machine_id=MACHINE_B)
    assert (machine, udid) == (None, None)
    assert MACHINE_B in note


# ── Test 5: by name ──────────────────────────────────────────────────────────

def test_a_uniquely_named_device_resolves(registry):
    _device(registry, MACHINE_A, UDID_X, name="iPhone 17 Pro")
    assert resolve(name="iPhone 17 Pro")[:2] == (MACHINE_A, UDID_X)


def test_a_name_on_two_machines_is_ambiguous(registry):
    _device(registry, MACHINE_A, UDID_X, name="iPhone 17 Pro")
    _device(registry, MACHINE_B, UDID_Y, name="iPhone 17 Pro")
    machine, udid, note = resolve(name="iPhone 17 Pro")
    assert (machine, udid) == (None, None)
    assert "2 machines" in note


def test_an_unknown_name_resolves_to_nothing(registry):
    assert resolve(name="iPhone 99")[:2] == (None, None)


# ── Test 6 & 7: machine ownership, not provider or hostname ──────────────────

def test_the_backend_machine_id_is_used_not_a_local_hostname_scheme(registry):
    machine_id = dm.ensure_backend_machine()
    _device(registry, machine_id, UDID_X)
    resolved_machine, udid, _ = resolve(UDID_X)
    assert resolved_machine == machine_id
    assert not resolved_machine.startswith("local:"), "the retired scheme is not recreated"


@pytest.mark.parametrize("provider", ["local", "some-agent-uuid", None, ""])
def test_provider_does_not_affect_the_resolved_machine(registry, provider):
    """4D.1 showed provider flips with whichever writer was last."""
    _device(registry, MACHINE_A, UDID_X, provider=provider)
    assert resolve(UDID_X)[0] == MACHINE_A


def test_hostname_does_not_affect_the_resolved_machine(registry):
    db = registry()
    try:
        db.add(ExecutionAgent(id=MACHINE_A, hostname="mac-a", os="Darwin"))
        db.add(ExecutionAgent(id=MACHINE_B, hostname="mac-a", os="Darwin"))  # same name
        db.commit()
    finally:
        db.close()
    _device(registry, MACHINE_B, UDID_X)
    assert resolve(UDID_X)[0] == MACHINE_B, "resolved by machine_id, not by hostname"


# ── Auto-pick ────────────────────────────────────────────────────────────────

def test_auto_pick_prefers_a_recently_online_device(registry):
    _device(registry, MACHINE_A, UDID_Y, name="iPad Pro", status="DISCONNECTED", age=9000)
    _device(registry, MACHINE_A, UDID_X, name="iPhone 16 Pro", status="ONLINE", age=5)
    machine, udid, note = resolve()
    assert (machine, udid) == (MACHINE_A, UDID_X)
    assert "last reported online" in note, "the note must not overstate live truth"


def test_auto_pick_honours_the_family_hint(registry):
    _device(registry, MACHINE_A, UDID_X, name="iPhone 16 Pro", status="ONLINE", age=5)
    _device(registry, MACHINE_A, UDID_Y, name="iPad Pro 11-inch", status="ONLINE", age=5)
    assert resolve(prefer="iPad")[1] == UDID_Y
    assert resolve(prefer="iPhone")[1] == UDID_X


def test_auto_pick_is_deterministic(registry):
    _device(registry, MACHINE_B, UDID_Y, status="ONLINE", age=5)
    _device(registry, MACHINE_A, UDID_X, status="ONLINE", age=5)
    assert len({resolve()[1] for _ in range(5)}) == 1, "same registry, same answer"


def test_an_empty_registry_resolves_to_nothing(registry):
    machine, udid, note = resolve()
    assert (machine, udid) == (None, None)
    assert "no iOS devices are registered" in note


def test_android_devices_are_not_offered_to_an_ios_resolution(registry):
    _device(registry, MACHINE_A, "emulator-5554", name="Pixel", platform="Android")
    assert resolve()[:2] == (None, None)


# ── Test 8: legacy rows survive ──────────────────────────────────────────────

def test_legacy_local_hostname_rows_are_read_but_never_rewritten(registry):
    _device(registry, "local:Admins-MacBook-Pro", UDID_X, name="legacy")
    before = registry().query(DeviceRecord).count()
    resolve(UDID_X)
    resolve()
    db = registry()
    try:
        assert db.query(DeviceRecord).count() == before, "resolution writes nothing"
        row = db.query(DeviceRecord).filter(DeviceRecord.udid == UDID_X).one()
        assert row.machine_id == "local:Admins-MacBook-Pro", "untouched"
    finally:
        db.close()


# ── Test 10: no simctl, proven by making it explode ──────────────────────────

def test_resolution_never_invokes_simctl(registry, monkeypatch):
    """Not a source scan: any simctl call during resolution fails the test."""
    import subprocess

    def boom(cmd, *a, **k):
        argv = cmd if isinstance(cmd, (list, tuple)) else [str(cmd)]
        if any("simctl" in str(x) or "xcrun" in str(x) for x in argv):
            raise AssertionError(f"queue-time resolution shelled out to simctl: {argv}")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(dm.subprocess if hasattr(dm, "subprocess") else subprocess, "run", boom)

    _device(registry, MACHINE_A, UDID_X)
    assert resolve(UDID_X)[:2] == (MACHINE_A, UDID_X)
    assert resolve()[:2] == (MACHINE_A, UDID_X)
    assert resolve("UNKNOWN")[:2] == (None, None)


def test_registry_failure_degrades_to_no_resolution(registry, monkeypatch):
    import automation.database.config as cfg
    monkeypatch.setattr(cfg, "SessionLocal",
                        lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    machine, udid, note = resolve(UDID_X)
    assert (machine, udid) == (None, None)
    assert "registry unavailable" in note


# ── Test 9: the machine-local resolver is deliberately untouched ─────────────

def test_the_local_resolver_still_exists_for_machine_local_callers():
    """preparation / recorder / Script Editor act on THIS Mac and still need simctl."""
    from automation.projects.builder import app_builder
    import inspect
    src = inspect.getsource(app_builder.resolve_ios_device)
    assert "simctl" in src, "the local path is intentionally preserved"


def test_queue_paths_no_longer_call_the_local_resolver():
    import inspect
    from automation.ci_cd import pr_poller
    from automation.api.v1.routers import webhooks, pull_requests
    for mod in (pr_poller, webhooks, pull_requests):
        src = inspect.getsource(mod)
        assert "resolve_ios_device_record" in src, f"{mod.__name__} must use the registry"
        assert "app_builder.resolve_ios_device(" not in src, \
            f"{mod.__name__} must not resolve via simctl"
