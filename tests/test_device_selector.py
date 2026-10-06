"""Phase P0-2 — a scenario's device survives moving to another Mac.

A saved scenario stored the udid of the simulator it was authored against. On a
second Mac that udid does not exist, so every scenario failed at device
resolution — the platform's single largest portability blocker.

The selector is now resolved through the EXISTING machine-aware DeviceRecord
registry (resolve_ios_device_record); there is deliberately no second resolver.
A udid that is present locally is still used verbatim, so nothing changes on the
machine the scenarios were written on.

Ambiguity is always an error. Silently picking one of two same-named simulators
would make a run's device depend on row order.
"""
import json
import subprocess

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import automation.scenarios.service as svc
from automation.database.models import Base, DeviceRecord

LOCAL_MACHINE = "machine-A"
OTHER_MACHINE = "machine-B"

OLD_UDID = "AAAAAAAA-1111-2222-3333-444444444444"   # the original Mac's iPhone
NEW_UDID = "BBBBBBBB-5555-6666-7777-888888888888"   # this Mac's equivalent
IPAD_UDID = "CCCCCCCC-9999-0000-1111-222222222222"


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A machine-aware DeviceRecord registry.

    'Locally present' is expressed as rows owned by LOCAL_MACHINE -- the registry
    is the single authority, so there is no simctl stub to keep in sync with it.
    """
    engine = create_engine(f"sqlite:///{tmp_path/'dev.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    import automation.database.config as cfg
    monkeypatch.setattr(cfg, "SessionLocal", Session)

    import automation.device_manager.service as dm
    monkeypatch.setattr(dm, "local_machine_id", lambda: LOCAL_MACHINE)
    monkeypatch.setattr(dm, "_backend_machine_id", None, raising=False)

    # The backend injects the database half (Phase 4F.4); the agent does not.
    import automation.scenarios.run_records as run_records
    monkeypatch.setattr(svc, "_run_store", run_records)

    db = Session()
    # This machine has an iPhone 16 Pro with a DIFFERENT udid than the scenario's.
    db.add(DeviceRecord(id="d1", machine_id=LOCAL_MACHINE, udid=NEW_UDID,
                        name="iPhone 16 Pro", platform="iOS"))
    # The original Mac's row for the same model — same name, other machine.
    db.add(DeviceRecord(id="d2", machine_id=OTHER_MACHINE, udid=OLD_UDID,
                        name="iPhone 16 Pro", platform="iOS"))
    db.add(DeviceRecord(id="d3", machine_id=LOCAL_MACHINE, udid=IPAD_UDID,
                        name="iPad Pro 11-inch (M4)", platform="iOS"))
    db.commit()

    def _present(udids):
        """Keep only these udids registered to the CURRENT local machine."""
        import automation.device_manager.service as _dm
        here = _dm.local_machine_id()
        keep = {u.upper() for u in udids}
        for r in db.query(DeviceRecord).filter(DeviceRecord.machine_id == here).all():
            if (r.udid or "").upper() not in keep:
                db.delete(r)
        db.commit()

    _present([NEW_UDID, IPAD_UDID])          # OLD_UDID is NOT on this machine
    yield db, _present
    db.close()


# ── a udid that is here is untouched ────────────────────────────────────────

def test_01_a_local_udid_resolves_unchanged(registry):
    assert svc.resolve_device_selector(NEW_UDID) == NEW_UDID


def test_02_case_insensitive_local_match(registry):
    """A lowercase udid matches, and comes back in the registry's canonical case.

    Canonical, not echoed: this udid is what the reservation table and Appium are
    handed next, and CoreSimulator reports uppercase.
    """
    assert svc.resolve_device_selector(NEW_UDID.lower()) == NEW_UDID


def test_03_empty_and_none_pass_through(registry):
    assert svc.resolve_device_selector("") == ""
    assert svc.resolve_device_selector(None) is None


# ── a portable NAME resolves to whatever this machine has ───────────────────

def test_04_a_device_name_resolves_to_this_machines_udid(registry):
    assert svc.resolve_device_selector("iPhone 16 Pro") == NEW_UDID


def test_05_the_same_name_gives_a_DIFFERENT_udid_on_another_machine(registry, monkeypatch):
    """The whole point: one seed, two Macs, two udids."""
    db, present = registry
    assert svc.resolve_device_selector("iPhone 16 Pro") == NEW_UDID

    third = "DDDDDDDD-3333-4444-5555-666666666666"
    db.add(DeviceRecord(id="d4", machine_id="machine-C", udid=third,
                        name="iPhone 16 Pro", platform="iOS"))
    db.commit()
    import automation.device_manager.service as dm
    monkeypatch.setattr(dm, "local_machine_id", lambda: "machine-C")
    present([third])
    assert svc.resolve_device_selector("iPhone 16 Pro") == third


# ── the migration path: an old udid falls through to its name ───────────────

def test_06_an_original_mac_udid_resolves_via_its_registered_name(registry):
    """A scenario carrying the first Mac's udid still runs on the second."""
    out = svc.resolve_device_selector(OLD_UDID)
    assert out == NEW_UDID, f"expected this machine's iPhone 16 Pro, got {out}"


def test_07_a_wrong_machine_device_is_never_returned_directly(registry):
    """OLD_UDID belongs to machine-B; it must never come back verbatim."""
    assert svc.resolve_device_selector(OLD_UDID) != OLD_UDID


# ── failures are explicit, never a guess ────────────────────────────────────

def test_08_duplicate_names_on_this_machine_fail_explicitly(registry):
    db, present = registry
    twin = "EEEEEEEE-7777-8888-9999-000000000000"
    db.add(DeviceRecord(id="d5", machine_id=LOCAL_MACHINE, udid=twin,
                        name="iPhone 16 Pro", platform="iOS"))
    db.commit()
    present([NEW_UDID, IPAD_UDID, twin])
    with pytest.raises(svc.ScenarioError) as e:
        svc.resolve_device_selector("iPhone 16 Pro")
    assert "2 simulators named" in e.value.detail
    assert NEW_UDID in e.value.detail and twin in e.value.detail


def test_09_an_unknown_name_fails_clearly(registry):
    with pytest.raises(svc.ScenarioError) as e:
        svc.resolve_device_selector("iPhone 99 Ultra")
    assert "iPhone 99 Ultra" in e.value.detail


def test_10_an_unregistered_udid_is_left_alone_not_guessed_at(registry):
    """An unknown udid is passed through UNCHANGED, never swapped for a guess.

    Deliberately not an error. A udid is directly drivable by Appium, and a
    single-machine install that never ran device discovery has an empty registry
    -- failing here would make device registration a new precondition for every
    run. The rule P0-2 enforces is 'never silently substitute another device',
    and passing the value through untouched satisfies it: no other machine's
    device can be reached this way.
    """
    unknown = "FFFFFFFF-0000-0000-0000-000000000000"
    out = svc.resolve_device_selector(unknown)
    assert out == unknown, f"an unregistered udid was rewritten to {out}"
    assert out != NEW_UDID and out != IPAD_UDID, "substituted a local device"


def test_11_no_arbitrary_fallback_when_the_registry_is_empty(registry, monkeypatch):
    """An empty registry must fail, not pick whatever simulator is around."""
    db, present = registry
    db.query(DeviceRecord).delete()
    db.commit()
    with pytest.raises(svc.ScenarioError):
        svc.resolve_device_selector("iPhone 16 Pro")


def test_12_stale_registry_rows_do_not_create_false_ambiguity(registry):
    """A row for a device that no longer exists must not block resolution.

    The live registry holds exactly this: a leaked test fixture named
    'iPhone 16 Pro' with udid 'UDID-LOCAL' that is not a simulator at all.
    """
    db, present = registry
    db.add(DeviceRecord(id="d6", machine_id=LOCAL_MACHINE, udid="UDID-STALE",
                        name="iPhone 16 Pro", platform="iOS"))
    db.commit()
    present([NEW_UDID, IPAD_UDID])          # UDID-STALE is not a real simulator
    assert svc.resolve_device_selector("iPhone 16 Pro") == NEW_UDID


# ── wired into the execution choke point ────────────────────────────────────

def test_13_resolve_run_rewrites_the_request_device(registry):
    req = svc.ScenarioRequest(project_id="p1", device_id=OLD_UDID, name="n",
                              steps=["tap Login"], bundle_id="com.example.app",
                              save=False, prepare=False)
    db, _ = registry
    svc.resolve_run(req, db=db)          # backend caller: passes a session
    assert req.device_id == NEW_UDID, "resolve_run() did not resolve the device"


def test_14_resolve_run_leaves_a_local_udid_alone(registry):
    req = svc.ScenarioRequest(project_id="p1", device_id=NEW_UDID, name="n",
                              steps=["tap Login"], bundle_id="com.example.app",
                              save=False, prepare=False)
    db, _ = registry
    svc.resolve_run(req, db=db)
    assert req.device_id == NEW_UDID


def test_15_resolve_run_surfaces_an_unresolvable_device(registry):
    req = svc.ScenarioRequest(project_id="p1", device_id="iPhone 99 Ultra", name="n",
                              steps=["tap Login"], bundle_id="com.example.app",
                              save=False, prepare=False)
    db, _ = registry
    with pytest.raises(svc.ScenarioError):
        svc.resolve_run(req, db=db)


# ── the seed ships portable names ───────────────────────────────────────────

def test_16_the_seed_carries_no_machine_specific_udids():
    data = json.loads(open("seeds/platform.json").read())
    bad = [s["name"] for s in data["scenarios"]
           if (s.get("device_id") or "").count("-") == 4
           and len(s.get("device_id") or "") == 36]
    assert not bad, f"seed still pins udids for: {bad}"
    names = {s.get("device_id") for s in data["scenarios"]}
    assert names == {"iPhone 16 Pro", "iPad Pro 11-inch (M4)"}, names


def test_17_no_parallel_resolver_module_exists():
    import os
    assert not os.path.exists("automation/scenarios/device_roles.py"), \
        "device_roles.py is back — resolve_ios_device_record is the single source"


# ── machine-awareness: the same udid on two Macs ────────────────────────────

def test_18_an_explicit_machine_id_selects_that_machines_device(registry):
    """CASE 2: the same udid registered on two machines is disambiguated by
    naming the machine, and each machine gets ITS OWN row."""
    db, present = registry
    shared = "12345678-1111-1111-1111-111111111111"
    db.add(DeviceRecord(id="s1", machine_id=LOCAL_MACHINE, udid=shared,
                        name="iPhone SE", platform="iOS"))
    db.add(DeviceRecord(id="s2", machine_id=OTHER_MACHINE, udid=shared,
                        name="iPhone SE", platform="iOS"))
    db.commit()

    from automation.device_manager.service import resolve_ios_device_record
    assert resolve_ios_device_record(shared, machine_id=LOCAL_MACHINE)[:2] == \
        (LOCAL_MACHINE, shared)
    assert resolve_ios_device_record(shared, machine_id=OTHER_MACHINE)[:2] == \
        (OTHER_MACHINE, shared)


def test_19_the_same_udid_on_two_machines_is_ambiguous_without_one(registry):
    """CASE 3: no machine named -> refuse, never silently pick a machine."""
    db, present = registry
    shared = "12345678-2222-2222-2222-222222222222"
    db.add(DeviceRecord(id="s3", machine_id=LOCAL_MACHINE, udid=shared,
                        name="iPhone SE", platform="iOS"))
    db.add(DeviceRecord(id="s4", machine_id=OTHER_MACHINE, udid=shared,
                        name="iPhone SE", platform="iOS"))
    db.commit()

    from automation.device_manager.service import resolve_ios_device_record
    machine, udid, note = resolve_ios_device_record(shared)
    assert (machine, udid) == (None, None), "picked a machine nobody named"
    assert "disambiguate" in note


def test_20_selector_honours_an_explicit_machine_id(registry):
    """The scenario selector itself is machine-aware, not just the registry."""
    assert svc.resolve_device_selector("iPhone 16 Pro",
                                       machine_id=OTHER_MACHINE) == OLD_UDID
    assert svc.resolve_device_selector("iPhone 16 Pro",
                                       machine_id=LOCAL_MACHINE) == NEW_UDID


def test_21_never_falls_back_to_another_machines_device(registry):
    """CASE 4 + the anti-fallback rule: a device absent from the named machine
    must fail, even though it is registered on a different one."""
    with pytest.raises(svc.ScenarioError) as e:
        svc.resolve_device_selector("iPad Pro 11-inch (M4)",
                                    machine_id=OTHER_MACHINE)
    assert IPAD_UDID not in str(e.value.detail), \
        "leaked the local machine's udid for a request scoped to another machine"


def test_22_no_registry_record_never_reaches_simctl(registry, monkeypatch):
    """CASE 8 + CASE 10: an unregistered but physically real simulator must not
    be fabricated into a resolution. Any simctl call here is a failure."""
    import subprocess
    calls = []

    def _boom(argv, *a, **kw):
        calls.append(argv)
        raise AssertionError(f"resolution shelled out to {argv}")

    monkeypatch.setattr(subprocess, "run", _boom)
    unknown = "99999999-0000-0000-0000-000000000000"
    assert svc.resolve_device_selector(unknown) == unknown
    # And an unresolvable NAME still fails without shelling out.
    with pytest.raises(svc.ScenarioError):
        svc.resolve_device_selector("iPhone 99 Ultra")
    assert not calls, f"simctl was consulted: {calls}"


def test_23_the_resolver_body_contains_no_simctl(registry):
    """Structural: the scenario selector must not grow a simctl path later.

    Checks the compiled CODE, not the source text -- the docstring legitimately
    says the word 'simctl' while explaining why it is never called, and a source
    grep would match that prose.
    """
    import inspect
    names = set()
    import automation.scenarios.run_records as _rr
    for fn in (svc.resolve_device_selector, svc._device_registry,
               _rr.resolve_device, _rr._registered_name):
        code = fn.__code__
        doc = fn.__doc__ or ""
        names |= set(code.co_names)
        names |= {c for c in code.co_consts
                  if isinstance(c, str) and c != doc}
    joined = " ".join(names).lower()
    assert "simctl" not in joined, "the selector shells out to simctl"
    assert "subprocess" not in joined, "the selector imports subprocess"
    # And the module-level helpers the old implementation used are gone.
    assert not hasattr(svc, "_local_udids"), "the simctl helper is back"


def test_24_a_legacy_null_machine_id_does_not_invent_a_machine(registry):
    """CASE 9: a legacy row with machine_id NULL is not silently adopted."""
    db, present = registry
    from automation.device_manager.service import resolve_ios_device_record
    legacy = "77777777-0000-0000-0000-000000000000"
    db.add(DeviceRecord(id="L1", machine_id="", udid=legacy,
                        name="Legacy Phone", platform="iOS"))
    db.commit()
    machine, udid, note = resolve_ios_device_record(legacy, machine_id=LOCAL_MACHINE)
    assert (machine, udid) == (None, None), \
        "a machine-less row was claimed by this machine"
