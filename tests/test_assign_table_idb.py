"""@assign_table through idb when the table sheet is up (measured 53-75s via Appium).

Sheet (screenshot, booking 4774): heading 'Select a table', chips I2 I3 I4 ...
(booked tables are not offered), Confirm pale until a chip is picked.
"""
import pytest

from automation.scenarios import cross_app_flows as caf
from automation.scenarios.cross_app_flows import FlowRunner

APP = {"AXLabel": "Vya Business", "type": "Application",
       "frame": {"x": 0, "y": 0, "width": 1210, "height": 834}}


def el(name, x, y, w=40, h=30, t="GenericElement"):
    return {"AXLabel": name, "type": t, "frame": {"x": x, "y": y, "width": w, "height": h}}


class Sheet:
    def __init__(self, confirm_turns_dark=True):
        self.picked, self.closed = None, False
        self.dark = confirm_turns_dark
        self.taps = []

    def els(self):
        base = [APP, el("I2", 160, 60, t="StaticText")]      # a table badge BEHIND the sheet
        if self.closed:
            return base
        return base + [el("Select a table", 500, 300, 200, 24, "StaticText"),
                       el("I2", 420, 360), el("I3", 470, 360), el("I4", 520, 360),
                       el("applyTableBtn", 430, 450, 380, 60)]

    def tap_el(self, udid, e, els=None, scroll=True):
        n = caf._idbd.name(e)
        self.taps.append(n)
        if n == "applyTableBtn" and self.picked:
            self.closed = True
        elif n.startswith("I"):
            self.picked = n
        return True, "idb"

    def colours(self, udid, points, els=None, radius=1.5):
        return [(97, 69, 119) if (self.picked and self.dark) else (177, 163, 187)] * len(points)


@pytest.fixture
def run(monkeypatch):
    def go(**kw):
        sh = Sheet(**kw)
        monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: sh.els())
        monkeypatch.setattr(caf._idbd, "tap_el", sh.tap_el)
        monkeypatch.setattr(caf._idbd, "sample_colors", sh.colours)
        monkeypatch.setattr(caf.time, "sleep", lambda s: None)
        runner = FlowRunner.__new__(FlowRunner)
        runner.devices = {"waiter": "IPAD"}
        monkeypatch.setattr(runner, "_table_sheet_open", lambda: not sh.closed, raising=False)
        notes = []
        return runner._assign_table_idb(notes), notes, sh
    return go


def test_first_free_table_is_picked_confirmed_and_the_sheet_closes(run):
    ok, notes, sh = run()
    assert ok and sh.taps == ["I2", "applyTableBtn"] and sh.closed
    assert "assigned 'I2'" in notes[-1]


def test_confirm_still_pale_hands_over_to_the_full_routine(run):
    ok, notes, sh = run(confirm_turns_dark=False)
    assert not ok and "applyTableBtn" not in sh.taps and not notes


def test_the_table_badge_behind_the_sheet_is_not_a_chip(run):
    ok, notes, sh = run()
    # 'I2' at y=60 sits above the sheet's heading: only the chip row counts.
    assert sh.taps[0] == "I2" and sh.picked == "I2"
