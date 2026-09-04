"""Phase 4B safety fixes, ahead of a second Mac joining the platform.

Covers the four findings from the Phase 4A portability audit:
  1. virtual fallback device ids collided across machines
  2. agent endpoints were open when AGENT_TOKEN was unset and the backend
     is served with --host 0.0.0.0
  3. a job whose agent died stayed 'running' forever (one had since 22 Jul)
  4. devices did not say which machine owned them
"""
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException


# ── Fix 1: unique virtual device ids ─────────────────────────────────────────

from automation.agent import main as agent_main


def test_virtual_device_id_is_host_specific():
    dev = agent_main.get_virtual_device()
    assert dev["id"] != "virtual-ios-simulator", "the colliding constant is gone"
    assert dev["id"].startswith("virtual-")
    assert agent_main.agent_slug() in dev["id"]


def test_two_machines_cannot_produce_the_same_virtual_id(monkeypatch):
    """The collision the audit found: Mac A and Mac B both advertising one id."""
    ids = []
    for host in ("mac-a.local", "mac-b.local"):
        monkeypatch.setattr(agent_main.socket, "gethostname", lambda h=host: h)
        ids.append(agent_main.get_virtual_device()["id"])
    assert ids[0] != ids[1]
    assert len(set(ids)) == 2


def test_virtual_id_is_stable_across_restarts(monkeypatch):
    """Same machine must keep the same id — no per-heartbeat randomness."""
    monkeypatch.setattr(agent_main.socket, "gethostname", lambda: "mac-a.local")
    ids = {agent_main.get_virtual_device()["id"] for _ in range(5)}
    assert len(ids) == 1


def test_virtual_id_slug_is_url_and_path_safe(monkeypatch):
    monkeypatch.setattr(agent_main.socket, "gethostname", lambda: "Bobs MacBook Pro (2).local")
    slug = agent_main.agent_slug()
    assert slug == "bobs-macbook-pro-2", slug
    assert all(c.isalnum() or c == "-" for c in slug)


def test_virtual_device_template_is_not_mutated(monkeypatch):
    """get_virtual_device returns a copy; the module constant stays clean."""
    monkeypatch.setattr(agent_main.socket, "gethostname", lambda: "mac-a")
    agent_main.get_virtual_device()
    assert agent_main.VIRTUAL_IOS_DEVICE["id"] == "virtual-ios-simulator"
    assert agent_main.VIRTUAL_ANDROID_DEVICE["id"] == "virtual-android-emulator"


def test_real_device_discovery_is_untouched(monkeypatch):
    """The virtual device only appears when NO real device is found."""
    class FakeProvider:
        def get_provider_name(self): return "fake"
        def discover_devices(self):
            class D:
                id, name, manufacturer, model = "REAL-UDID", "iPhone", "Apple", "iPhone"
                platform, platform_version = "iOS", "18.3"
            return [D()]
    monkeypatch.setattr(agent_main, "AndroidDiscoveryProvider", FakeProvider)
    monkeypatch.setattr(agent_main, "IOSDiscoveryProvider", FakeProvider)
    ids = [d["id"] for d in agent_main.discover_devices()]
    assert ids == ["REAL-UDID", "REAL-UDID"]
    assert not any("virtual" in i for i in ids)


# ── Fix 2: agent auth required off-loopback ──────────────────────────────────

from automation.auth import security


class _Req:
    """Minimal stand-in for a Starlette Request's client address."""
    def __init__(self, host):
        self.client = type("C", (), {"host": host})() if host is not None else None


@pytest.fixture
def no_token(monkeypatch):
    monkeypatch.setattr(security, "AGENT_TOKEN", "")
    monkeypatch.setattr(security, "IS_PRODUCTION", False)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"])
def test_local_development_still_works_without_a_token(no_token, host):
    """Existing localhost workflows must be unaffected."""
    assert security.require_agent(_Req(host), "") == "agent"


@pytest.mark.parametrize("host", ["192.168.1.42", "10.0.0.7", "203.0.113.9"])
def test_lan_caller_without_a_token_is_rejected(no_token, host):
    with pytest.raises(HTTPException) as e:
        security.require_agent(_Req(host), "")
    assert e.value.status_code == 503
    assert "AGENT_TOKEN" in e.value.detail
    assert "localhost" in e.value.detail, "the message must say what to configure"


def test_unknown_caller_address_is_treated_as_remote(no_token):
    """An address we cannot read is not evidence of being local."""
    with pytest.raises(HTTPException):
        security.require_agent(_Req(None), "")
    with pytest.raises(HTTPException):
        security.require_agent(None, "")


def test_configured_token_is_enforced_everywhere(monkeypatch):
    monkeypatch.setattr(security, "AGENT_TOKEN", "s3cret")
    monkeypatch.setattr(security, "IS_PRODUCTION", False)
    assert security.require_agent(_Req("192.168.1.42"), "s3cret") == "agent"
    assert security.require_agent(_Req("127.0.0.1"), "s3cret") == "agent"
    for bad in ("", "wrong"):
        for host in ("127.0.0.1", "192.168.1.42"):
            with pytest.raises(HTTPException) as e:
                security.require_agent(_Req(host), bad)
            assert e.value.status_code == 401


def test_production_without_a_token_still_fails_closed(monkeypatch):
    monkeypatch.setattr(security, "AGENT_TOKEN", "")
    monkeypatch.setattr(security, "IS_PRODUCTION", True)
    with pytest.raises(HTTPException) as e:
        security.require_agent(_Req("127.0.0.1"), "")
    assert e.value.status_code == 503


def test_agent_endpoints_all_use_the_guard():
    """register / heartbeat / poll / status / evidence must be behind require_agent."""
    import inspect
    from automation.api.v1.routers import agents as agents_router, jobs as jobs_router
    for mod, names in ((agents_router, ["register_agent", "heartbeat"]),
                       (jobs_router, ["poll_job", "update_job_status", "upload_evidence"])):
        for name in names:
            src = inspect.getsource(getattr(mod, name))
            assert "require_agent" in src, f"{name} is not guarded"


# ── Fix 3: reclaim stranded jobs ─────────────────────────────────────────────

from automation.ops import monitor as ops_monitor
from automation.database.models import Base, TestRun, ExecutionAgent, SystemAlert


@pytest.fixture
def db(monkeypatch, tmp_path):
    """An isolated sqlite database — never the real one."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(f"sqlite:///{tmp_path/'ops.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(ops_monitor, "SessionLocal", Session)
    s = Session()
    yield s
    s.close()


def _agent(db, *, status="online", age_seconds=0):
    a = ExecutionAgent(hostname="mac-a", os="Darwin", status=status,
                       last_heartbeat=datetime.utcnow() - timedelta(seconds=age_seconds))
    db.add(a)
    db.commit()
    return a


def _run(db, *, job_state, agent_id=None, attempts=1):
    r = TestRun(id=f"run-{job_state}-{agent_id}-{attempts}-{id(db)}{len(job_state)}",
                test_suite="s", test_name="t", status=job_state,
                job_state=job_state, agent_id=agent_id, attempts=attempts,
                started_at=datetime.utcnow(), created_at=datetime.utcnow())
    db.add(r)
    db.commit()
    return r


def test_healthy_agent_running_job_stays_running(db):
    a = _agent(db, status="online", age_seconds=5)
    r = _run(db, job_state="running", agent_id=a.id)
    ops_monitor.OpsMonitor().reclaim_stranded_jobs()
    db.expire_all()
    assert db.query(TestRun).get(r.id).job_state == "running"
    assert db.query(TestRun).get(r.id).agent_id == a.id


@pytest.mark.parametrize("state", list(ops_monitor.IN_FLIGHT_JOB_STATES))
def test_stale_agent_in_flight_job_is_requeued(db, state):
    a = _agent(db, status="online", age_seconds=600)      # heartbeat long past
    r = _run(db, job_state=state, agent_id=a.id)
    ops_monitor.OpsMonitor().reclaim_stranded_jobs()
    db.expire_all()
    job = db.query(TestRun).get(r.id)
    assert job.job_state == "queued"
    assert job.status == "queued"
    assert job.agent_id is None, "stale ownership must be cleared"
    assert job.attempts == 2
    assert state in (job.error_message or ""), "the transition is auditable"


def test_agent_marked_offline_triggers_reclaim(db):
    a = _agent(db, status="offline", age_seconds=1)
    r = _run(db, job_state="running", agent_id=a.id)
    ops_monitor.OpsMonitor().reclaim_stranded_jobs()
    db.expire_all()
    assert db.query(TestRun).get(r.id).job_state == "queued"


def test_missing_agent_row_triggers_reclaim(db):
    r = _run(db, job_state="running", agent_id="agent-that-no-longer-exists")
    ops_monitor.OpsMonitor().reclaim_stranded_jobs()
    db.expire_all()
    assert db.query(TestRun).get(r.id).job_state == "queued"


def test_backend_in_process_runs_are_never_touched(db):
    """Scenario runs write job_state='running' with no agent — no agent is coming
    back for them, and requeueing one would hand a backend run to the agent."""
    r = _run(db, job_state="running", agent_id=None)
    ops_monitor.OpsMonitor().reclaim_stranded_jobs()
    db.expire_all()
    job = db.query(TestRun).get(r.id)
    assert job.job_state == "running"
    assert job.agent_id is None


@pytest.mark.parametrize("state", ["queued", "passed", "completed", "failed", "cancelled"])
def test_settled_and_queued_jobs_are_unchanged(db, state):
    a = _agent(db, status="offline", age_seconds=600)
    r = _run(db, job_state=state, agent_id=a.id)
    ops_monitor.OpsMonitor().reclaim_stranded_jobs()
    db.expire_all()
    job = db.query(TestRun).get(r.id)
    assert job.job_state == state
    assert job.agent_id == a.id


def test_a_job_that_keeps_stranding_is_abandoned_not_cycled(db):
    """Guard against an infinite requeue loop."""
    a = _agent(db, status="offline", age_seconds=600)
    r = _run(db, job_state="running", agent_id=a.id,
             attempts=ops_monitor.MAX_JOB_ATTEMPTS)
    ops_monitor.OpsMonitor().reclaim_stranded_jobs()
    db.expire_all()
    job = db.query(TestRun).get(r.id)
    assert job.job_state == "failed"
    assert "Abandoned after" in job.error_message


def test_reclaim_writes_an_audit_alert(db):
    a = _agent(db, status="offline", age_seconds=600)
    r = _run(db, job_state="running", agent_id=a.id)
    ops_monitor.OpsMonitor().reclaim_stranded_jobs()
    alerts = db.query(SystemAlert).filter(SystemAlert.type == "job_reclaimed").all()
    assert len(alerts) == 1
    assert r.id in alerts[0].message
    assert a.id in alerts[0].message


def test_reclaimed_job_can_only_be_claimed_by_one_agent(db):
    """poll_job's claim is a conditional UPDATE; two agents racing must not both win."""
    a = _agent(db, status="offline", age_seconds=600)
    r = _run(db, job_state="running", agent_id=a.id)
    ops_monitor.OpsMonitor().reclaim_stranded_jobs()
    db.expire_all()

    def claim(agent_id):
        return db.query(TestRun).filter(
            TestRun.id == r.id, TestRun.job_state == "queued"
        ).update({"job_state": "assigned", "agent_id": agent_id},
                 synchronize_session=False)

    assert claim("agent-1") == 1, "first claim wins"
    db.commit()
    assert claim("agent-2") == 0, "second claim must find nothing to claim"
    db.commit()
    db.expire_all()
    assert db.query(TestRun).get(r.id).agent_id == "agent-1"


def test_reclaim_is_idempotent(db):
    a = _agent(db, status="offline", age_seconds=600)
    r = _run(db, job_state="running", agent_id=a.id)
    first = ops_monitor.OpsMonitor().reclaim_stranded_jobs()
    second = ops_monitor.OpsMonitor().reclaim_stranded_jobs()
    assert len(first) == 1 and second == []
    db.expire_all()
    assert db.query(TestRun).get(r.id).attempts == 2, "not incremented twice"


# ── Fix 4: device API exposes the owning machine ─────────────────────────────

from automation.device_manager.service import DeviceDiscoveryService
from automation.device_manager.models import Device


def test_agent_devices_carry_their_machine_hostname():
    svc = DeviceDiscoveryService()
    svc.sync_agent_devices("agent-b", [{"id": "UDID-B", "name": "iPhone 16 Pro",
                                        "platform": "iOS"}], "mac-b")
    dev = svc.get_device("UDID-B")
    assert dev.hostname == "mac-b"
    assert dev.provider == "agent-b", "provider still carries the agent id"


def test_two_machines_are_distinguishable_in_the_device_list():
    svc = DeviceDiscoveryService()
    svc.sync_agent_devices("agent-a", [{"id": "UDID-A", "platform": "iOS"}], "mac-a")
    svc.sync_agent_devices("agent-b", [{"id": "UDID-B", "platform": "iOS"}], "mac-b")
    owners = {d.id: d.hostname for d in svc.get_all_devices()}
    assert owners == {"UDID-A": "mac-a", "UDID-B": "mac-b"}


def test_backend_local_simulators_report_the_backend_machine():
    svc = DeviceDiscoveryService()
    dev = svc.register_local_device("UDID-LOCAL", "iPhone 16 Pro")
    assert dev.provider == "local"
    assert dev.hostname, "backend-owned simulators must name their machine too"


def test_hostname_is_optional_so_existing_callers_still_work():
    svc = DeviceDiscoveryService()
    svc.sync_agent_devices("agent-x", [{"id": "UDID-X", "platform": "iOS"}])
    assert svc.get_device("UDID-X").hostname is None


def test_device_serialization_includes_hostname():
    """What the device API actually returns."""
    d = Device(id="U", name="iPhone", platform="iOS", provider="local", hostname="mac-a")
    assert d.model_dump()["hostname"] == "mac-a"


# ── Phase 4D.2: reclaim must preserve routing intent ─────────────────────────

def test_reclaim_clears_agent_id_but_preserves_machine_id(db):
    """agent_id is who is executing; machine_id is where the job belongs.

    Clearing both would lose the machine a job was routed to, which is exactly
    what the next phase will match on.
    """
    a = _agent(db, status="offline", age_seconds=600)
    r = _run(db, job_state="running", agent_id=a.id)
    r.machine_id = "machine-Y"
    db.commit()

    ops_monitor.OpsMonitor().reclaim_stranded_jobs()
    db.expire_all()

    job = db.query(TestRun).get(r.id)
    assert job.job_state == "queued"
    assert job.agent_id is None, "worker assignment is released"
    assert job.machine_id == "machine-Y", "routing intent survives the reclaim"


def test_abandoned_job_also_keeps_its_machine_id(db):
    a = _agent(db, status="offline", age_seconds=600)
    r = _run(db, job_state="running", agent_id=a.id,
             attempts=ops_monitor.MAX_JOB_ATTEMPTS)
    r.machine_id = "machine-Y"
    db.commit()

    ops_monitor.OpsMonitor().reclaim_stranded_jobs()
    db.expire_all()

    job = db.query(TestRun).get(r.id)
    assert job.job_state == "failed"
    assert job.machine_id == "machine-Y"


def test_legacy_job_without_machine_id_reclaims_normally(db):
    """Reclaim must not require a machine_id to work."""
    a = _agent(db, status="offline", age_seconds=600)
    r = _run(db, job_state="running", agent_id=a.id)
    assert r.machine_id is None

    ops_monitor.OpsMonitor().reclaim_stranded_jobs()
    db.expire_all()

    job = db.query(TestRun).get(r.id)
    assert job.job_state == "queued" and job.machine_id is None
