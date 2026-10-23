"""Phase 4F.1 — the backend derives agent identity from a credential, not a claim.

Before this, every agent shared one AGENT_TOKEN and announced its own identity in
the request body, so any holder of that token could act as any machine. AGENT_TOKEN
is now only a BOOTSTRAP credential — it proves a caller may register at all —
while registration issues a per-agent secret that answers "which machine is this?".
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect as sa_inspect, text
from sqlalchemy.orm import sessionmaker

from automation.api.v1.routers import agents as agents_router
from automation.api.v1.routers import jobs as jobs_router
from automation.auth import security
from automation.database.models import Base, ExecutionAgent, TestProject, TestRun


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'auth.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    import automation.database.config as cfg
    monkeypatch.setattr(cfg, "SessionLocal", Session)
    import automation.device_manager.service as dm
    monkeypatch.setattr(dm, "_backend_machine_id", None)
    s = Session()
    s.add(TestProject(id="proj-1", name="Demo", git_url="https://example.com/x.git"))
    s.commit()
    yield s
    s.close()


def _register(db, hostname, os_name="Darwin"):
    """Register a machine and return (agent_id, credential)."""
    out = agents_router.register_agent(
        agents_router.RegisterAgentRequest(hostname=hostname, os=os_name,
                                           capabilities={}, connected_devices=[]),
        db=db)
    return out["id"], out["agent_credential"]


def _auth(db, credential):
    """Resolve a credential to an agent exactly as a request would."""
    return security.authenticated_agent(request=None, x_agent_credential=credential, db=db)


# ── 1-2. Each agent authenticates as itself ──────────────────────────────────

def test_01_agent_a_authenticates(db):
    a_id, a_cred = _register(db, "mac-a")
    assert a_cred and len(a_cred) > 30
    assert _auth(db, a_cred).id == a_id


def test_02_agent_b_authenticates_independently(db):
    a_id, a_cred = _register(db, "mac-a")
    b_id, b_cred = _register(db, "mac-b")
    assert a_id != b_id and a_cred != b_cred
    assert _auth(db, a_cred).id == a_id
    assert _auth(db, b_cred).id == b_id


def test_09_two_machines_stay_independently_identifiable(db):
    a_id, a_cred = _register(db, "mac-a")
    b_id, b_cred = _register(db, "mac-b")
    assert {_auth(db, a_cred).hostname, _auth(db, b_cred).hostname} == {"mac-a", "mac-b"}


# ── 3-4. Impersonation is impossible ─────────────────────────────────────────

def test_03_agent_a_cannot_authenticate_as_agent_b(db):
    a_id, a_cred = _register(db, "mac-a")
    b_id, b_cred = _register(db, "mac-b")
    assert _auth(db, a_cred).id != b_id, "A's credential must never resolve to B"


def test_04_a_forged_agent_id_does_not_change_the_authenticated_identity(db):
    """credential A + agent_id B  ->  still agent A."""
    a_id, a_cred = _register(db, "mac-a")
    b_id, _ = _register(db, "mac-b")

    identity = _auth(db, a_cred)
    assert identity.id == a_id

    # A presenting its own credential but naming B is refused outright.
    with pytest.raises(HTTPException) as e:
        security.assert_agent_identity(identity, b_id)
    assert e.value.status_code == 403
    # Naming itself is fine.
    security.assert_agent_identity(identity, a_id)


def test_04b_poll_job_refuses_a_mismatched_claim(db):
    """The real endpoint: A cannot poll for B's jobs."""
    a_id, a_cred = _register(db, "mac-a")
    b_id, _ = _register(db, "mac-b")
    db.add(TestRun(id="job-b", project_id="proj-1", test_suite="s", test_name="t",
                   status="queued", job_state="queued", device_name="UDID-X",
                   machine_id=b_id))
    db.commit()

    req = jobs_router.PollJobRequest(agent_id=b_id, connected_devices=["UDID-X"])
    with pytest.raises(HTTPException) as e:
        jobs_router.poll_job(req, db=db, _agent="agent", authenticated=_auth(db, a_cred))
    assert e.value.status_code == 403

    db.expire_all()
    assert db.query(TestRun).get("job-b").job_state == "queued", "B's job is untouched"


def test_04c_polling_as_yourself_still_works(db):
    a_id, a_cred = _register(db, "mac-a")
    db.add(TestRun(id="job-a", project_id="proj-1", test_suite="s", test_name="t",
                   status="queued", job_state="queued", device_name="UDID-X",
                   machine_id=a_id))
    db.commit()
    req = jobs_router.PollJobRequest(agent_id=a_id, connected_devices=["UDID-X"])
    got = jobs_router.poll_job(req, db=db, _agent="agent", authenticated=_auth(db, a_cred))
    assert got["job_id"] == "job-a"


def test_heartbeat_refuses_a_mismatched_agent(db):
    a_id, a_cred = _register(db, "mac-a")
    b_id, _ = _register(db, "mac-b")
    req = agents_router.HeartbeatRequest(status="online", connected_devices=[])
    with pytest.raises(HTTPException) as e:
        agents_router.heartbeat(b_id, req, db=db, _agent="agent",
                                authenticated=_auth(db, a_cred))
    assert e.value.status_code == 403
    # As itself: fine.
    assert agents_router.heartbeat(a_id, req, db=db, _agent="agent",
                                   authenticated=_auth(db, a_cred))["status"] == "ok"


# ── 5-6. Bad and missing credentials ─────────────────────────────────────────

@pytest.mark.parametrize("bad", ["", "   ", "not-a-real-credential", "x" * 43])
def test_05_invalid_or_missing_credentials_resolve_to_nobody(db, bad):
    _register(db, "mac-a")
    assert _auth(db, bad) is None


def test_06_require_authenticated_agent_rejects_an_unproven_caller(db):
    with pytest.raises(HTTPException) as e:
        security.require_authenticated_agent(agent=None)
    assert e.value.status_code == 401
    assert "credential" in e.value.detail.lower()

    a_id, a_cred = _register(db, "mac-a")
    assert security.require_authenticated_agent(agent=_auth(db, a_cred)).id == a_id


# ── 7. Local development still works ─────────────────────────────────────────

def test_07_an_uncredentialed_caller_keeps_the_existing_behaviour(db):
    """No credential presented: identity is unproven, and claims are left alone —
    which is the unchanged single-machine workflow, not a bypass."""
    a_id, _ = _register(db, "mac-a")
    assert _auth(db, "") is None
    security.assert_agent_identity(None, a_id)      # no credential -> no assertion
    security.assert_agent_identity(None, "anything-at-all")


def test_07b_bootstrap_token_still_guards_registration(monkeypatch):
    """AGENT_TOKEN is unchanged as the bootstrap credential."""
    monkeypatch.setattr(security, "AGENT_TOKEN", "boot")
    monkeypatch.setattr(security, "IS_PRODUCTION", False)

    class _Req:
        client = type("C", (), {"host": "192.168.1.9"})()

    assert security.require_agent(_Req(), "boot") == "agent"
    with pytest.raises(HTTPException) as e:
        security.require_agent(_Req(), "wrong")
    assert e.value.status_code == 401


# ── 8. Restart ───────────────────────────────────────────────────────────────

def test_08_restart_keeps_the_same_agent_and_reissues_a_credential(db):
    """Phase 4D.1's row reuse is preserved; the credential rotates."""
    a_id, first = _register(db, "mac-a")
    again_id, second = _register(db, "mac-a")

    assert again_id == a_id, "same machine keeps its identity across restarts"
    assert db.query(ExecutionAgent).count() == 1, "no new row per restart"
    assert second != first, "a fresh secret is issued"
    assert _auth(db, second).id == a_id
    assert _auth(db, first) is None, "the superseded credential stops working"


def test_08b_normalized_hostname_still_resolves_to_one_machine(db):
    a_id, _ = _register(db, "Admins-MacBook-Pro.local")
    b_id, cred = _register(db, "Admins-MacBook-Pro")
    assert a_id == b_id
    assert _auth(db, cred).id == a_id


# ── 10. /me semantics ────────────────────────────────────────────────────────

def test_10_me_resolves_to_the_credential_not_the_request(db):
    """What a future /agents/me/... endpoint will depend on."""
    a_id, a_cred = _register(db, "mac-a")
    b_id, b_cred = _register(db, "mac-b")

    me = security.require_authenticated_agent(agent=_auth(db, a_cred))
    assert me.id == a_id
    assert me.hostname == "mac-a"
    # The machine identity for a future ownership check comes from the same object.
    assert me.id != b_id


# ── Storage hygiene ──────────────────────────────────────────────────────────

def test_only_the_hash_is_stored(db):
    a_id, cred = _register(db, "mac-a")
    row = db.query(ExecutionAgent).filter(ExecutionAgent.id == a_id).one()
    assert row.agent_credential_hash and len(row.agent_credential_hash) == 64
    assert cred not in row.agent_credential_hash
    assert row.agent_credential_hash != cred, "the raw secret is never persisted"
    assert row.agent_credential_issued_at is not None


def test_credentials_are_unguessable_and_unique():
    seen = {security.issue_agent_credential()[0] for _ in range(50)}
    assert len(seen) == 50


def test_the_columns_are_added_to_an_existing_database(tmp_path, monkeypatch):
    import automation.database.config as cfg
    engine = create_engine(f"sqlite:///{tmp_path/'old.db'}",
                           connect_args={"check_same_thread": False})
    with engine.begin() as c:
        c.execute(text("CREATE TABLE execution_agents (id VARCHAR(36) PRIMARY KEY, "
                       "hostname VARCHAR(255), os VARCHAR(50), status VARCHAR(50))"))
        c.execute(text("INSERT INTO execution_agents VALUES ('a1','mac-a','Darwin','online')"))
    monkeypatch.setattr(cfg, "engine", engine)
    monkeypatch.setattr(cfg, "SessionLocal", sessionmaker(bind=engine))
    cfg._add_missing_columns()

    cols = {c["name"] for c in sa_inspect(engine).get_columns("execution_agents")}
    assert {"agent_credential_hash", "agent_credential_issued_at"} <= cols
    row = engine.connect().execute(text(
        "SELECT hostname, agent_credential_hash FROM execution_agents WHERE id='a1'")).one()
    assert row == ("mac-a", None), "existing agents keep their data, uncredentialed"


# ── Boundary ─────────────────────────────────────────────────────────────────

def test_me_endpoints_use_the_authenticated_agent():
    """Phase 4F.2A added /agents/me/... — every one must derive the machine from
    the authenticated agent, never from a client field. (This asserted the
    endpoints did not exist yet, before 4F.2A.)"""
    from automation.api.v1.routers import agents
    import inspect
    src = inspect.getsource(agents)
    assert "/me/devices/" in src
    for fn in ("get_my_device", "reserve_my_device", "activate_my_reservation",
               "release_my_reservation", "allocate_my_wda_port"):
        assert "require_authenticated_agent" in inspect.getsource(getattr(agents, fn)), \
            f"{fn} must depend on the authenticated agent"
