"""@decline_appointment and @wallet_event_shows:<text> (AI Agent drafts
cc_decline_invite / cc_book_two_hours).

Element ids come from the Consumer app source:
  Wallet/Upcoming.js        '<Restaurant>InviteCard' opens the invitation modal
  Components/Modal/index.js 'eventDecline' / 'eventAccept', 'inviteclose'
  Wallet/UpcomingCard.js    an open event prints 'TODAY' + 'HH:MM' at the top,
                            '<User>Close', and '<area> .<duration>' ('Any .2 hr')
"""
import pytest

from automation.scenarios import cross_app_flows as F

CARD = "NylaiKitchen2InviteCard"
WALLET = ["walletUpcomingSearchInput", "upcomingBlock", "Home", "Wallet", "Menu"]


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    monkeypatch.setattr(F.time, "sleep", lambda *_: None)


def _e(label, cy):
    return {"id": "", "label": label, "type": "GenericElement",
            "x": 0, "y": cy - 20, "w": 300, "h": 40, "cx": 150, "cy": cy}


class Invites:
    """Two pending invitations with the same label; 'b' (11:20) is this run's.
    `after` is what the list shows once 'b' is declined."""

    def __init__(self, after="gone", decline_lands=True):
        self.state = "list"
        self.after = after
        self.decline_lands = decline_lands
        self.declined = None
        self.taps = []

    def els(self, udid=""):
        if self.state == "list":
            rows = [_e(CARD, 300)]
            if self.declined is None or self.after != "gone":
                rows.append(_e(CARD, 420))
            return [_e(l, 20) for l in WALLET] + rows
        slot = "10:55" if self.state == "modal-a" else "11:20"
        out = [_e("inviteclose", 100), _e(f"TODAY {slot} - Not Sure", 150)]
        if not (self.state == "modal-b" and self.declined):
            out += [_e("eventDecline", 600), _e("eventAccept", 650)]
        return out

    def tap(self, x, y, udid=""):
        self.taps.append((x, y))
        if self.state == "list":
            self.state = "modal-a" if y == 300 else "modal-b"
        elif y == 100:                                   # inviteclose
            self.state = "list"
        elif y == 600 and self.decline_lands:            # eventDecline
            self.declined = self.state
            self.state = "list"
        return True


def runner(scr, slot="11:20"):
    fr = object.__new__(F.FlowRunner)
    fr._idb_els = scr.els
    fr._idb_tap = scr.tap
    fr._clear_logbox = lambda *a, **k: 0
    fr._booked_slot = slot
    return fr


def test_declines_the_invitation_for_this_runs_slot_and_it_leaves_the_list():
    scr = Invites()
    notes = []
    assert runner(scr)._decline_appointment(None, notes), notes
    assert scr.declined == "modal-b"                     # never the 10:55 one
    assert "left the pending list (2 → 1)" in notes[-1]


def test_a_declined_row_that_stays_listed_must_no_longer_offer_decline():
    scr = Invites(after="stays")
    notes = []
    assert runner(scr)._decline_appointment(None, notes), notes
    assert "no longer offers ACCEPT/DECLINE" in notes[-1]


def test_never_declines_someone_elses_invitation():
    scr = Invites()
    notes = []
    assert runner(scr, slot="12:40")._decline_appointment(None, notes) is False
    assert scr.declined is None
    assert "nothing declined" in notes[-1]


def test_refuses_without_a_booked_slot():
    scr = Invites()
    notes = []
    assert runner(scr, slot="")._decline_appointment(None, notes) is False
    assert scr.taps == []


def test_a_landed_decline_tap_that_changes_nothing_fails():
    scr = Invites(decline_lands=False)
    notes = []
    assert runner(scr)._decline_appointment(None, notes) is False
    assert "modal stayed up" in notes[-1]


def test_the_step_is_dispatched():
    fr = runner(Invites())
    called = []
    fr._decline_appointment = lambda r, n: called.append("d") or True
    fr._wallet_event_shows = lambda w, n: called.append(w) or True
    assert fr._handle_special(None, "@decline_appointment", [])
    assert fr._handle_special(None, "@wallet_event_shows:2 hr", [])
    assert called == ["d", "2 hr"]


# ── @wallet_event_shows ──────────────────────────────────────────────────────
def _ax(label, y, h=40):
    return {"AXLabel": label, "type": "GenericElement",
            "frame": {"x": 0, "y": y, "width": 300, "height": h}}


class Wallet:
    """Two NylaiKitchen2 events TODAY at 11:20: the first is another diner's
    (Not Sure), the second this run's (2 hr)."""

    def __init__(self, durations=("Any .Not Sure", "Any .2 hr")):
        self.durations = durations
        self.open = None

    def describe_all(self, udid):
        app = {"AXLabel": "", "type": "Application",
               "frame": {"x": 0, "y": 0, "width": 390, "height": 844}}
        if self.open is None:
            return [app, _ax("walletUpcomingSearchInput", 120),
                    _ax("NylaiKitchen2Card", 300), _ax("NylaiKitchen2Card", 450)]
        return [app, _ax("RoopaDClose", 200), _ax("TODAY", 240, 20), _ax("11:20", 240, 20),
                _ax(self.durations[self.open], 700)]


@pytest.fixture
def wallet(monkeypatch):
    def make(**kw):
        w = Wallet(**kw)
        monkeypatch.setattr(F._idbd, "describe_all", w.describe_all)
        monkeypatch.setattr(F._idbd, "swipe", lambda *a, **k: None)
        monkeypatch.setattr(F._idbd, "tap", lambda *a, **k: (True, ""))
        fr = object.__new__(F.FlowRunner)
        fr.devices = {"consumer": "SIM"}
        fr._booked_slot = "11:20"

        def tap_inside(udid, e):
            y = F._idbd.frame(e)[1]
            if w.open is None:
                w.open = 0 if y == 300 else 1
            else:
                w.open = None                            # the Close button
            return True, ""
        fr._tap_inside = tap_inside
        fr._wait_els = lambda udid, pick, secs: (w.describe_all(udid), pick(w.describe_all(udid)))
        return fr, w
    return make


def test_finds_this_runs_event_past_another_diners_at_the_same_slot(wallet):
    fr, w = wallet()
    notes = []
    assert fr._wallet_event_shows("2 hr", notes), notes
    assert "'Any .2 hr'" in notes[-1]


def test_fails_when_no_event_at_the_slot_has_the_text(wallet):
    fr, w = wallet(durations=("Any .Not Sure", "Any .1 hr"))
    notes = []
    assert fr._wallet_event_shows("2 hr", notes) is False
    assert "no Wallet event TODAY 11:20 reads '2 hr'" in notes[-1]


def test_twelve_hours_is_not_two(wallet):
    fr, w = wallet(durations=("Any .12 hr", "Any .12 hr"))
    assert fr._wallet_event_shows("2 hr", []) is False
