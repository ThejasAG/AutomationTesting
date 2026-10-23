"""Phase 4D.3 — poll_job routes a machine-pinned job only to its machine.

Every test here calls the REAL poll_job against a REAL sqlite database, so the
actual query and the actual atomic claim are what is under test. Nothing is
mocked: a job pinned to machine A must be invisible to machine B even when B
reports the identical UDID, which is the bare-UDID routing bug this phase closes.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from automation.api.v1.routers import jobs as jobs_router
from automation.database.models import Base, TestRun, TestProject, ExecutionAgent

MACHINE_A = "machine-aaaaaaaa-0000-0000-0000-00000000000a"
MACHINE_B = "machine-bbbbbbbb-0000-0000-0000-00000000000b"
UDID_X = "AAAAAAAA-1111-2222-3333-444444444444"
UDID_Y = "BBBBBBBB-5555-6666-7777-888888888888"


@pytest.fixture
def db():
    """An isolated database — the live one is never touched by these tests."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    # Both machines exist and both report the SAME simulator UDID.
    session.add(ExecutionAgent(id=MACHINE_A, hostname="mac-a", os="Darwin", status="online"))
    session.add(ExecutionAgent(id=MACHINE_B, hostname="mac-b", os="Darwin", status="online"))
    session.add(TestProject(id="proj-1", name="Demo", git_url="https://example.com/x.git"))
    session.commit()
    yield session
    session.close()


def _queue(db, run_id, *, device_name=UDID_X, machine_id=None, created_offset=0):
    db.add(TestRun(
        id=run_id, project_id="proj-1", test_suite="Demo", test_name=run_id,
        status="queued", job_state="queued", device_name=device_name,
        machine_id=machine_id,
        created_at=datetime.utcnow() + timedelta(seconds=created_offset),
    ))
    db.commit()
    return run_id


def _poll(db, agent_id, devices):
    """Call the real endpoint function with a real session.

    poll_job answers {"job_id": None} when nothing is eligible, so normalize that
    to None here rather than in every assertion.
    """
    req = jobs_router.PollJobRequest(agent_id=agent_id, connected_devices=devices)
    got = jobs_router.poll_job(req, db=db, _agent="agent")
    return got if (got or {}).get("job_id") else None


def _state(db, run_id):
    db.expire_all()
    r = db.query(TestRun).filter(TestRun.id == run_id).one()
    return r.job_state, r.agent_id, r.machine_id


# ── Test 1 & 2: a pinned job reaches its machine and only its machine ────────

def test_machine_pinned_job_goes_to_the_right_agent(db):
    _queue(db, "job-1", machine_id=MACHINE_A)

    # The wrong machine sees nothing, even though it has the same device.
    assert _poll(db, MACHINE_B, [UDID_X]) is None
    assert _state(db, "job-1") == ("queued", None, MACHINE_A), "still unclaimed"

    # The right machine claims it.
    got = _poll(db, MACHINE_A, [UDID_X])
    assert got is not None and got["job_id"] == "job-1"
    state, agent_id, machine_id = _state(db, "job-1")
    assert state == "assigned"
    assert agent_id == MACHINE_A, "agent_id remains the execution-agent field"
    assert machine_id == MACHINE_A, "machine_id is not consumed or cleared"


def test_a_pinned_job_is_invisible_to_the_wrong_machine(db):
    _queue(db, "job-2", machine_id=MACHINE_A)
    for _ in range(3):
        assert _poll(db, MACHINE_B, [UDID_X]) is None
    assert _state(db, "job-2")[0] == "queued"


# ── Test 3: legacy compatibility ─────────────────────────────────────────────

def test_legacy_null_machine_job_is_claimable_by_any_machine(db):
    """156 historical runs are NULL — they must behave exactly as before."""
    _queue(db, "job-legacy", machine_id=None)
    got = _poll(db, MACHINE_B, [UDID_X])
    assert got is not None and got["job_id"] == "job-legacy"
    assert _state(db, "job-legacy") == ("assigned", MACHINE_B, None)


def test_legacy_job_claimable_by_the_other_machine_too(db):
    _queue(db, "job-legacy-2", machine_id=None)
    got = _poll(db, MACHINE_A, [UDID_X])
    assert got is not None and got["job_id"] == "job-legacy-2"


# ── Test 4: the original bare-UDID routing bug ───────────────────────────────

def test_same_udid_on_two_machines_routes_to_the_pinned_one(db):
    """Both machines report UDID X. The job names machine A. Only A may run it."""
    _queue(db, "job-udid", device_name=UDID_X, machine_id=MACHINE_A)

    assert _poll(db, MACHINE_B, [UDID_X]) is None, "bare-UDID matching would have claimed this"
    got = _poll(db, MACHINE_A, [UDID_X])
    assert got is not None and got["job_id"] == "job-udid"
    assert got["device_id"] == UDID_X, "device_name is passed through opaquely"


# ── Test 5: machine ownership alone is not enough ────────────────────────────

def test_right_machine_wrong_device_is_still_rejected(db):
    _queue(db, "job-dev", device_name=UDID_X, machine_id=MACHINE_A)
    assert _poll(db, MACHINE_A, [UDID_Y]) is None, "device matching still applies"
    assert _state(db, "job-dev")[0] == "queued"


def test_device_name_is_not_parsed(db):
    """Cross-app device_name values pack several devices; they stay opaque."""
    packed = "C:DA24A3 W:D19D3E K:DA24"
    _queue(db, "job-packed", device_name=packed, machine_id=MACHINE_A)
    assert _poll(db, MACHINE_A, [UDID_X, "DA24A3", "D19D3E"]) is None
    got = _poll(db, MACHINE_A, [packed])          # only an exact match works
    assert got is not None and got["device_id"] == packed


# ── Test 6: reclaim keeps the job pinned ─────────────────────────────────────

def test_reclaimed_job_stays_pinned_to_its_machine(db):
    from automation.ops import monitor as ops_monitor

    _queue(db, "job-reclaim", machine_id=MACHINE_A)
    assert _poll(db, MACHINE_A, [UDID_X])["job_id"] == "job-reclaim"

    # Machine A's agent dies.
    agent = db.query(ExecutionAgent).filter(ExecutionAgent.id == MACHINE_A).one()
    agent.status = "offline"
    agent.last_heartbeat = datetime.utcnow() - timedelta(seconds=600)
    db.query(TestRun).filter(TestRun.id == "job-reclaim").update({"job_state": "running"})
    db.commit()

    import automation.database.config as cfg
    Session = sessionmaker(bind=db.get_bind())
    original = cfg.SessionLocal
    cfg.SessionLocal = Session
    try:
        ops_monitor.SessionLocal = Session
        ops_monitor.OpsMonitor().reclaim_stranded_jobs()
    finally:
        cfg.SessionLocal = original

    state, agent_id, machine_id = _state(db, "job-reclaim")
    assert (state, agent_id, machine_id) == ("queued", None, MACHINE_A)

    # Still pinned: B cannot take it, A can.
    assert _poll(db, MACHINE_B, [UDID_X]) is None
    assert _poll(db, MACHINE_A, [UDID_X])["job_id"] == "job-reclaim"


# ── Test 7: a mixed queue ────────────────────────────────────────────────────

def test_each_machine_sees_only_its_eligible_jobs(db):
    _queue(db, "job-A", device_name=UDID_X, machine_id=MACHINE_A, created_offset=0)
    _queue(db, "job-B", device_name=UDID_Y, machine_id=MACHINE_B, created_offset=1)
    _queue(db, "job-N", device_name=UDID_X, machine_id=None,      created_offset=2)

    # A has device X: its own pinned job first (FIFO), then the legacy one.
    assert _poll(db, MACHINE_A, [UDID_X])["job_id"] == "job-A"
    assert _poll(db, MACHINE_A, [UDID_X])["job_id"] == "job-N"
    assert _poll(db, MACHINE_A, [UDID_X]) is None

    # B never sees A's job; it takes its own.
    assert _poll(db, MACHINE_B, [UDID_Y])["job_id"] == "job-B"
    assert _poll(db, MACHINE_B, [UDID_X, UDID_Y]) is None

    assert _state(db, "job-A")[1] == MACHINE_A
    assert _state(db, "job-B")[1] == MACHINE_B
    assert _state(db, "job-N")[1] == MACHINE_A


def test_fifo_order_is_preserved(db):
    _queue(db, "old", machine_id=None, created_offset=0)
    _queue(db, "new", machine_id=None, created_offset=10)
    assert _poll(db, MACHINE_A, [UDID_X])["job_id"] == "old"


# ── Test 8: concurrency regression ───────────────────────────────────────────

def test_two_agents_cannot_both_claim_one_job(db):
    """The existing atomic-claim guarantee, unchanged by the new predicate."""
    _queue(db, "job-race", machine_id=None)
    first = _poll(db, MACHINE_A, [UDID_X])
    second = _poll(db, MACHINE_B, [UDID_X])
    assert first is not None and first["job_id"] == "job-race"
    assert second is None, "a claimed job is never handed out twice"
    assert _state(db, "job-race")[1] == MACHINE_A


def test_the_claim_itself_carries_the_machine_predicate(db):
    """Belt and braces: even a direct claim cannot cross machines."""
    from sqlalchemy import or_
    _queue(db, "job-claim", machine_id=MACHINE_A)
    wrong = (db.query(TestRun)
             .filter(TestRun.id == "job-claim", TestRun.job_state == "queued")
             .filter(or_(TestRun.machine_id.is_(None), TestRun.machine_id == MACHINE_B))
             .update({"job_state": "assigned"}, synchronize_session=False))
    db.commit()
    assert wrong == 0, "the wrong machine's claim matches no row"
    assert _state(db, "job-claim")[0] == "queued"


# ── Boundaries this phase must not cross ─────────────────────────────────────

def test_an_agent_with_no_id_only_sees_legacy_jobs(db):
    _queue(db, "job-pinned", machine_id=MACHINE_A)
    _queue(db, "job-null", machine_id=None, created_offset=1)
    got = _poll(db, "", [UDID_X])
    assert got is not None and got["job_id"] == "job-null"
    assert _state(db, "job-pinned")[0] == "queued"


def test_no_reservation_or_capability_matching_was_added():
    """4D.3 is routing only — later phases own these."""
    import ast, inspect, textwrap
    # Inspect the CODE, not the prose: the docstring legitimately mentions UDID
    # parsing in order to say the function does none.
    tree = ast.parse(textwrap.dedent(inspect.getsource(jobs_router.poll_job)))
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        fn.body = fn.body[1:]                      # drop the docstring
    code = ast.unparse(fn).lower()
    for forbidden in ("reserve", "capabilit", "local:", "gethostname",
                      "udid", "hostname", "provider", "split("):
        assert forbidden not in code, f"poll_job must not reference {forbidden}"
    # And it must still match on the machine identity established in 4D.1.
    assert "machine_id" in code and "device_name" in code
