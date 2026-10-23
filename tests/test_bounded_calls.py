"""Ceilings on Appium calls, and what happens after one is hit (review 2026-10-19).

A wedged WDA parks an Appium call on this app's huge tree with no ceiling of its own
(@open_order and a typing step hung 240s on 2026-10-17). _bounded_call gives up on
it -- but cannot CANCEL it: Appium queues each session's commands and runs an
abandoned one later. So after a timeout nothing more may be sent that could act on
the screen late, and the app is relaunched with xcrun simctl, never through Appium."""
import subprocess
import threading
import time

from automation.scenarios import cross_app_flows as caf
from automation.scenarios.cross_app_flows import FlowRunner

# The real method, captured at import: conftest stubs it for every test so that no
# test can relaunch the app on a live simulator.
_REAL_RELAUNCH = FlowRunner._relaunch_business


def _quick(monkeypatch, secs=0.2):
    """Shrink every ceiling so a hung call costs the test a fraction of a second."""
    orig = caf._bounded_call
    monkeypatch.setattr(caf, "_bounded_call",
                        lambda fn, s=20.0, default=None, late=caf._NOT_GIVEN:
                        orig(fn, secs, default, late))


def test_bounded_call_abandons_a_hung_call_and_tells_late_from_failed():
    ev = threading.Event()
    try:
        t0 = time.monotonic()
        assert caf._bounded_call(ev.wait, 0.2, "dflt") == "dflt"
        assert time.monotonic() - t0 < 1.0, "must not wait for the hung call (AGENTS.md #5)"
        late = object()
        assert caf._bounded_call(ev.wait, 0.2, "dflt", late=late) is late
    finally:
        ev.set()            # concurrent.futures joins a stuck worker at interpreter exit
    assert caf._bounded_call(lambda: 1 / 0, 1.0, "d") == "d"
    assert caf._bounded_call(lambda: 1 / 0, 1.0, "d", late=object()) == "d"   # an error is not late
    assert caf._bounded_call(lambda: 42, 1.0, "d") == 42


def test_window_size_falls_back_to_idb_when_wda_does_not_answer(monkeypatch):
    _quick(monkeypatch)
    ev = threading.Event()

    class D:
        session_id = "s1"

        def get_window_size(self):
            ev.wait()
            return {"width": 1, "height": 1}

    monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: [
        {"AXLabel": "Vya Business", "type": "Application",
         "frame": {"x": 0, "y": 0, "width": 1210, "height": 834}}])
    runner = FlowRunner.__new__(FlowRunner)
    runner.devices = {"waiter": "IPAD"}
    r = type("R", (), {"d": D()})()
    try:
        assert runner._window_size(r) == (1210.0, 834.0)
        assert runner._window_h(r) == 834.0 and runner._window_w(r) == 1210.0
    finally:
        ev.set()


def test_the_relaunch_goes_through_simctl_not_appium(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw.get("timeout")))
        return subprocess.CompletedProcess(cmd, 0, "org.app: 123", "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(caf.time, "sleep", lambda s: None)
    runner = FlowRunner.__new__(FlowRunner)
    runner.devices = {"waiter": "IPAD-UDID"}
    runner.business_bundle = "org.app"
    monkeypatch.setattr(runner, "_business_udid", lambda: "IPAD-UDID", raising=False)
    notes = []
    assert _REAL_RELAUNCH(runner, notes, "@open_order")
    assert [c[0] for c in calls] == [["xcrun", "simctl", "terminate", "IPAD-UDID", "org.app"],
                                     ["xcrun", "simctl", "launch", "IPAD-UDID", "org.app"]]
    assert all(t for _c, t in calls), "every simctl call has a timeout"


def test_a_failed_relaunch_is_reported(monkeypatch):
    def run(cmd, **kw):
        rc = 1 if "launch" in cmd else 0
        return subprocess.CompletedProcess(cmd, rc, "", "Unable to launch org.app")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(caf.time, "sleep", lambda s: None)
    runner = FlowRunner.__new__(FlowRunner)
    runner.business_bundle = "org.app"
    monkeypatch.setattr(runner, "_business_udid", lambda: "IPAD-UDID", raising=False)
    notes = []
    assert not _REAL_RELAUNCH(runner, notes, "@open_order")
    assert "Unable to launch" in notes[-1]


def test_a_typing_fallback_that_times_out_does_not_type_a_second_time(monkeypatch):
    """MEASURED 2026-10-17 (flow6): the idb fill did not verify and the Appium fallback
    hung. A timed-out fill keeps running (Appium queues it), so falling through to the
    resolver would type into the same field again."""
    _quick(monkeypatch)
    ev = threading.Event()
    monkeypatch.setattr(caf._idbd, "fill", lambda udid, field, text, **k: (False, ""))
    monkeypatch.setattr(caf, "_fill_field", lambda d, field, text: ev.wait())

    class R:
        d = object()

        def run_one(self, *a, **k):
            raise AssertionError("must not type a second time")

    runner = FlowRunner.__new__(FlowRunner)
    runner.devices = {"waiter": "IPAD"}
    runner._cur_udid = "IPAD"
    try:
        ok, note, _flaky = runner._smart_click(R(), "type 9686496589 in mobileInputBtn")
    finally:
        ev.set()
    assert not ok and "not typing a second time" in note
