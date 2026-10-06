"""Phase 4F.4a — the agent's planned scenarios use the job's TestRun.

Before: every planned scenario called the run-start bookkeeping, minting a SECOND
TestRun (triggered_by="scenarios-tab") from inside the agent process, and
run-finish then wrote status/duration straight to the database —
from the agent, and once per scenario, against a job that was still running.

After: the agent passes run_id=job_id. scenario_events() reuses that identity,
creates nothing, finalizes nothing, and finish is reported over the existing
authenticated status API. Backend-local callers (Scenarios tab, tickets,
workflow) pass no run_id and keep their own TestRun exactly as before.
"""
import ast
import inspect
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import automation.scenarios.service as sc
from automation.agent import main as agent_main
from automation.scenarios import run_records
from automation.api.v1.routers.jobs import JobStatusRequest
from automation.database.models import Base, TestProject, TestRun


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'p.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    import automation.database.config as cfg
    monkeypatch.setattr(cfg, "SessionLocal", Session)
    db = Session()
    db.add(TestProject(id="proj-1", name="Demo", git_url="https://example.com/x.git",
                       app_bundle_id="com.example.app"))
    db.commit()
    yield db
    db.close()


# ── the agent hands its own identity down ──────────────────────────────────

def test_agent_passes_the_job_id_as_the_run_id(env, monkeypatch):
    seen = {}
    monkeypatch.setattr(sc, "run_scenario_headless",
                        lambda req, db=None, run_id=None: seen.update(run_id=run_id)
                        or {"ok": True, "steps": [], "error": None})
    monkeypatch.setattr(agent_main, "report_scenario_results", lambda r, res: len(res))

    agent_main._run_planned_scenarios(
        "job-abc", "proj-1", "UDID-X", [{"name": "Login", "steps": ["tap Login"]}],
        app_bundle_id="com.example.app")

    assert seen["run_id"] == "job-abc"


def test_scenario_results_are_reported_against_that_same_id(env, monkeypatch):
    calls = []
    monkeypatch.setattr(sc, "run_scenario_headless",
                        lambda req, db=None, run_id=None: {"ok": True, "steps": [], "error": None})
    monkeypatch.setattr(agent_main, "report_scenario_results",
                        lambda r, res: calls.append(r) or len(res))

    agent_main._run_planned_scenarios(
        "job-abc", "proj-1", "UDID-X", [{"name": "Login", "steps": ["tap Login"]}],
        app_bundle_id="com.example.app")

    assert calls == ["job-abc"], "steps must land on the job's run, not a second one"


# ── a supplied run id suppresses BOTH halves of the bookkeeping ────────────

def _drain(gen):
    """Consume the stream and return the event types it emitted."""
    seen = []
    try:
        for frame in gen:
            if frame.startswith("data: "):
                seen.append(json.loads(frame[6:]).get("type"))
    except Exception:
        pass
    return seen


def test_a_supplied_run_id_creates_no_second_testrun(env, monkeypatch):
    """Real path: scenario_events() itself, with the injected store trapped."""
    finished = []

    class _Store:
        step = staticmethod(run_records.step)
        start = staticmethod(lambda req: pytest.fail("minted a second TestRun"))
        finish = staticmethod(lambda rid, *a, **k: finished.append(rid))

    monkeypatch.setattr(sc, "_run_store", _Store)

    req = sc.ScenarioRequest(project_id="proj-1", steps=["tap Login"], device_id="UDID-X",
                             name="Login", save=False, prepare=False,
                             bundle_id="com.example.app")
    seen = _drain(sc.scenario_events(req, "com.example.app", ["tap Login"],
                                     str(env.bind.url), run_id="job-abc"))

    assert "error" in seen, "the boot failure should still have reached the finish path"
    assert finished == [], (
        "with a caller-owned run id nothing may be finalized here — the caller "
        f"reports finish over the status API; got {finished}")


def test_backend_callers_still_get_their_own_testrun(env, monkeypatch):
    """The Scenarios tab has no run record of its own; it must keep creating one."""
    started, finished = [], []

    class _Store:
        step = staticmethod(run_records.step)
        start = staticmethod(lambda req: started.append(req) or "tr-1")
        finish = staticmethod(lambda rid, *a, **k: finished.append(rid))

    monkeypatch.setattr(sc, "_run_store", _Store)

    req = sc.ScenarioRequest(project_id="proj-1", steps=["tap Login"], device_id="UDID-X",
                             name="Login", save=False, prepare=False,
                             bundle_id="com.example.app")
    seen = _drain(sc.scenario_events(req, "com.example.app", ["tap Login"], str(env.bind.url)))

    assert "run" in seen, "the backend caller must still be told its run id"
    assert len(started) == 1, "backend-local behaviour must be unchanged"
    assert finished == ["tr-1"], "and it must still finalize the run it created"


def test_the_run_bookkeeping_is_not_deleted():
    """4F.4a keeps it, 4F.4 only moved it — backend callers depend on it."""
    assert callable(run_records.start) and callable(run_records.finish)
    assert sc._run_store is run_records, "importing run_records must register the store"


# ── crash_detected: the only finish field the status API could not carry ───

def test_status_api_carries_every_field_persist_run_finish_writes():
    """The audit's conclusion, pinned. No lifecycle API was needed.

    status / job_state / completed_at / duration_ms / error_message were already
    handled by POST /jobs/{job_id}/status (the last two server-computed); only
    crash_detected had to be added.
    """
    fields = set(JobStatusRequest.model_fields)
    assert {"status", "error_message", "crash_detected"} <= fields

    from automation.api.v1.routers import jobs
    src = inspect.getsource(jobs.update_job_status)
    for written in ("job.job_state", "job.completed_at", "job.duration_ms",
                    "job.error_message", "job.crash_detected"):
        assert written in src, f"the status endpoint no longer writes {written}"


def test_a_crash_reaches_the_status_api_instead_of_the_database(env, monkeypatch):
    """The agent is the only process that can see the app crash. It must say so."""
    monkeypatch.setattr(sc, "run_scenario_headless",
                        lambda req, db=None, run_id=None: {"ok": False, "steps": [],
                                                           "error": "app crashed",
                                                           "crash_detected": True})
    monkeypatch.setattr(agent_main, "report_scenario_results", lambda r, res: len(res))

    out = agent_main._run_planned_scenarios(
        "job-abc", "proj-1", "UDID-X", [{"name": "Login", "steps": ["tap Login"]}],
        app_bundle_id="com.example.app")

    assert out["crash_detected"] is True

    posted = {}
    monkeypatch.setattr(agent_main.HTTP, "post",
                        lambda url, json=None, timeout=None: posted.update(json or {}))
    agent_main.report_status("job-abc", "failed", crash_detected=out["crash_detected"])
    assert posted["crash_detected"] is True


def test_the_done_event_reports_the_crash_verdict():
    """run_scenario_headless() can only surface it if scenario_events emits it."""
    src = inspect.getsource(sc.scenario_events)
    tree = ast.parse(src.lstrip())
    keys = {k.value for n in ast.walk(tree) if isinstance(n, ast.Dict)
            for k in n.keys if isinstance(k, ast.Constant)}
    assert "crash_detected" in keys, "the done event dropped the crash verdict"
