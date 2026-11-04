"""A booking the app will not open yet stops the run with the time to come back.

The waiter's app opens a booking only from 30 minutes before its start
(BookingCard.onPress); earlier, it shows "The appointment should only be
clickable before 30 minutes of its start time." and stays on the board. The
run used to keep searching until the step timed out and called it a failure.
"""
import types

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from automation.database.models import Base, ScenarioResult, TestRun
from automation.scenarios import cross_app_flows as caf
from automation.scenarios.cross_app_flows import FlowRunner, resume_plan

TOAST = "The appointment should only be clickable before 30 minutes of its start time."


def test_the_toast_stops_the_open_and_says_when_to_come_back(monkeypatch):
    runner = FlowRunner.__new__(FlowRunner)
    runner._booked_ticket = "4905"
    monkeypatch.setattr(caf.time, "sleep", lambda s: None)
    monkeypatch.setattr(runner, "_idb_els",
                        lambda: [{"id": "", "label": TOAST, "cy": 0}], raising=False)
    notes = []
    assert not runner._reservation_opened(notes, "@open_reservation", "12:00", "My Orders panel")
    assert runner._too_early.startswith("booking 4905 at 12:00 can only be opened from 11:30")
    assert "come back after 11:30 and press Resume from failure" in runner._too_early
    assert notes[-1].startswith("[WAIT] booking 4905")


def test_a_normal_open_is_not_mistaken_for_it(monkeypatch):
    runner = FlowRunner.__new__(FlowRunner)
    monkeypatch.setattr(caf.time, "sleep", lambda s: None)
    monkeypatch.setattr(runner, "_idb_els",
                        lambda: [{"id": "addItemsBtn", "label": "addItemsBtn", "cy": 0}],
                        raising=False)
    monkeypatch.setattr(runner, "_table_modal_up", lambda: False, raising=False)
    monkeypatch.setattr(runner, "_note_ticket", lambda notes: None, raising=False)
    assert runner._reservation_opened([], "@open_reservation", "12:00", "events list")
    assert not getattr(runner, "_too_early", None)


FLOW = {"id": "f", "name": "F", "segments": [
    {"num": "1", "name": "C-App: book", "role": "consumer", "steps": ["open app"]},
    {"num": "2", "name": "Waiter: assign", "role": "waiter", "steps": ["@open_reservation"]},
    {"num": "3", "name": "Kitchen: ready", "role": "kitchen", "steps": ["@kitchen_ready"]},
]}


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'early.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(caf, "SessionLocal", Session)
    with Session() as s:
        s.add(TestRun(id="run1", status="running", job_state="running"))
        s.commit()
    monkeypatch.setattr(caf, "acquire_devices", lambda *a, **k: True)
    monkeypatch.setattr(caf, "release_devices", lambda run_id: None)
    monkeypatch.setattr(FlowRunner, "_flow_udids", lambda self: [])
    monkeypatch.setattr(FlowRunner, "_preflight", lambda self: None)
    return Session


def test_the_run_ends_stopped_with_the_time_and_can_be_resumed(db, monkeypatch):
    def segment(self, seg):
        if seg["num"] == "1":
            self._persist(seg, "PASS", ["[ok] @first_time_slot — selected slot '12:00'"], 60.0)
            return True
        self._too_early = ("booking 4905 at 12:00 can only be opened from 11:30 ... "
                           "come back after 11:30 and press Resume from failure.")
        self._persist(seg, "STOPPED", [f"[WAIT] {self._too_early}"], 10.0)
        return False

    monkeypatch.setattr(FlowRunner, "_run_segment", segment)
    FlowRunner("run1", FLOW, {}, {}).run()
    with db() as s:
        run = s.query(TestRun).filter_by(id="run1").one()
        rows = {r.scenario_num: r for r in s.query(ScenarioResult).filter_by(run_id="run1")}
        assert run.status == "stopped", "waiting for the window is not a failure"
        assert run.error_message.startswith("Waiting: booking 4905 at 12:00")
        assert rows["2"].status == "STOPPED"
        assert rows["3"].status == "SKIPPED" and "waiting" in rows["3"].reasons[0]
        assert "Stopped: booking 4905" in run.timeline
        plan = resume_plan(FLOW, list(rows.values()))
    assert plan["start_at"] == 1 and plan["booked_slot"] == "12:00"
    assert plan["retry"] is True, "segment 2 was tried (STOPPED): part of it may be done"


def test_a_run_asked_to_stop_after_a_segment_ends_stopped_and_resumes_at_the_next(db,
                                                                                  monkeypatch):
    ran = []

    def segment(self, seg):
        ran.append(seg["num"])
        self._persist(seg, "PASS", ["[ok] @first_time_slot — selected slot '12:00'"], 60.0)
        return True

    monkeypatch.setattr(FlowRunner, "_run_segment", segment)
    monkeypatch.setenv("FLOW_STOP_AFTER", "1")
    FlowRunner("run1", FLOW, {}, {}).run()
    assert ran == ["1"]
    with db() as s:
        run = s.query(TestRun).filter_by(id="run1").one()
        rows = {r.scenario_num: r for r in s.query(ScenarioResult).filter_by(run_id="run1")}
        assert run.status == "stopped", "stopping where asked is not a failure"
        assert "after segment 1" in run.error_message
        assert rows["2"].status == "SKIPPED" and "after segment 1" in rows["2"].reasons[0]
        plan = resume_plan(FLOW, list(rows.values()))
    assert plan["start_at"] == 1 and plan["booked_slot"] == "12:00"
    assert plan["retry"] is False, "segment 2 was not reached: a first attempt"


def test_a_run_asked_to_stop_after_its_last_segment_just_passes(db, monkeypatch):
    def segment(self, seg):
        self._persist(seg, "PASS", ["[ok] done"], 1.0)
        return True

    monkeypatch.setattr(FlowRunner, "_run_segment", segment)
    monkeypatch.setenv("FLOW_STOP_AFTER", "3")
    FlowRunner("run1", FLOW, {}, {}).run()
    with db() as s:
        assert s.query(TestRun).filter_by(id="run1").one().status == "passed"


def test_the_segment_marks_it_stopped_not_failed():
    import inspect
    src = inspect.getsource(FlowRunner._run_segment)
    assert 'if not ok and getattr(self, "_too_early", None):' in src
    assert 'status = "STOPPED"' in src


# -- the message is up for ~3s only -------------------------------------------------

def _reads(monkeypatch, frames):
    """Successive screen reads; the toast is in only some of them."""
    runner = FlowRunner.__new__(FlowRunner)
    seq = iter(frames)
    clock = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))
    monkeypatch.setattr(runner, "_idb_els", lambda: next(seq, []), raising=False)
    monkeypatch.setattr(runner, "_table_modal_up", lambda: False, raising=False)
    return runner, clock


def test_the_first_look_is_straight_after_the_tap(monkeypatch):
    toast = [{"id": "", "label": TOAST, "cy": 0}]
    runner, clock = _reads(monkeypatch, [toast])
    assert not runner._reservation_opened([], "@open_reservation", "12:00", "My Orders panel")
    assert runner._too_early and clock["t"] == 0.0, "no wait before the first read"


def test_a_toast_on_the_second_read_is_caught_within_its_three_seconds(monkeypatch):
    toast = [{"id": "", "label": TOAST, "cy": 0}]
    runner, clock = _reads(monkeypatch, [[], toast])
    assert not runner._reservation_opened([], "@open_reservation", "12:00", "My Orders panel")
    assert runner._too_early and clock["t"] < 1.0


def test_a_missed_toast_is_still_recognised_by_the_clock(monkeypatch):
    runner, _ = _reads(monkeypatch, [])
    monkeypatch.setattr(runner, "_window_not_open", lambda slot: True, raising=False)
    notes = []
    assert not runner._reservation_opened(notes, "@open_reservation", "12:00", "My Orders panel")
    assert runner._too_early and "was not caught" in notes[-1]


def test_an_open_window_that_fails_is_a_normal_failure(monkeypatch):
    runner, _ = _reads(monkeypatch, [])
    monkeypatch.setattr(runner, "_window_not_open", lambda slot: False, raising=False)
    notes = []
    assert not runner._reservation_opened(notes, "@open_reservation", "12:00", "events list")
    assert not getattr(runner, "_too_early", None)
    assert "did not open" in notes[-1]


def test_the_window_rule_matches_the_app(monkeypatch):
    from datetime import datetime as real_dt

    class Now(real_dt):
        @classmethod
        def now(cls, tz=None):
            return real_dt(2026, 10, 1, 11, 0)
    import datetime as dtmod
    monkeypatch.setattr(dtmod, "datetime", Now)
    runner = FlowRunner.__new__(FlowRunner)
    assert runner._window_not_open("12:00")          # opens 11:30
    assert not runner._window_not_open("11:30")      # opened at 11:00
    assert not runner._window_not_open("11:15")


def test_time_the_ipad_cannot_be_read_does_not_count_against_the_wait(monkeypatch):
    # MEASURED 2026-11-02 (run 34bf4868): right after the participant's iPhone shut down,
    # "tapped the 15:30 row ... but the reservation did not open"; the order was open.
    order = [{"id": "ORDER SUMMARY", "label": "ORDER SUMMARY", "cy": 58}]
    runner, clock = _reads(monkeypatch, [])
    reads = iter([[]] * 8 + [order])

    def read():
        frame = next(reads, order)
        clock["t"] += 30.0 if not frame else 3.0      # an unread screen: idb's 30s timeout
        return frame
    monkeypatch.setattr(runner, "_idb_els", read, raising=False)
    monkeypatch.setattr(runner, "_note_ticket", lambda notes: None, raising=False)
    notes = []
    assert runner._reservation_opened(notes, "@open_order", "15:30", "My Orders panel")
    assert "opened the 15:30 booking" in notes[-1]


def test_an_unreadable_ipad_still_gives_up_in_bounded_time(monkeypatch):
    runner, clock = _reads(monkeypatch, [])

    def read():
        clock["t"] += 30.0
        return []
    monkeypatch.setattr(runner, "_idb_els", read, raising=False)
    monkeypatch.setattr(runner, "_window_not_open", lambda slot: False, raising=False)
    notes = []
    assert not runner._reservation_opened(notes, "@open_order", "15:30", "My Orders panel")
    assert clock["t"] <= 340.0


def test_opening_an_order_gets_the_long_step_limit():
    assert caf._step_limit("@open_order") == caf.STEP_TIMEOUT_HEAVY
    assert caf._step_limit("@open_event") == caf.STEP_TIMEOUT_HEAVY
