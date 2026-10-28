"""The Event screen's Modify Table handler (@modify_table).

Covers automation/scenarios/cross_app_flows.py::FlowRunner._modify_table,
dispatched from `@modify_table` in _handle_special.

The header's table (modifyTable, Screens/Event/PaymentDetails.js:4913) is ONE
accessible element, so the table name inside it is read off the pixels; it opens
the 'Modify Table' sheet (Components/Modal/EventTableSelect), whose chips are
named after the free tables. These tests pin: it moves to a table OTHER than the
current one, the move is reported only when the header prints the new table, and
a sheet that stays open (a refused table) is a failure.
"""
import pytest

from automation.scenarios import cross_app_flows as F
from automation.scenarios import screen_text


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(F.time, "sleep", lambda s=0: clock.__setitem__(0, clock[0] + (s or 0.1)))
    monkeypatch.setattr(F.time, "time", lambda: clock.__setitem__(0, clock[0] + 0.1) or clock[0])


def _el(label, x=0, y=0, w=100, h=40, typ="Other"):
    return {"AXIdentifier": "", "AXLabel": label, "type": typ,
            "frame": {"x": x, "y": y, "width": w, "height": h}}


HEADER = (900, 80, 60, 24)


class EventScreen:
    def __init__(self, table="I2", free=("I2", "I3", "I5"), header_follows=True):
        self.table = table
        self.free = list(free)
        self.header_follows = header_follows
        self.screen = "event"
        self.picked = None
        self.regions = []

    def describe_all(self, udid):
        out = [_el("", 0, 0, 1210, 834, "Application"), _el("modifyTable", *HEADER, "Button"),
               _el("selectAllItemsBtn", 100, 700, 120, 40)]
        if self.screen == "sheet":
            out += [_el("Modify Table", 450, 280, 200, 30, "StaticText"),
                    _el("AssignTableBtn", 520, 480, 160, 50, "Button")]
            out += [_el(t, 400 + i * 70, 360, 60, 40) for i, t in enumerate(self.free)]
        return out

    def read_text(self, udid, region=None, accurate=False):
        self.regions.append(region)
        x, y, w, h = HEADER
        if region and region[0] <= x and region[1] <= y and region[2] >= x + w \
                and region[3] >= y + h:
            return [(self.table, x + 30, y + 2, 20, 18)]
        return []

    def tap(self, udid, names, els=None, scroll=True, wait=0.0):
        if names[0] == "modifyTable" and self.screen == "event":
            self.screen = "sheet"
        return True, "idb"

    def tap_el(self, udid, e, els=None, scroll=True):
        n = F._idbd.name(e)
        if n in self.free:
            self.picked = n
        elif n == "AssignTableBtn" and self.picked and self.picked != "I5":
            self.screen = "event"                 # I5 is refused: the sheet stays open
            if self.header_follows:
                self.table = self.picked
        return True, "idb"


def runner(screen, monkeypatch):
    for fn in ("describe_all", "tap", "tap_el"):
        monkeypatch.setattr(F._idbd, fn, getattr(screen, fn))
    monkeypatch.setattr(screen_text, "read_text", screen.read_text)
    fr = object.__new__(F.FlowRunner)
    fr._cur_udid = "IPAD"
    fr.devices = {"business": "IPAD"}
    fr._business_udid = lambda: "IPAD"
    fr._table_sheet_open = lambda: screen.screen == "sheet"
    fr._screen_toast = lambda udid: ""
    return fr


def test_dispatch_reaches_the_handler():
    fr = object.__new__(F.FlowRunner)
    calls = []
    fr._modify_table = lambda notes: calls.append(1) or True
    assert F.FlowRunner._handle_special(fr, None, "@modify_table", [])
    assert calls == [1]


def test_moves_to_another_table_and_the_header_shows_it(monkeypatch):
    screen = EventScreen()
    fr = runner(screen, monkeypatch)
    notes = ["[ok] @assign_table — assigned 'I2' and the sheet closed (idb)"]
    assert fr._modify_table(notes) is True, notes
    assert screen.picked == "I3"                       # never the current table I2
    assert screen.table == "I3"
    assert "moved from I2 to I3" in notes[-1]
    # the header is read as an (x0, y0, x1, y1) region around modifyTable
    x, y, w, h = HEADER
    assert all(r[2] > r[0] and r[3] > r[1] for r in screen.regions if r)


def test_the_current_table_is_read_off_the_header_when_unknown(monkeypatch):
    screen = EventScreen(table="I3", free=("I3", "I2"))
    fr = runner(screen, monkeypatch)
    notes = ["[ok] @assign_table — assigned 'T0AssignAnyBtn' and the sheet closed (idb)"]
    assert fr._modify_table(notes) is True, notes
    assert screen.table == "I2"


def test_a_header_that_does_not_change_is_a_failure(monkeypatch):
    screen = EventScreen(header_follows=False)
    fr = runner(screen, monkeypatch)
    notes = ["[ok] @assign_table — assigned 'I2' and the sheet closed (idb)"]
    assert fr._modify_table(notes) is False
    assert "Event header reads" in notes[-1]


def test_a_refused_table_leaves_the_sheet_open_and_fails(monkeypatch):
    screen = EventScreen(table="I2", free=("I5",))
    fr = runner(screen, monkeypatch)
    notes = ["[ok] @assign_table — assigned 'I2' and the sheet closed (idb)"]
    assert fr._modify_table(notes) is False
    assert "stayed open" in notes[-1]
