"""The My Bookings cancel handler (@cancel_event) and its consumer-side check
(@wallet_invite_cancelled).

Covers automation/scenarios/cross_app_flows.py::FlowRunner._cancel_event and
::FlowRunner._wallet_invite_cancelled, dispatched from `@cancel_event` /
`@wallet_invite_cancelled` in _handle_special.

The waiter-created appointment's card (Components/BookingCard/index.js) is ONE
accessible element, 'RoopaDcardConfirmationPending', so its ⋮ is tapped at an
offset from the card's corner; Cancel the event / Confirm is
Components/Modal/EventAction/index.js; Screens/Home/index.js `_cancelAppointment`
toasts the result. These tests pin: the cancel is reported only once the toast
shows AND the card leaves the pending cards, a refusal says why, and with several
pending cards in the row it cancels only the one printing this run's slot.
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


def _corners(frame):
    """screen_text.read_text's region is (x0, y0, x1, y1). MEASURED (run 3f9f259f):
    passing idb's (x, y, w, h) frame cropped nothing and the card read ''."""
    x, y, w, h = frame
    return (x, y, x + w, y + h)


class Board:
    """The waiter's board with pending cards for Roopa D in the 12:00 row; card i
    prints the slot times[i]. Tapping the ⋮ of card i opens the action menu for it."""

    def __init__(self, times=("12:20",), cancel_works=True):
        self.screen = "board"
        self.times = list(times)
        self.status = ["ConfirmationPending"] * len(times)
        self.cancel_works = cancel_works
        self.menu_for = None
        self.picked = None
        self.taps = []

    def card_frame(self, i):
        return (110 + i * 500, 355, 496, 150)

    def els(self):
        out = [_el("", 0, 0, 1210, 834, "Application"), _el("addNewEvent", 600, 760, 50, 50),
               _el("My Orders", 1200, 30, 10, 30), _el("11:00", 20, 200, 50, 20),
               _el("12:00", 20, 360, 50, 20), _el("13:00", 20, 520, 50, 20)]
        out += [_el(f"RoopaDcard{s}", *self.card_frame(i)) for i, s in enumerate(self.status)]
        if self.screen == "menu":
            out += [_el("cancelEvent", 400, 300, 300, 40), _el("editEvent", 400, 420, 300, 40),
                    _el("confirmEventBtn", 450, 600, 200, 50)]
        if self.screen == "toast":
            text = ("Event has been cancelled successfully!" if self.cancel_works
                    else "Something went wrong!")
            out += [_el(text, 300, 700, 400, 40, "StaticText")]
        if self.screen == "edit":                  # Components/EditEventModal
            out += [_el(f"{t}Btn", 800 + i * 100, 500, 96, 32)
                    for i, t in enumerate(("12:15", "12:20", "12:30", "12:45"))]
            out += [_el("saveEditBtn", 800, 600, 300, 50, "Button")]
        if self.screen == "edited":
            out += [_el("Reservation updated" if self.cancel_works
                        else "Could not update reservation", 300, 700, 400, 40, "StaticText")]
        return out

    def describe_all(self, udid):
        return self.els()

    def read_text(self, udid, region=None, accurate=False):
        for i, t in enumerate(self.times):
            if region == _corners(self.card_frame(i)):
                return [(f"{t} - 13:20", 0, 0, 10, 10)]
        return []

    def tap_point(self, udid, x, y):
        self.taps.append(("point", round(x), round(y)))
        for i in range(len(self.times)):
            cx, cy, cw, _ = self.card_frame(i)
            if self.screen == "board" and cx + cw - 30 <= x <= cx + cw and 355 <= y <= 400:
                self.screen, self.menu_for = "menu", i
        return True, "idb"

    def tap(self, udid, names, els=None, scroll=True, wait=0.0):
        name = names[0]
        self.taps.append(name)
        if name == "confirmEventBtn" and self.screen == "menu" and "cancelEvent" in self.taps:
            self.screen = "toast"
            if self.cancel_works:
                self.status[self.menu_for] = "Cancelled"
        elif name == "confirmEventBtn" and self.screen == "menu" and "editEvent" in self.taps:
            self.screen = "edit"
        elif name == "saveEditBtn" and self.screen == "edit":
            self.screen = "edited"
            if self.cancel_works and self.picked:
                self.times[self.menu_for] = self.picked     # the card prints the new time
        elif self.screen == "edit" and name.endswith("Btn") and name[:2].isdigit():
            self.picked = name[:-3]
        return True, "idb"


def runner(board, monkeypatch):
    for fn in ("describe_all", "tap_point", "tap"):
        monkeypatch.setattr(F._idbd, fn, getattr(board, fn))
    monkeypatch.setattr(screen_text, "read_text", board.read_text)
    fr = object.__new__(F.FlowRunner)
    fr._cur_udid = "UDID"
    fr.devices = {}
    fr._booked_slot = "12:20"
    return fr


def test_dispatch_reaches_the_handlers():
    fr = object.__new__(F.FlowRunner)
    calls = []
    fr._cancel_event = lambda notes: calls.append("cancel") or True
    fr._wallet_invite_cancelled = lambda notes: calls.append("wallet") or True
    assert F.FlowRunner._handle_special(fr, None, "@cancel_event", [])
    assert F.FlowRunner._handle_special(fr, None, "@wallet_invite_cancelled", [])
    assert calls == ["cancel", "wallet"]


def test_cancels_through_the_menu_and_proves_the_card_left_the_pending_cards(monkeypatch):
    board = Board()
    fr = runner(board, monkeypatch)
    notes = []
    assert fr._cancel_event(notes) is True, notes
    assert board.taps.index("cancelEvent") < board.taps.index("confirmEventBtn")
    assert board.status == ["Cancelled"]
    assert "1 → 0" in notes[-1]


def test_a_failed_cancel_is_a_failure(monkeypatch):
    board = Board(cancel_works=False)              # 'Something went wrong!'
    fr = runner(board, monkeypatch)
    notes = []
    assert fr._cancel_event(notes) is False
    assert board.status == ["ConfirmationPending"]


def test_with_several_pending_cards_it_cancels_the_one_at_this_runs_slot(monkeypatch):
    board = Board(times=("12:05", "12:20"))
    fr = runner(board, monkeypatch)
    notes = []
    assert fr._cancel_event(notes) is True, notes
    assert board.status == ["ConfirmationPending", "Cancelled"]


def test_no_pending_card_at_this_runs_slot_refuses_to_guess(monkeypatch):
    board = Board(times=("12:05", "12:40"))
    fr = runner(board, monkeypatch)
    notes = []
    assert fr._cancel_event(notes) is False
    assert not any(isinstance(t, tuple) for t in board.taps)   # no ⋮ was tapped
    assert "refusing to act on" in notes[-1]


class CrowdedRow:
    """MEASURED (run 20d429b3): the 12:00 row held 5 cards; this run's pending one
    was off to the right. The row's scroll view is clipped at x≈737 while the My
    Orders title sits at x≈823: a drag that starts right of the clip moves nothing,
    and a ⋮ in the clipped strip cannot be tapped."""
    CLIP = 737

    def __init__(self):
        self.screen = "board"
        self.cards = [["RoopaDcardReserved", 140], ["RoopaDcardCancelled", 640],
                      ["RoopaDcardDeclined", 1140], ["RoopaDcardConfirmationPending", 1640]]
        self.taps = []

    def describe_all(self, udid):
        out = [_el("", 0, 0, 1210, 834, "Application"), _el("addNewEvent", 600, 760, 50, 50),
               _el("My Orders", 823, 30, 200, 30), _el("11:00", 20, 200, 50, 20),
               _el("12:00", 20, 360, 50, 20), _el("13:00", 20, 520, 50, 20)]
        out += [_el(n, x, 355, 480, 150) for n, x in self.cards]
        if self.screen == "menu":
            out += [_el("cancelEvent", 400, 300, 300, 40), _el("confirmEventBtn", 450, 600, 200, 50)]
        if self.screen == "toast":
            out += [_el("Event has been cancelled successfully!", 300, 700, 400, 40, "StaticText")]
        return out

    def swipe(self, udid, x1, y1, x2, y2, duration=0.25):
        if x1 < self.CLIP and 355 <= y1 <= 505:
            for c in self.cards:
                c[1] += x2 - x1

    def tap_point(self, udid, x, y):
        self.taps.append(("point", round(x), round(y)))
        n, cx = self.cards[3]
        if self.screen == "board" and x < self.CLIP and cx + 480 - 30 <= x <= cx + 480 \
                and 355 <= y <= 400:
            self.screen = "menu"
        return True, "idb"

    def tap(self, udid, names, els=None, scroll=True, wait=0.0):
        self.taps.append(names[0])
        if names[0] == "confirmEventBtn" and self.screen == "menu":
            self.screen = "toast"
            self.cards[3][0] = "RoopaDcardCancelled"
        return True, "idb"


def test_a_card_past_the_rows_clip_is_dragged_in_from_inside_the_row(monkeypatch):
    board = CrowdedRow()
    for fn in ("describe_all", "tap_point", "tap", "swipe"):
        monkeypatch.setattr(F._idbd, fn, getattr(board, fn))
    monkeypatch.setattr(screen_text, "read_text", lambda *a, **k: [])
    fr = object.__new__(F.FlowRunner)
    fr._cur_udid = "UDID"
    fr.devices = {}
    fr._booked_slot = "12:30"
    notes = []
    assert fr._cancel_event(notes) is True, notes
    point = next(t for t in board.taps if isinstance(t, tuple))
    assert point[1] < CrowdedRow.CLIP                # the ⋮ was tapped where it is visible


def test_edit_dispatch_reaches_the_handler():
    fr = object.__new__(F.FlowRunner)
    calls = []
    fr._edit_event_time = lambda notes: calls.append(1) or True
    assert F.FlowRunner._handle_special(fr, None, "@edit_event_time", [])
    assert calls == [1]


def test_edit_moves_to_the_next_time_and_makes_it_this_runs_slot(monkeypatch):
    board = Board()
    fr = runner(board, monkeypatch)
    notes = []
    assert fr._edit_event_time(notes) is True, notes
    assert board.taps.index("editEvent") < board.taps.index("confirmEventBtn") \
        < board.taps.index("12:30Btn") < board.taps.index("saveEditBtn")
    assert "cancelEvent" not in board.taps
    assert fr._booked_slot == "12:30"               # the consumer side checks the new time
    assert "from 12:20 to 12:30" in notes[-1]


def test_a_missed_toast_is_fine_when_the_card_prints_the_new_time(monkeypatch):
    """MEASURED (run 8e8e2196): the panel closed, the toast fell between two reads."""
    board = Board()
    base = board.els
    board.els = lambda: [e for e in base() if F._idbd.name(e) != "Reservation updated"]
    fr = runner(board, monkeypatch)
    notes = []
    assert fr._edit_event_time(notes) is True, notes
    assert fr._booked_slot == "12:30"


def test_a_closed_panel_with_the_old_time_on_the_card_is_a_failure(monkeypatch):
    board = Board(cancel_works=False)              # catch(): panel closes, time unchanged
    base = board.els
    board.els = lambda: [e for e in base() if "reservation" not in F._idbd.name(e).lower()]
    fr = runner(board, monkeypatch)
    notes = []
    assert fr._edit_event_time(notes) is False
    assert "prints 12:30" in notes[-1]
    assert fr._booked_slot == "12:20"


def test_a_refused_edit_keeps_the_old_slot(monkeypatch):
    board = Board(cancel_works=False)              # 'Could not update reservation'
    fr = runner(board, monkeypatch)
    notes = []
    assert fr._edit_event_time(notes) is False
    assert fr._booked_slot == "12:20"
    assert "the app said 'Could not update reservation'" in notes[-1]


def test_a_seat_capacity_refusal_is_reported_in_the_apps_words(monkeypatch):
    """MEASURED (run b5dd8ea4): Update left the panel open with no success toast."""
    board = Board()
    sorry = "Sorry, at 12:30 PM - 1:30 PM this restaurant has only 0 seats !!"
    base = board.els
    board.els = lambda: [e for e in base() if F._idbd.name(e) != "Reservation updated"] + (
        [_el(sorry, 300, 700, 400, 40, "StaticText")] if board.screen == "edited" else [])
    fr = runner(board, monkeypatch)
    notes = []
    assert fr._edit_event_time(notes) is False
    assert sorry in notes[-1]
    assert fr._booked_slot == "12:20"


class Wallet:
    """C-App Wallet → Upcoming with invitation cards; each prints a time and maybe
    the 'EVENT CANCELLED' band."""

    def __init__(self, invites):
        self.invites = invites                     # [(time, cancelled)]

    def frame(self, i):
        return (20, 150 + i * 160, 350, 150)

    def describe_all(self, udid):
        out = [_el("", 0, 0, 390, 844, "Application")]
        out += [_el("NylaiKitchen2InviteCard", *self.frame(i)) for i in range(len(self.invites))]
        return out

    def read_text(self, udid, region=None, accurate=False):
        for i, (t, cancelled) in enumerate(self.invites):
            if region == _corners(self.frame(i)):        # (x0, y0, x1, y1), not a frame
                return [("Today", 0, 0, 1, 1), (t, 0, 0, 1, 1)] + (
                    [("EVENT CANCELLED", 0, 0, 1, 1)] if cancelled else [])
        return []

    def swipe(self, *a, **k):
        return True


def wallet_runner(wallet, monkeypatch):
    monkeypatch.setattr(F._idbd, "describe_all", wallet.describe_all)
    monkeypatch.setattr(F._idbd, "swipe", wallet.swipe)
    monkeypatch.setattr(screen_text, "read_text", wallet.read_text)
    fr = object.__new__(F.FlowRunner)
    fr._cur_udid = "PHONE"
    fr.devices = {"consumer": "PHONE"}
    fr._booked_slot = "12:20"
    return fr


def test_wallet_sees_this_runs_invitation_cancelled(monkeypatch):
    fr = wallet_runner(Wallet([("12:05", False), ("12:20", True)]), monkeypatch)
    notes = []
    assert fr._wallet_invite_cancelled(notes) is True, notes
    assert "12:20" in notes[-1]


def test_another_slots_cancelled_invitation_does_not_count(monkeypatch):
    fr = wallet_runner(Wallet([("12:05", True), ("12:20", False)]), monkeypatch)
    notes = []
    assert fr._wallet_invite_cancelled(notes) is False
    assert "no invitation for 12:20" in notes[-1]
