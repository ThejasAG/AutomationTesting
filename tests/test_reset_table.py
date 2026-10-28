"""The My Bookings reset-table handler (@reset_table).

Covers automation/scenarios/cross_app_flows.py::FlowRunner._reset_table,
dispatched from `@reset_table` in _handle_special.

The booking card (Components/BookingCard/index.js) is ONE accessible element, so
its ⋮ is tapped at an offset from the card's corner; the Reset table / Confirm
radio sheet is Components/Modal/EventAction/index.js; the final 'Reset' is the
app's native Alert (Screens/Home/index.js `_resetTable`). These tests pin: the
reset is only reported once the toast shows AND the card leaves the Table view,
a disabled Reset (canResetTable false) is a failure that names why, and with
several of the diner's In Progress cards in the row and no known table it
refuses instead of resetting someone else's booking.
"""
import pytest

from automation.scenarios import cross_app_flows as F
from automation.scenarios import screen_text


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(F.time, "sleep", lambda s=0: clock.__setitem__(0, clock[0] + (s or 0.1)))
    monkeypatch.setattr(F.time, "time", lambda: clock.__setitem__(0, clock[0] + 0.1) or clock[0])
    monkeypatch.setattr(screen_text, "read_text", lambda *a, **k: [])


def _el(label, x=0, y=0, w=100, h=40, typ="Other"):
    return {"AXIdentifier": "", "AXLabel": label, "type": typ,
            "frame": {"x": x, "y": y, "width": w, "height": h}}


class Board:
    """The waiter's board, with this run's In Progress card in the 12:00 row."""

    def __init__(self, cards=1, resettable=True):
        self.screen = "board"
        self.cards = cards
        self.resettable = resettable
        self.taps = []

    def els(self):
        out = [_el("", 0, 0, 1210, 834, "Application"), _el("addNewEvent", 600, 760, 50, 50),
               _el("tableBtn", 500, 40, 60, 30), _el("allBtn", 420, 40, 40, 30),
               _el("My Orders", 820, 30, 200, 30), _el("11:00", 20, 200, 50, 20),
               _el("12:00", 20, 360, 50, 20), _el("13:00", 20, 520, 50, 20)]
        out += [_el("RoopaDcardInProgress", 110 + i * 500, 355, 496, 150)
                for i in range(self.cards)]
        if self.screen == "menu":
            out += [_el("resetTableBtn", 400, 300, 300, 40), _el("confirmEventBtn", 450, 600, 200, 50)]
        if self.screen == "alert":
            out += [_el("Reset table", 450, 300, 200, 30, "StaticText"),
                    _el("Cancel", 450, 400, 100, 40, "Button"), _el("Reset", 560, 400, 100, 40, "Button")]
        if self.screen == "toast":
            out += [_el("Table has been reset successfully!", 300, 700, 400, 40, "StaticText")]
        return out

    def describe_all(self, udid):
        return self.els()

    def tap_point(self, udid, x, y):
        self.taps.append(("point", round(x), round(y)))
        if self.screen == "board" and 580 <= x <= 606 and 355 <= y <= 400:
            self.screen = "menu"                     # the ⋮ of the first card
        return True, "idb"

    def tap(self, udid, names, els=None, scroll=True, wait=0.0):
        name = names[0]
        self.taps.append(name)
        if name == "confirmEventBtn" and self.screen == "menu" and self.resettable:
            self.screen = "alert"
        elif name == "Reset" and self.screen == "alert":
            self.screen, self.cards = "toast", self.cards - 1
        return True, "idb"


def runner(board, monkeypatch):
    for fn in ("describe_all", "tap_point", "tap"):
        monkeypatch.setattr(F._idbd, fn, getattr(board, fn))
    fr = object.__new__(F.FlowRunner)
    fr._cur_udid = "UDID"
    fr.devices = {}
    fr._booked_slot = "12:20"
    return fr


def test_dispatch_reaches_the_handler():
    fr = object.__new__(F.FlowRunner)
    calls = []
    fr._reset_table = lambda notes: calls.append(1) or True
    assert F.FlowRunner._handle_special(fr, None, "@reset_table", [])
    assert calls == [1]


def test_resets_through_the_menu_and_proves_the_card_left_the_table_view(monkeypatch):
    board = Board()
    fr = runner(board, monkeypatch)
    notes = []
    assert fr._reset_table(notes) is True, notes
    assert board.taps[0] == "tableBtn"
    assert board.taps.index("resetTableBtn") < board.taps.index("confirmEventBtn") \
        < board.taps.index("Reset")
    assert board.taps[-1] == "allBtn"               # the board's filter is put back
    assert "1 → 0 In Progress" in notes[-1]


def test_a_disabled_reset_never_shows_the_alert_and_says_why(monkeypatch):
    board = Board(resettable=False)                 # canResetTable false: Confirm does nothing
    fr = runner(board, monkeypatch)
    notes = []
    assert fr._reset_table(notes) is False
    assert "Reset" not in board.taps
    assert "did not bring up the 'Reset table' alert" in notes[-1]


def test_several_cards_and_no_known_table_refuses_to_guess(monkeypatch):
    board = Board(cards=2)
    fr = runner(board, monkeypatch)
    notes = []
    assert fr._reset_table(notes) is False
    assert not any(isinstance(t, tuple) for t in board.taps)   # no ⋮ was tapped
    assert "refusing to reset" in notes[-1]


def test_the_assigned_table_is_read_from_this_segments_notes():
    notes = ["[ok] @assign_table — assigned 'tableChipI2' and the sheet closed (idb)"]
    assert F.FlowRunner._assigned_table(notes) == "I2"
    assert F.FlowRunner._same_table("12", "I2")      # OCR reads I as 1
    assert not F.FlowRunner._same_table("I3", "I2")
