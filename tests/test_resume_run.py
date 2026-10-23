"""Resume a failed cross-app flow from the segment that failed.

Retry starts a flow from segment 1: a failure in the last segment (the B-App
payment, measured 2026-09-29) meant ~8 minutes of booking, assigning and kitchen
work again before the broken step was even reached. Resume starts at the first
segment that did not pass, carries the passed ones over, and hands on the one
piece of state later segments need: the slot the diner booked.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from automation.database.models import Base, ScenarioResult, TestRun
from automation.scenarios import cross_app_flows as caf
from automation.scenarios.cross_app_flows import FlowRunner, resume_plan

FLOW = {"id": "f", "name": "F", "segments": [
    {"num": "1", "name": "C-App: book", "role": "consumer", "steps": ["open app"]},
    {"num": "2", "name": "Waiter: assign", "role": "waiter", "steps": ["@open_reservation"]},
    {"num": "3", "name": "Kitchen: ready", "role": "kitchen", "steps": ["@kitchen_ready"]},
    {"num": "4", "name": "Waiter: pay", "role": "waiter", "steps": ["@pay:epay"]},
]}


def row(num, status, reasons=(), secs=10.0):
    return SimpleNamespace(scenario_num=num, status=status, reasons=list(reasons),
                           launch_time=secs)


FAILED_AT_4 = [
    row("1", "PASS", ["[ok] @first_time_slot — selected slot '12:50' (idb) (13.9s)"], 113.9),
    row("2", "PASS", ["    · booked slot '12:50' → target 12:00 row", "[ok] @assign_table"], 250.5),
    row("3", "PASS", ["[ok] @kitchen_ready"], 101.2),
    row("4", "FAIL", ["[FAIL] @pay:epay — payment method not found"], 227.4),
]


# -- where to resume ---------------------------------------------------------------

def test_resumes_at_the_first_segment_that_did_not_pass():
    plan = resume_plan(FLOW, FAILED_AT_4)
    assert plan["start_at"] == 3 and plan["booked_slot"] == "12:50"
    assert set(plan["carried"]) == {"1", "2", "3"}
    assert plan["carried"]["2"]["launch_time"] == 250.5


def test_skipped_segments_after_a_failure_are_run_too():
    rows = FAILED_AT_4[:1] + [row("2", "FAIL"), row("3", "SKIPPED"), row("4", "SKIPPED")]
    assert resume_plan(FLOW, rows)["start_at"] == 1


def test_a_failure_in_segment_one_resumes_from_the_start():
    plan = resume_plan(FLOW, [row("1", "FAIL")])
    assert plan["start_at"] == 0 and plan["booked_slot"] == "" and plan["carried"] == {}


def test_a_run_that_passed_has_nothing_to_resume():
    with pytest.raises(ValueError, match="nothing to resume"):
        resume_plan(FLOW, [row(n, "PASS") for n in "1234"])


# -- the resumed run -----------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'resume.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(caf, "SessionLocal", Session)
    import automation.database.config as cfg
    monkeypatch.setattr(cfg, "SessionLocal", Session)
    with Session() as s:
        s.add(TestRun(id="run2", status="running", job_state="running"))
        s.commit()
    monkeypatch.setattr(caf, "acquire_devices", lambda *a, **k: True)
    monkeypatch.setattr(caf, "release_devices", lambda run_id: None)
    monkeypatch.setattr(FlowRunner, "_flow_udids", lambda self: [])
    monkeypatch.setattr(FlowRunner, "_preflight", lambda self: None)
    return Session


def test_only_the_remaining_segments_run_and_the_slot_is_handed_on(db, monkeypatch):
    ran, slots = [], []

    def segment(self, seg):
        ran.append(seg["num"])
        slots.append(getattr(self, "_booked_slot", ""))
        self._persist(seg, "PASS", ["[ok] paid"], 5.0)
        return True

    monkeypatch.setattr(FlowRunner, "_run_segment", segment)
    plan = dict(resume_plan(FLOW, FAILED_AT_4), from_run="abcdef12-old")
    FlowRunner("run2", FLOW, {}, {}, resume=plan).run()

    assert ran == ["4"] and slots == ["12:50"]
    with db() as s:
        rows = {r.scenario_num: r for r in s.query(ScenarioResult).filter_by(run_id="run2")}
        run = s.query(TestRun).filter_by(id="run2").one()
    assert {k: r.status for k, r in rows.items()} == {"1": "PASS", "2": "PASS",
                                                     "3": "PASS", "4": "PASS"}
    first = rows["2"].reasons
    assert first[0].startswith("[ok] carried over — passed in run abcdef12")
    assert "[ok] @assign_table" in first and rows["2"].launch_time == 250.5
    assert run.status == "passed"
    assert "Resumed from segment 4 of run abcdef12 (booked slot 12:50)" in run.timeline


def test_a_resumed_segment_that_fails_again_fails_the_run(db, monkeypatch):
    def segment(self, seg):
        self._persist(seg, "FAIL", ["[FAIL] still broken"], 5.0)
        return False

    monkeypatch.setattr(FlowRunner, "_run_segment", segment)
    FlowRunner("run2", FLOW, {}, {},
               resume=dict(resume_plan(FLOW, FAILED_AT_4), from_run="old")).run()
    with db() as s:
        assert s.query(TestRun).filter_by(id="run2").one().status == "failed"


# -- the endpoint --------------------------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    from automation.api.v1.routers import jobs
    engine = create_engine(f"sqlite:///{tmp_path / 'api.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(jobs, "SessionLocal", Session)
    import automation.database.config as cfg
    monkeypatch.setattr(cfg, "SessionLocal", Session)
    flow = next(f for f in caf.list_flows() if len(f["segments"]) >= 4)
    started = {}
    monkeypatch.setattr(caf, "start_flow_run",
                        lambda fid, env="prod", business_device="tablet", resume=None:
                        started.update(fid=fid, env=env, resume=resume) or "new-run")

    def make(status="failed", seg_status=("PASS", "PASS", "PASS", "FAIL")):
        with Session() as s:
            s.add(TestRun(id="old", status=status, test_name=f"[Staging] {flow['name']}",
                          test_suite="Cross-app flows (iOS · Staging)",
                          device_name="C:D03927 B:6767DB", bot_type="ios-crossapp-flow"))
            for seg, st in zip(flow["segments"], seg_status):
                s.add(ScenarioResult(run_id="old", scenario_num=seg["num"],
                                     scenario_name=seg["name"], status=st,
                                     reasons=["[ok] selected slot '13:15'"]))
            s.commit()
        return flow, started
    return jobs, make


def test_the_endpoint_resumes_from_the_failed_segment(api):
    jobs, make = api
    flow, started = make()
    out = jobs.resume_run("old", current_user=None)
    assert out["run_id"] == "new-run" and out["resumed_from"] == "old"
    assert out["from_segment"] == flow["segments"][3]["num"]
    assert started["env"] == "staging" and started["fid"] == flow["id"]
    assert started["resume"]["start_at"] == 3 and started["resume"]["booked_slot"] == "13:15"
    assert started["resume"]["from_run"] == "old"


def test_a_run_still_going_cannot_be_resumed(api):
    jobs, make = api
    make(status="running")
    with pytest.raises(HTTPException) as e:
        jobs.resume_run("old", current_user=None)
    assert e.value.status_code == 409


def test_a_passed_run_is_refused_with_the_reason(api):
    jobs, make = api
    make(status="passed", seg_status=("PASS",) * 4)
    with pytest.raises(HTTPException) as e:
        jobs.resume_run("old", current_user=None)
    assert e.value.status_code == 400 and "nothing to resume" in e.value.detail


# -- the page ------------------------------------------------------------------------

def test_the_page_has_a_separate_resume_button_next_to_retry():
    from pathlib import Path
    page = Path("automation/dashboard/src/pages/RunDetails.tsx").read_text()
    assert "Resume from failure" in page and "onClick={onResume}" in page
    assert "Retry scenario" in page and "onClick={onRetry}" in page     # still there
    assert "RESUMABLE.has(run.status) && run.bot_type === 'ios-crossapp-flow'" in page


# -- a resumed segment meets a booking that is further along -------------------------

def test_steps_a_payment_booking_has_done_are_skipped():
    runner = FlowRunner.__new__(FlowRunner)
    runner._remember_status("4954  PAYMENT 17:15 - 18:15 I4 17:29")
    for step in ("@select_all_items", "@serve_items", "@comp_item", "@notify_payment"):
        notes = []
        assert FlowRunner._handle_special(runner, None, step, notes)
        assert notes[-1].startswith(f"[skip] {step} — the booking is already at PAYMENT")


def test_a_serve_booking_is_not_skipped(monkeypatch):
    runner = FlowRunner.__new__(FlowRunner)
    runner._remember_status("4954 SERVE 17:15 - 18:15")
    monkeypatch.setattr(FlowRunner, "_select_all_items", lambda self, notes: "ran")
    assert FlowRunner._handle_special(runner, None, "@select_all_items", []) == "ran"
    assert FlowRunner._PLAIN_STEP_TOKENS["click serveItemsBtn"] == "@serve_items"
