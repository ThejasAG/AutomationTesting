"""Live Steps shows what the run is doing while it does it.

Measured 2026-09-29, run 9cfa91da (Preorder -> C-App card -> pay in B-App): the
run started 05:38:06 and segment 1's first row was written 05:39:49. For those
103s the page said "No steps recorded yet" while the devices were visibly
working. The setup's log events were dropped (the runner is started without a
callback), waiter/kitchen segments appeared only after their 20-30s sign-in,
and a long step's own progress lines appeared only once it returned.
"""
import concurrent.futures as fut
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from automation.database.models import Base, ScenarioResult, TestRun
from automation.scenarios import cross_app_flows as caf
from automation.scenarios.cross_app_flows import FlowRunner

PAGE = Path("automation/dashboard/src/pages/RunDetails.tsx").read_text()

FLOW = {"id": "f", "name": "F", "segments": [
    {"num": "1", "name": "C-App: book", "role": "consumer", "steps": ["open app"]},
    {"num": "2", "name": "Waiter: assign", "role": "waiter", "steps": ["@assign_table"]},
]}


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'live.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(caf, "SessionLocal", Session)
    with Session() as s:
        s.add(TestRun(id="run1", status="running", job_state="running"))
        s.commit()
    monkeypatch.setattr(caf, "acquire_devices", lambda *a, **k: True)
    monkeypatch.setattr(caf, "release_devices", lambda run_id: None)
    monkeypatch.setattr(FlowRunner, "_capture_screenshot", lambda self: None)
    monkeypatch.setattr(FlowRunner, "_collect_evidence", lambda self, role: [])
    monkeypatch.setattr(FlowRunner, "_flow_udids", lambda self: [])
    return Session


def rows(Session):
    with Session() as s:
        return {r.scenario_num: (r.status, list(r.reasons or []))
                for r in s.query(ScenarioResult).filter_by(run_id="run1")}


def run_status(Session):
    with Session() as s:
        r = s.query(TestRun).filter_by(id="run1").one()
        return r.status, r.error_message


def test_setup_is_shown_on_segment_one_while_the_devices_get_ready(db, monkeypatch):
    seen = {}

    def preflight(self):
        self._setup_stage("checking the apps are installed")
        self.on_event({"type": "log", "message": "preflight: C-App installed\nmore detail"})
        seen["during"] = rows(db)

    monkeypatch.setattr(FlowRunner, "_preflight", preflight)
    monkeypatch.setattr(FlowRunner, "_run_segment", lambda self, seg: True)
    FlowRunner("run1", FLOW, {}, {}).run()

    during = seen["during"]
    status, notes = during["1"]
    assert status == "running"
    assert "· setup: reserving the simulators" in notes[0]
    assert "· preflight: C-App installed" in notes
    assert notes[-1] == "▶ setup: checking the apps are installed"
    # The whole flow is visible from the start.
    assert during["2"] == ("queued", [])


def test_segment_one_keeps_the_setup_lines_and_shows_its_session_starting(db, monkeypatch):
    seen = []

    def session_for(self, role):
        seen.append(rows(db)["1"])
        raise RuntimeError("WDA did not start")

    monkeypatch.setattr(FlowRunner, "_preflight",
                        lambda self: self._setup_stage("starting the device sessions"))
    monkeypatch.setattr(FlowRunner, "_session_for", session_for)
    FlowRunner("run1", FLOW, {}, {}).run()

    assert seen[0][0] == "running" and seen[0][1][-1] == "▶ starting the consumer session"
    status, notes = rows(db)["1"]
    assert status == "FAIL"
    assert any(n.startswith("· setup: starting the device sessions (") for n in notes)
    assert any("WDA did not start" in n for n in notes)
    assert rows(db)["2"][0] == "SKIPPED"


def test_a_setup_crash_names_the_stage_and_never_started_segments_are_skipped(db, monkeypatch):
    def preflight(self):
        self._setup_stage("checking the apps are installed")
        raise RuntimeError("C-App is not installed")

    monkeypatch.setattr(FlowRunner, "_preflight", preflight)
    FlowRunner("run1", FLOW, {}, {}).run()

    status, notes = rows(db)["1"]
    assert status == "FAIL"
    assert notes[-1].startswith("[fail] setup: checking the apps are installed")
    assert not any(n.startswith("▶") for n in notes)
    assert rows(db)["2"] == ("SKIPPED", ["[skipped] not run — the run ended before "
                                         "this segment started."])
    assert run_status(db)[0] == "failed"


def test_a_run_that_passes_is_still_passed(db, monkeypatch):
    def segment(self, seg):
        self._persist(seg, "PASS", ["[ok] done"], 1.0)
        return True

    monkeypatch.setattr(FlowRunner, "_preflight", lambda self: None)
    monkeypatch.setattr(FlowRunner, "_run_segment", segment)
    FlowRunner("run1", FLOW, {}, {}).run()
    assert {k: v[0] for k, v in rows(db).items()} == {"1": "PASS", "2": "PASS"}
    assert run_status(db)[0] == "passed"


def timeline(Session):
    import json
    with Session() as s:
        return [e["event"] for e in json.loads(s.query(TestRun).filter_by(id="run1").one().timeline)]


def test_the_execution_timeline_is_written_for_flow_runs(db, monkeypatch):
    # It was only written by the remote-agent path: "No timeline data available".
    monkeypatch.setattr(FlowRunner, "_preflight", lambda self: None)
    monkeypatch.setattr(FlowRunner, "_run_segment", lambda self, seg: seg["num"] == "1")
    FlowRunner("run1", FLOW, {}, {}).run()
    events = timeline(db)
    assert events[0] == "Run started" and events[1].startswith("Devices ready (")
    assert events[2] == "Segment 1: C-App: book" and events[3].startswith("Segment 1 passed (")
    assert events[4] == "Segment 2: Waiter: assign" and events[5].startswith("Segment 2 Failed (")
    assert events[-1].startswith("Run Failed (")


def test_a_long_step_shows_its_progress_before_it_returns(monkeypatch):
    runner = FlowRunner.__new__(FlowRunner)
    runner.LIVE_REFRESH = 0.02
    shown = []
    monkeypatch.setattr(FlowRunner, "_persist",
                        lambda self, seg, status, notes, secs, screenshot=None:
                        shown.append((status, list(notes))))
    notes = ["[ok] earlier step"]
    release = threading.Event()

    def step():
        notes.append("    · PennePolloInc → added")
        release.wait(2)
        return True, False

    ex = fut.ThreadPoolExecutor(max_workers=1)
    f = ex.submit(step)
    t = threading.Thread(target=lambda: (time.sleep(0.2), release.set()))
    t.start()
    assert runner._await_step(f, FLOW["segments"][0], "@add_all_products",
                              notes, 1, time.time()) == (True, False)
    ex.shutdown()
    assert ("running", ["[ok] earlier step", "    · PennePolloInc → added",
                        "▶ @add_all_products"]) in shown


def test_a_hung_step_still_times_out(monkeypatch):
    runner = FlowRunner.__new__(FlowRunner)
    runner.LIVE_REFRESH = 0.02
    monkeypatch.setattr(caf, "STEP_TIMEOUT", 0.1)
    ex = fut.ThreadPoolExecutor(max_workers=1)
    gate = threading.Event()
    f = ex.submit(gate.wait, 2)
    with pytest.raises(fut.TimeoutError):
        runner._await_step(f, FLOW["segments"][0], "hang", [], 0, time.time())
    gate.set()
    ex.shutdown(wait=False)


# -- the page -------------------------------------------------------------------

def test_the_page_spins_on_the_running_segment_not_the_last_row():
    assert "i === scenarios.length - 1 && !done" not in PAGE
    assert "const running = isActive && i === inFlight && !done;" in PAGE
    assert "queued ? 'queued'" in PAGE
