"""Phase 4D.5 — end-to-end integration across the whole machine-aware stack.

One flow, four phases' worth of machinery:

    resolve_ios_device_record()   4D.4  registry -> (machine_id, udid)
        -> TestRun.machine_id     4D.2  durable routing intent
        -> poll_job()             4D.3  machine predicate + device predicate
        -> atomic claim           pre-existing, unchanged

Every test drives the real functions against an isolated database. Nothing here
mocks the routing decision it is meant to be proving.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from automation.api.v1.routers import jobs as jobs_router
from automation.database.models import (Base, DeviceRecord, ExecutionAgent, TestProject,
                                        TestRun)
from automation.device_manager import service as dm
from automation.ops import monitor as ops_monitor

MACHINE_A = "aaaaaaaa-0000-0000-0000-00000000000a"
MACHINE_B = "bbbbbbbb-0000-0000-0000-00000000000b"
UDID_X = "AAAAAAAA-1111-2222-3333-444444444444"
UDID_Y = "BBBBBBBB-5555-6666-7777-888888888888"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated DB wired into every layer that opens its own session."""
    engine = create_engine(f"sqlite:///{tmp_path/'4d.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    import automation.database.config as cfg
    monkeypatch.setattr(cfg, "SessionLocal", Session)
    monkeypatch.setattr(ops_monitor, "SessionLocal", Session)
    monkeypatch.setattr(dm, "_backend_machine_id", None)

    db = Session()
    db.add(ExecutionAgent(id=MACHINE_A, hostname="mac-a", os="Darwin", status="online"))
    db.add(ExecutionAgent(id=MACHINE_B, hostname="mac-b", os="Darwin", status="online"))
    db.add(TestProject(id="proj-1", name="Demo", git_url="https://example.com/x.git"))
    db.commit()
    yield db, Session
    db.close()


def _device(db, machine_id, udid, *, name="iPhone 16 Pro", status="ONLINE",
            provider=None, age=5, platform="iOS"):
    db.add(DeviceRecord(
        machine_id=machine_id, udid=udid, name=name, platform=platform, status=status,
        provider=machine_id if provider is None else provider,
        last_seen=datetime.utcnow() - timedelta(seconds=age)))
    db.commit()


def _queue(db, run_id, *, device_name, machine_id, offset=0):
    db.add(TestRun(id=run_id, project_id="proj-1", test_suite="Demo", test_name=run_id,
                   status="queued", job_state="queued", device_name=device_name,
                   machine_id=machine_id,
                   created_at=datetime.utcnow() + timedelta(seconds=offset)))
    db.commit()
    return run_id


def _poll(db, agent_id, devices):
    req = jobs_router.PollJobRequest(agent_id=agent_id, connected_devices=devices)
    got = jobs_router.poll_job(req, db=db, _agent="agent")
    return got if (got or {}).get("job_id") else None


def _row(db, run_id):
    db.expire_all()
    return db.query(TestRun).filter(TestRun.id == run_id).one()


# ── 1. The critical end-to-end path ──────────────────────────────────────────

def test_01_new_machine_aware_job_end_to_end(env):
    db, _ = env
    _device(db, MACHINE_A, UDID_X)

    machine, udid, note = dm.resolve_ios_device_record(UDID_X)
    assert (machine, udid, note) == (MACHINE_A, UDID_X, None)

    _queue(db, "job-1", device_name=udid, machine_id=machine)

    assert _poll(db, MACHINE_B, [UDID_X]) is None, "wrong machine is not eligible"
    got = _poll(db, MACHINE_A, [UDID_X])
    assert got["job_id"] == "job-1" and got["device_id"] == UDID_X

    r = _row(db, "job-1")
    assert (r.job_state, r.agent_id, r.machine_id) == ("assigned", MACHINE_A, MACHINE_A)


# ── 2. Same UDID on two machines ─────────────────────────────────────────────

def test_02a_same_udid_no_constraint_is_ambiguous(env):
    db, _ = env
    _device(db, MACHINE_A, UDID_X)
    _device(db, MACHINE_B, UDID_X)
    machine, udid, note = dm.resolve_ios_device_record(UDID_X)
    assert (machine, udid) == (None, None), "must not silently choose a machine"
    assert MACHINE_A in note and MACHINE_B in note


def test_02b_explicit_machine_disambiguates_and_routes(env):
    db, _ = env
    _device(db, MACHINE_A, UDID_X)
    _device(db, MACHINE_B, UDID_X)

    machine, udid, _ = dm.resolve_ios_device_record(UDID_X, machine_id=MACHINE_B)
    assert (machine, udid) == (MACHINE_B, UDID_X)

    _queue(db, "job-2", device_name=udid, machine_id=machine)
    assert _poll(db, MACHINE_A, [UDID_X]) is None, "A holds the same UDID but not the job"
    assert _poll(db, MACHINE_B, [UDID_X])["job_id"] == "job-2"


# ── 3. Legacy jobs ───────────────────────────────────────────────────────────

def test_03_legacy_null_machine_job_runs_on_either_machine(env):
    db, _ = env
    _queue(db, "job-legacy-a", device_name=UDID_X, machine_id=None)
    assert _poll(db, MACHINE_A, [UDID_X])["job_id"] == "job-legacy-a"

    _queue(db, "job-legacy-b", device_name=UDID_X, machine_id=None)
    assert _poll(db, MACHINE_B, [UDID_X])["job_id"] == "job-legacy-b"


# ── 4. Reclaim ───────────────────────────────────────────────────────────────

def test_04_reclaim_preserves_machine_pinning(env):
    db, _ = env
    _device(db, MACHINE_A, UDID_X)
    _queue(db, "job-r", device_name=UDID_X, machine_id=MACHINE_A)
    assert _poll(db, MACHINE_A, [UDID_X])["job_id"] == "job-r"

    agent = db.query(ExecutionAgent).filter(ExecutionAgent.id == MACHINE_A).one()
    agent.status, agent.last_heartbeat = "offline", datetime.utcnow() - timedelta(seconds=600)
    db.query(TestRun).filter(TestRun.id == "job-r").update({"job_state": "running"})
    db.commit()

    ops_monitor.OpsMonitor().reclaim_stranded_jobs()

    r = _row(db, "job-r")
    assert (r.job_state, r.agent_id, r.machine_id) == ("queued", None, MACHINE_A)
    assert _poll(db, MACHINE_B, [UDID_X]) is None, "reclaim did not make it global"
    assert _poll(db, MACHINE_A, [UDID_X])["job_id"] == "job-r"


# ── 5. Ambiguous legacy registry, and what the queue does about it ───────────

def test_05_ambiguous_registration_fails_safely_and_the_job_still_runs(env):
    """The distinction that matters: the RESOLVER refusing to assign a machine is
    not the same as the JOB being unrunnable. It falls back to legacy behavior."""
    db, Session = env
    # Exactly the shape the live database has: a legacy local: row plus an agent row.
    _device(db, "local:mac-a", UDID_X, provider="local", age=4000, status="ONLINE")
    _device(db, MACHINE_B, UDID_X, provider=MACHINE_B, age=5)

    machine, udid, note = dm.resolve_ios_device_record(UDID_X)
    assert (machine, udid) == (None, None), "no freshest-wins, no provider preference"
    assert "2 machines" in note

    before = db.query(DeviceRecord).count()
    # The caller keeps its device_name and leaves machine_id NULL — a legacy job.
    _queue(db, "job-amb", device_name=UDID_X, machine_id=None)
    assert _poll(db, MACHINE_B, [UDID_X])["job_id"] == "job-amb", "still executable"
    assert db.query(DeviceRecord).count() == before, "resolution deleted/merged nothing"


# ── 6. Provider independence ─────────────────────────────────────────────────

@pytest.mark.parametrize("provider", ["local", "some-agent-uuid", None, ""])
def test_06_provider_never_influences_routing(env, provider):
    db, _ = env
    _device(db, MACHINE_A, UDID_X, provider=provider)
    machine, udid, _ = dm.resolve_ios_device_record(UDID_X)
    assert machine == MACHINE_A

    _queue(db, f"job-p-{provider!r}", device_name=udid, machine_id=machine)
    assert _poll(db, MACHINE_B, [UDID_X]) is None
    assert _poll(db, MACHINE_A, [UDID_X]) is not None


# ── 7. Hostname independence ─────────────────────────────────────────────────

def test_07_identical_hostnames_do_not_confuse_routing(env):
    db, _ = env
    for a in db.query(ExecutionAgent).all():
        a.hostname = "same-name"          # deliberately identical
    db.commit()

    _device(db, MACHINE_B, UDID_X)
    machine, udid, _ = dm.resolve_ios_device_record(UDID_X)
    assert machine == MACHINE_B, "routed by machine_id, not hostname"

    _queue(db, "job-h", device_name=udid, machine_id=machine)
    assert _poll(db, MACHINE_A, [UDID_X]) is None
    assert _poll(db, MACHINE_B, [UDID_X])["job_id"] == "job-h"


# ── 8. Queue-caller compatibility ────────────────────────────────────────────

def test_08_queue_callers_still_produce_valid_testruns(env):
    """The three registry-backed callers, plus the Script Editor path."""
    import inspect
    from automation.ci_cd import pr_poller
    from automation.api.v1.routers import webhooks, pull_requests
    from automation.runner import service as runner_service

    for mod in (pr_poller, webhooks, pull_requests):
        src = inspect.getsource(mod)
        assert "resolve_ios_device_record" in src
        assert "app_builder.resolve_ios_device(" not in src
        assert "machine_id" in src

    # Script Editor boots locally first, so it keeps machine_for_local_device.
    src = inspect.getsource(runner_service)
    assert "machine_id" in src and "machine_for_local_device" in src


def test_08b_a_resolved_device_produces_a_routable_run(env):
    """Simulates what a queue caller does, using the real resolver + real poll."""
    db, _ = env
    _device(db, MACHINE_A, UDID_X)
    machine, udid, _ = dm.resolve_ios_device_record(UDID_X)
    _queue(db, "job-c", device_name=udid or "pending", machine_id=machine)
    r = _row(db, "job-c")
    assert r.device_name == UDID_X and r.machine_id == MACHINE_A
    assert _poll(db, MACHINE_A, [UDID_X])["job_id"] == "job-c"


def test_08c_unresolvable_device_yields_a_null_machine_but_a_valid_run(env):
    db, _ = env
    machine, udid, note = dm.resolve_ios_device_record("UNREGISTERED")
    assert (machine, udid) == (None, None)
    _queue(db, "job-u", device_name="UNREGISTERED", machine_id=machine)
    assert _row(db, "job-u").machine_id is None
    assert _poll(db, MACHINE_A, ["UNREGISTERED"])["job_id"] == "job-u", "legacy behavior"


# ── 9. All three resolution modes route correctly ────────────────────────────

def test_09_exact_udid_name_and_auto_pick_all_route(env):
    db, _ = env
    _device(db, MACHINE_A, UDID_X, name="iPhone 17 Pro")
    _device(db, MACHINE_B, UDID_Y, name="iPad Pro 11-inch")

    for i, (kwargs, want_machine, want_udid) in enumerate([
        ({"device_id": UDID_X}, MACHINE_A, UDID_X),
        ({"name": "iPad Pro 11-inch"}, MACHINE_B, UDID_Y),
        ({"prefer": "iPad"}, MACHINE_B, UDID_Y),
    ]):
        machine, udid, _ = dm.resolve_ios_device_record(**kwargs)
        assert (machine, udid) == (want_machine, want_udid), kwargs

        run_id = f"job-9-{i}"
        _queue(db, run_id, device_name=udid, machine_id=machine, offset=i)
        other = MACHINE_B if want_machine == MACHINE_A else MACHINE_A
        assert _poll(db, other, [udid]) is None
        assert _poll(db, want_machine, [udid])["job_id"] == run_id


def test_09b_auto_pick_stays_deterministic(env):
    db, _ = env
    _device(db, MACHINE_B, UDID_Y)
    _device(db, MACHINE_A, UDID_X)
    assert len({dm.resolve_ios_device_record()[1] for _ in range(5)}) == 1


# ── 10. The "booted" boundary ────────────────────────────────────────────────

def test_10_booted_means_recently_reported_not_guaranteed_now(env):
    """A device last seen beyond DEVICE_STALE_AFTER is not preferred, and the note
    never claims live boot state."""
    db, _ = env
    stale_age = int(dm.DEVICE_STALE_AFTER.total_seconds()) + 60
    _device(db, MACHINE_A, UDID_Y, name="iPad Pro", status="ONLINE", age=stale_age)
    _device(db, MACHINE_A, UDID_X, name="iPhone 16 Pro", status="ONLINE", age=5)

    machine, udid, note = dm.resolve_ios_device_record()
    assert udid == UDID_X, "the recently-reported device wins"
    assert "last reported online" in note
    for overclaim in ("is booted", "currently booted", "guaranteed"):
        assert overclaim not in note.lower()

    # A stale device is still resolvable when named explicitly — staleness is a
    # preference, not an eligibility rule.
    assert dm.resolve_ios_device_record(UDID_Y)[:2] == (MACHINE_A, UDID_Y)


# ── 11. Atomic claim ─────────────────────────────────────────────────────────

def test_11_exactly_one_claim_wins_a_race(env):
    db, _ = env
    _queue(db, "job-race", device_name=UDID_X, machine_id=None)
    first = _poll(db, MACHINE_A, [UDID_X])
    second = _poll(db, MACHINE_B, [UDID_X])
    assert first["job_id"] == "job-race" and second is None
    assert _row(db, "job-race").agent_id == MACHINE_A


def test_11b_pinned_job_race_only_the_owner_can_win(env):
    db, _ = env
    _device(db, MACHINE_A, UDID_X)
    _queue(db, "job-race-2", device_name=UDID_X, machine_id=MACHINE_A)
    assert _poll(db, MACHINE_B, [UDID_X]) is None
    assert _poll(db, MACHINE_B, [UDID_X]) is None
    assert _poll(db, MACHINE_A, [UDID_X])["job_id"] == "job-race-2"
    assert _poll(db, MACHINE_A, [UDID_X]) is None, "claimed once only"


# ── 12. Historical data is immutable ─────────────────────────────────────────

def test_12_the_full_stack_never_rewrites_historical_rows(env):
    """Hydration + resolution + polling + reclaim, against representative history."""
    db, Session = env
    _device(db, "local:mac-a", UDID_X, provider="local", name="legacy sim", age=99999)
    db.add(TestRun(id="hist-1", project_id="proj-1", test_suite="Demo",
                   test_name="historical", status="passed", job_state="completed",
                   device_name=UDID_X, machine_id=None, agent_id="old-agent-id"))
    db.commit()

    def snapshot():
        db.expire_all()
        d = db.query(DeviceRecord).filter(DeviceRecord.udid == UDID_X).one()
        t = db.query(TestRun).filter(TestRun.id == "hist-1").one()
        return ((d.machine_id, d.provider, d.hostname, d.udid, d.name),
                (t.machine_id, t.agent_id, t.job_state, t.device_name))

    before = snapshot()
    device_rows, run_rows = db.query(DeviceRecord).count(), db.query(TestRun).count()

    dm.DeviceDiscoveryService().hydrate_from_db()
    dm.resolve_ios_device_record(UDID_X)
    dm.resolve_ios_device_record()
    _poll(db, MACHINE_A, [UDID_X])
    ops_monitor.OpsMonitor().reclaim_stranded_jobs()

    assert snapshot() == before, "a historical device or run was rewritten"
    assert db.query(DeviceRecord).count() == device_rows, "devices deleted or merged"
    assert db.query(TestRun).count() == run_rows
    assert not db.query(DeviceRecord).filter(
        DeviceRecord.machine_id.like("local:%"),
        DeviceRecord.udid != UDID_X).count(), "no new local: rows were created"


def test_12b_hydration_does_not_backfill_machine_id(env):
    db, _ = env
    db.add(TestRun(id="hist-2", project_id="proj-1", test_suite="Demo", test_name="h",
                   status="failed", job_state="failed", device_name=UDID_X))
    db.commit()
    dm.DeviceDiscoveryService().hydrate_from_db()
    dm.ensure_backend_machine()
    assert _row(db, "hist-2").machine_id is None
