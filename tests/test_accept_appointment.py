"""The 'accept the appointment' handler.

The step used to have no handler at all: it fell through to the fuzzy resolver and
burned the whole 150s STEP_TIMEOUT. These tests pin the two things that matter —
it is dispatched to a real handler, and it never calls a landed tap "accepted"
without the app's own state transition.

Element ids come from the Consumer app source, not from guesses:
  Wallet/Upcoming.js       '<Restaurant>InviteCard' opens the invitation modal
  Components/Modal/index.js 'eventAccept' is the ACCEPT button
  Wallet/index.js:463      accept moves the row out of `invitation` -> the
                           InviteCard disappears, toast 'Appointment updated successfully'
"""
import pytest

from automation.scenarios import cross_app_flows as F

CARD = "NylaiKitchen2InviteCard"
WALLET = ["walletUpcomingSearchInput", "upcomingBlock", "Home", "Wallet", "Menu"]


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    monkeypatch.setattr(F.time, "sleep", lambda *_: None)


class Screen:
    """A scripted device. `frames` is a list of label-lists; each tap advances it."""

    def __init__(self, frames):
        self.frames = [list(f) for f in frames]
        self.taps = []
        self.clicks = []

    def _cur(self):
        return self.frames[min(len(self.taps), len(self.frames) - 1)]

    def els(self, udid=""):
        return [{"id": "", "label": l, "type": "GenericElement",
                 "x": 0, "y": 0, "w": 100, "h": 40, "cx": 50, "cy": 20}
                for l in self._cur()]

    def tap(self, x, y, udid=""):
        self.taps.append((x, y))
        return True

    def smart_click(self, r, step):
        self.clicks.append(step)
        self.taps.append(("smart", step))
        return True, "tapped", False


def runner(frames):
    fr = object.__new__(F.FlowRunner)
    scr = Screen(frames)
    fr._idb_els = scr.els
    fr._idb_tap = scr.tap
    fr._clear_logbox = lambda *a, **k: 0
    fr._smart_click = scr.smart_click
    return fr, scr


def run(frames):
    fr, scr = runner(frames)
    notes = []
    return fr._accept_appointment(None, notes), notes, scr


def joined(notes):
    return " ".join(notes)


# ── dispatch ─────────────────────────────────────────────────────────────────
def test_the_plain_english_step_reaches_a_dedicated_handler():
    assert F.FlowRunner._PLAIN_STEP_TOKENS["accept the appointment"] == "@accept_appointment"
    assert hasattr(F.FlowRunner, "_accept_appointment")


def test_the_flow_block_still_uses_the_plain_wording():
    # The alias exists precisely so DB-stored flows keep working; don't silently
    # rewrite the step out of the flow definition.
    assert "accept the appointment" in F._C_ACCEPT_APPT


# ── nothing pending ──────────────────────────────────────────────────────────
def test_no_pending_appointment_fails_with_that_reason_not_a_timeout():
    ok, notes, scr = run([WALLET + ["NylaiKitchen2Card"]])
    assert ok is False
    assert "no pending appointment" in joined(notes)
    assert scr.taps == []                       # never blindly taps around


# ── happy path ───────────────────────────────────────────────────────────────
def test_accepts_and_confirms_the_invite_left_the_pending_list():
    ok, notes, scr = run([
        WALLET + [CARD],                              # pending
        WALLET + [CARD, "eventAccept", "eventDecline"],  # modal open
        WALLET + ["NylaiKitchen2Card", "Appointment updated successfully"],  # accepted
    ])
    assert ok is True
    assert "accepted" in joined(notes)
    assert len(scr.taps) == 2                     # open the card, then ACCEPT


def test_the_one_hour_booking_confirmed_modal_is_answered_with_order_later():
    # Flows 5/6 have the WAITER add the items, so pre-ordering here would be wrong.
    ok, notes, scr = run([
        WALLET + [CARD],
        WALLET + [CARD, "eventAccept"],
        WALLET + ["orderLater", "preOrderBooking", "appointmentId"],
        WALLET + ["NylaiKitchen2Card"],
    ])
    assert ok is True
    assert scr.clicks == ["click orderLater"]


# ── the failures that must NOT be silent ─────────────────────────────────────
def test_a_card_tap_that_never_opens_the_modal_fails_loudly():
    ok, notes, scr = run([WALLET + [CARD]])       # frozen: modal never renders
    assert ok is False
    assert "invitation modal never opened" in joined(notes)
    assert CARD in joined(notes)


def test_a_landed_accept_tap_is_not_acceptance():
    """The whole point: eventAccept still up and the invite still pending means the
    POST never landed, however well the tap itself went."""
    ok, notes, scr = run([
        WALLET + [CARD],
        WALLET + [CARD, "eventAccept"],
        WALLET + [CARD, "eventAccept"],           # tap landed, nothing moved
    ])
    assert ok is False
    assert "did not transition to the expected state" in joined(notes)


def test_an_invite_still_pending_after_the_final_modal_is_a_failure():
    """The belt-and-braces last check: the booking-confirmed modal came and went,
    so the tap clearly did something — but the invite is back in the pending list,
    which means it was never actually accepted."""
    ok, notes, scr = run([
        WALLET + [CARD],
        WALLET + [CARD, "eventAccept"],
        WALLET + ["orderLater", "appointmentId"],   # post-accept modal
        WALLET + [CARD],                            # ...and the invite is STILL pending
    ])
    assert ok is False
    assert "still 1 pending invitation(s) after ACCEPT (was 1)" in joined(notes)


def test_failure_notes_always_name_the_screen_for_tuning():
    for frames in ([WALLET], [WALLET + [CARD]]):
        ok, notes, _ = run(frames)
        assert ok is False
        assert "On screen:" in joined(notes)


def test_an_old_open_event_is_closed_and_the_invitation_waited_for():
    """MEASURED 2026-10-21 (flow5/flow6): the Wallet opened with the previous flow's
    Completed event expanded -- no list cards in the tree -- and one read failed the
    step although the waiter had just saved the invitation."""
    open_old = ["walletUpcomingSearchInput", "RoopaDClose", "NylaiKitchen2Card",
                "RoopaDorderCardCompleted", "Wallet", "Menu"]
    ok, notes, scr = run([
        open_old,                                     # an old event is expanded
        WALLET + [CARD],                              # closed: the invitation shows
        WALLET + [CARD, "eventAccept", "eventDecline"],
        WALLET + ["NylaiKitchen2Card", "Appointment updated successfully"],
    ])
    assert ok is True, notes
    assert "closed an open event" in joined(notes)
    assert len(scr.taps) == 3                     # Close, the card, ACCEPT


class TwoInvites(Screen):
    """Two pending invitations with the SAME label; the second is this run's (11:20).
    MEASURED 2026-10-22: a dashboard run that stopped after creating one left it
    pending, and accepting 'the first' then failed the label-based check."""

    def __init__(self):
        super().__init__([[]])
        self.state = "list"          # list -> modal(a|b) -> accepted

    def els(self, udid=""):
        def e(label, cy):
            return {"id": "", "label": label, "type": "GenericElement",
                    "x": 0, "y": cy - 20, "w": 300, "h": 40, "cx": 150, "cy": cy}
        if self.state == "list":
            return [e(l, 20) for l in WALLET] + [e(CARD, 300), e(CARD, 420)]
        if self.state == "modal-a":
            return [e("inviteclose", 100), e("TODAY 10:55 - Not Sure", 150), e("eventAccept", 600)]
        if self.state == "modal-b":
            return [e("inviteclose", 100), e("TODAY 11:20 - Not Sure", 150), e("eventAccept", 600)]
        return [e(l, 20) for l in WALLET] + [e(CARD, 300), e("Appointment updated successfully", 700)]

    def tap(self, x, y, udid=""):
        self.taps.append((x, y))
        if self.state == "list":
            self.state = "modal-a" if y == 300 else "modal-b"
        elif y == 100:                                   # inviteclose
            self.state = "list"
        elif y == 600:                                   # eventAccept
            self.accepted = self.state
            self.state = "accepted"
        return True


def test_with_several_invitations_the_one_for_this_slot_is_accepted():
    fr = object.__new__(F.FlowRunner)
    scr = TwoInvites()
    fr._idb_els = scr.els
    fr._idb_tap = scr.tap
    fr._clear_logbox = lambda *a, **k: 0
    fr._smart_click = scr.smart_click
    fr._booked_slot = "11:20"
    notes = []
    assert fr._accept_appointment(None, notes), notes
    assert scr.accepted == "modal-b"
    assert any("#2 is this run's (11:20)" in n for n in notes)


def test_a_cancelled_invitation_is_skipped_and_an_already_accepted_slot_passes(monkeypatch):
    """MEASURED 2026-10-22 (flow6, 11:45): the only card was a CANCELLED invitation (its
    tap does nothing) and this run's booking was already an accepted reservation."""
    from automation.scenarios import screen_text
    fr, scr = runner([WALLET + [CARD]])                 # tapping never opens anything
    fr._booked_slot = "11:45"
    monkeypatch.setattr(screen_text, "read_text", lambda udid, *a, **k: [
        ("TODAY", 36, 515, 40, 12), ("11:45", 36, 535, 40, 12),
        ("RESERVATION", 107, 535, 90, 12), ("PRE-ORDER NOW", 150, 560, 120, 14)])
    notes = []
    assert fr._accept_appointment(None, notes) is True
    assert "already accepted" in notes[-1]


def test_without_the_slot_on_screen_a_dead_invitation_still_fails(monkeypatch):
    from automation.scenarios import screen_text
    fr, scr = runner([WALLET + [CARD]])
    fr._booked_slot = "11:45"
    monkeypatch.setattr(screen_text, "read_text", lambda udid, *a, **k: [
        ("TODAY", 36, 315, 40, 12), ("11:15", 36, 335, 40, 12), ("EVENT CANCELLED", 140, 357, 120, 14)])
    notes = []
    assert fr._accept_appointment(None, notes) is False
    assert "never opened" in joined(notes)
