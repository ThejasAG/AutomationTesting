"""The consumer's Duration became a 4-stop slider (Oct 2026): 1 hr, 2 hr, 3 hr,
Not Sure. The knob has no id; the labels under the stops are `duration1`..4
(Reservation.js). MEASURED on the iPhone: labels at y=609, knob centre y=592,
and sliding it to Not Sure replaced the 'Select a duration' prompt with slots."""
import types

import pytest

from automation.scenarios import cross_app_flows as caf
from automation.scenarios.cross_app_flows import FLOWS, FlowRunner


def el(name, x, y, w=80, h=14, kind="GenericElement"):
    return {"AXLabel": name, "AXIdentifier": "", "type": kind,
            "frame": {"x": x, "y": y, "width": w, "height": h}}


class Bar:
    def __init__(self, slide_works=True):
        self.stop, self.slide_works, self.swipes, self.taps = 0, slide_works, [], []

    def describe_all(self, udid):
        els = [el("Vya Consumer", 0, 0, 402, 874, "Application")]
        els += [el(f"duration{i}", 2 + 106 * (i - 1), 609) for i in range(1, 5)]
        if self.stop:
            els += [el("10:55", 20, 735, 60, 30), el("11:00", 90, 735, 60, 30)]
        else:
            els.append(el("Select a duration to see available time slots", 20, 735, 262, 32,
                          "StaticText"))
        return els

    def swipe(self, udid, x1, y1, x2, y2, duration=0.25):
        self.swipes.append((round(x1), round(y1), round(x2), round(y2)))
        knob_x = 42 + 106 * max(self.stop - 1, 0)        # the knob starts on 1 hr
        if self.slide_works and abs(x1 - knob_x) < 20 and abs(y1 - 592) < 10:
            self.stop = 1 + round((x2 - 42) / 106)

    def tap(self, udid, names, els=None, scroll=True):
        self.taps.append(names[0])
        self.stop = int(names[0][-1])
        return True, "idb"


@pytest.fixture
def run(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))

    def go(bar, which="not_sure"):
        for fn in ("describe_all", "swipe", "tap"):
            monkeypatch.setattr(caf._idbd, fn, getattr(bar, fn))
        r = FlowRunner.__new__(FlowRunner)
        r.devices = {"consumer": "IPHONE"}
        notes = []
        return r._set_duration(which, notes), notes
    return go


def test_slides_the_knob_from_1hr_to_not_sure_on_its_centre_line(run):
    bar = Bar()
    ok, notes = run(bar)
    assert ok and bar.stop == 4 and bar.taps == []
    assert bar.swipes[0] == (42, 592, 360, 592)
    assert "Not Sure" in notes[-1] and "slid the bar" in notes[-1]


def test_falls_back_to_the_stop_label_when_the_slide_does_not_take(run):
    bar = Bar(slide_works=False)
    ok, notes = run(bar)
    assert ok and bar.taps == ["duration4"] and "tapped the stop's label" in notes[-1]


def test_other_stops(run):
    bar = Bar()
    ok, _ = run(bar, "2")
    assert ok and bar.stop == 2


def test_old_wording_still_works_in_saved_flows():
    assert FlowRunner._PLAIN_STEP_TOKENS["select Not Sure"] == "@duration:not_sure"
    for f in FLOWS.values():
        for s in f["segments"]:
            assert "select Not Sure" not in s["steps"], f["id"]
