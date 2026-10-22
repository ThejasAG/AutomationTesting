"""C-App Checkout on this run's booking. MEASURED 2026-10-07 on the iPhone: the
Wallet card's Checkout is `RoopaDfinishedCard` (UpcomingCard.js:4475) at y=834,
under the tab bar; there is no element named 'Checkout', and `...consumerCheckout`
has no onPress. Tapping it opened 'Payment / Select Payment Method'."""
import types

import pytest

from automation.scenarios import cross_app_flows as caf
from automation.scenarios.cross_app_flows import FLOWS, FlowRunner


def el(name, x, y, w=100, h=20):
    return {"AXLabel": name, "AXIdentifier": "", "type": "GenericElement",
            "frame": {"x": x, "y": y, "width": w, "height": h}}


class Wallet:
    def __init__(self, cards):
        self.cards, self.taps, self.sheet = cards, [], False      # cards: [(time, y)]

    def describe_all(self, udid):
        els = [el("Vya Consumer", 0, 0, 402, 874), el("walletUpcomingSearchInput", 67, 151)]
        for t, y in self.cards:
            els += [el(t, 216, y), el("RoopaDfinishedCard", 20, y + 535, 362, 51)]
        if self.sheet:
            els += [el("Select Payment Method", 20, 300), el("ePayment", 20, 340)]
        return els

    def tap_el(self, udid, e, els=None, scroll=True):
        self.taps.append(caf._idbd.frame(e)[1])
        self.sheet = True
        return True, "idb, scrolled into view"

    def tap(self, udid, names, els=None, scroll=True):
        return True, "idb"


@pytest.fixture
def run(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))

    def go(wallet, slot):
        for fn in ("describe_all", "tap_el", "tap"):
            monkeypatch.setattr(caf._idbd, fn, getattr(wallet, fn))
        r = FlowRunner.__new__(FlowRunner)
        r.devices = {"consumer": "IPHONE"}
        r._booked_slot = slot
        notes = []
        return r._consumer_checkout(None, notes), notes
    return go


def test_taps_checkout_of_the_booked_event_not_another(run):
    w = Wallet([("13:00", 299), ("15:20", 1200)])
    ok, notes = run(w, "15:20")
    assert ok and w.taps == [1735]                 # the 15:20 card's Checkout
    assert "15:20 booking" in notes[-1] and "Select Payment Method" in notes[-1]


def test_refuses_to_guess_between_bookings_without_a_booked_time(run):
    w = Wallet([("13:00", 299), ("15:20", 1200)])
    ok, notes = run(w, "")
    assert not ok and w.taps == [] and "cannot be told apart" in notes[-1]


def test_flows_use_it_and_old_steps_map_to_it():
    assert FlowRunner._PLAIN_STEP_TOKENS["click Checkout"] == "@consumer_checkout"
    assert "@consumer_checkout" in caf._C_PAY_FINAL and "click Checkout" not in caf._C_PAY_FINAL


# ── @consumer_epay: E-Payment → Stripe 'Pay €X' → paid ───────────────────────
class PaySheets:
    """MEASURED 2026-10-07: Select Payment Method → ePayment → Stripe sheet (TEST
    MODE, 'Visa ending in 4 2 4 2', 'Pay with Link', 'Pay €40.31') → paid: the
    card shows 'Left to pay 0.00€' and 'RoopaDorderCardCompleted'."""

    def __init__(self, result="ok", card_updates=True):
        self.state, self.result, self.card_updates, self.taps = "sheet", result, card_updates, []

    def describe_all(self, udid):
        els = [el("Vya Consumer", 0, 0, 402, 874)]
        if self.state == "sheet":
            els += [el("Select Payment Method", 20, 300), el("ePayment", 20, 340),
                    el("Total", 20, 600), el("40.31 €", 300, 600)]
        elif self.state == "stripe":
            els += [el("TEST MODE", 56, 517), el("Pay with Link", 20, 552),
                    el("Visa ending in 4 2 4 2", 130, 664), el("Pay €40.31", 20, 790)]
        elif self.state == "error":
            els.append(el("Error code: Failed", 40, 400))
        else:
            paid = self.card_updates
            els += [el("Left to pay", 35, 774), el("0.00€" if paid else "40.31€", 310, 774)]
            if paid:
                els.append(el("RoopaDorderCardCompleted", 20, 834))
        return els

    def tap(self, udid, names, els=None, scroll=True):
        self.taps.append(names[0])
        if names[0] == "ePayment":
            self.state = "stripe"
        return True, "idb"

    def tap_el(self, udid, e, els=None, scroll=True):
        self.taps.append(caf._idbd.name(e))
        self.state = "error" if self.result == "error" else "done"
        return True, "idb"


@pytest.fixture
def pay(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))

    def go(sheets):
        for fn in ("describe_all", "tap_el", "tap"):
            monkeypatch.setattr(caf._idbd, fn, getattr(sheets, fn))
        r = FlowRunner.__new__(FlowRunner)
        r.devices = {"consumer": "IPHONE"}
        notes = []
        return r._consumer_epay(notes), notes
    return go


def test_epay_taps_stripe_pay_not_link_and_proves_it_on_the_card(pay):
    s = PaySheets()
    ok, notes = pay(s)
    assert ok and s.taps == ["ePayment", "Pay €40.31"]
    assert "paid €40.31" in notes[-1]
    assert "Left to pay 0.00" in notes[-1] or "Completed" in notes[-1]


def test_epay_reports_stripes_error(pay):
    ok, notes = pay(PaySheets(result="error"))
    assert not ok and "Error code" in notes[-1]


def test_epay_does_not_trust_closed_sheets_alone(pay):
    ok, notes = pay(PaySheets(card_updates=False))
    assert not ok and "does not show it as paid" in notes[-1]


def test_pay_the_bill_segment_is_checkout_then_epay():
    assert caf._C_PAY_FINAL[-2:] == ["@consumer_checkout", "@consumer_epay"]


# ── @waiter_close_table: after the C-App payment ─────────────────────────────
def _closer(monkeypatch, settled, on_board=True):
    r = FlowRunner.__new__(FlowRunner)
    r._cur_udid = "IPAD"
    calls = []
    monkeypatch.setattr(r, "_already_settled", lambda: settled)
    monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: [] if on_board else
                        [{"AXLabel": "closeTableBtn", "frame": {}}])
    monkeypatch.setattr(r, "_open_reservation",
                        lambda rr, notes, statuses=(), what="": calls.append(statuses) or True)
    monkeypatch.setattr(r, "_close_table", lambda notes: calls.append("close") or True)
    return r, calls


def test_opens_the_paid_booking_then_closes_it(monkeypatch):
    """MEASURED 2026-10-07: 5077 read PAYMENT in My Orders; opened, Close Table."""
    r, calls = _closer(monkeypatch, "")
    assert r._waiter_close_table(None, []) is True
    assert "payment" in calls[0] and calls[-1] == "close"


def test_a_completed_booking_needs_no_close(monkeypatch):
    r, calls = _closer(monkeypatch, "booking 5077 (15:20) is already completed")
    notes = []
    assert r._waiter_close_table(None, notes) is True and calls == []
    assert "nothing to close" in notes[-1]


def test_c_app_flows_close_through_the_booking():
    assert caf._W_CLOSE == ["@waiter_close_table"]


# ── Paid in full: the pre-order covered the bill ─────────────────────────────
def test_checkout_passes_when_the_booking_is_already_paid_in_full(monkeypatch):
    """MEASURED 2026-10-17 (flow2, run 56bf81f4): the pre-order was paid by card, so
    after Notify Payment this run's event read 'RoopaDorderCardCompleted' with no
    Checkout. Nothing is left to pay -- that is a pass, and E-Payment skips."""
    open_event = [el("Vya Consumer", 0, 0, 402, 874), el("walletUpcomingSearchInput", 67, 151),
                  el("RoopaDClose", 300, 200), el("RoopaDorderCardCompleted", 20, 834)]
    monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: open_event)
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(time=lambda: 0.0, sleep=lambda s: None))
    r = FlowRunner.__new__(FlowRunner)
    r.devices = {"consumer": "IPHONE"}
    r._booked_slot = "11:55"
    monkeypatch.setattr(r, "_event_open", lambda es: ("TODAY", "11:55"), raising=False)
    monkeypatch.setattr(r, "_wait_els", lambda udid, pred, secs: (open_event, pred(open_event)),
                        raising=False)
    notes = []
    assert r._consumer_checkout(None, notes)
    assert "already paid in full" in notes[-1]
    notes = []
    assert r._consumer_epay(notes) and notes[-1].startswith("[skip] @consumer_epay")


def test_epay_taps_pay_again_when_the_sheet_ignored_the_first_tap(pay):
    """MEASURED 2026-10-17 (flow4): 'Pay €35.02' stayed on screen for 45s after the
    tap. With Pay still showing (not Stripe's spinner), select the card and retry."""
    class Deaf(PaySheets):
        ignored = False

        def tap_el(self, udid, e, els=None, scroll=True):
            n = caf._idbd.name(e)
            if n.startswith("Pay €") and not self.ignored:
                self.ignored = True
                self.taps.append(n)
                return True, "idb"
            if n.startswith("Visa"):
                self.taps.append(n)
                return True, "idb"
            return super().tap_el(udid, e, els, scroll)

    s = Deaf()
    ok, notes = pay(s)
    assert ok, notes
    assert s.taps == ["ePayment", "Pay €40.31", "Visa ending in 4 2 4 2", "Pay €40.31"]
    assert any("tapped Pay again" in n for n in notes)


def test_another_bookings_event_at_the_same_slot_is_not_taken_as_paid(monkeypatch):
    """The consumer account is shared on staging (MEASURED 2026-10-19: another
    diner's booking sat at this run's exact slot). A pre-ordered event prints its
    ticket; when that is not this run's, it must be skipped -- never called "paid in
    full" (and never paid)."""
    state = {"closed": False}
    theirs = [el("Vya Consumer", 0, 0, 402, 874), el("walletUpcomingSearchInput", 67, 151),
              el("RoopaDClose", 300, 200), el("TICKET: 5232", 200, 420),
              el("RoopaDorderCardCompleted", 20, 834)]
    wallet = [el("Vya Consumer", 0, 0, 402, 874), el("walletUpcomingSearchInput", 67, 151)]
    screen = lambda: wallet if state["closed"] else theirs                     # noqa: E731
    monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: screen())
    monkeypatch.setattr(caf._idbd, "swipe", lambda *a, **k: None)
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(time=lambda: 0.0, sleep=lambda s: None))
    r = FlowRunner.__new__(FlowRunner)
    r.devices = {"consumer": "IPHONE"}
    r._booked_slot, r._booked_ticket = "11:55", "5229"
    monkeypatch.setattr(r, "_event_open", lambda es: None if state["closed"] else ("TODAY", "11:55"),
                        raising=False)
    monkeypatch.setattr(r, "_event_code", lambda es: "ABCDEF", raising=False)
    monkeypatch.setattr(r, "_tap_inside",
                        lambda udid, e: state.__setitem__("closed", True) or (True, "idb"), raising=False)
    monkeypatch.setattr(r, "_wait_els", lambda udid, pred, secs: (screen(), pred(screen())),
                        raising=False)
    notes = []
    assert not r._consumer_checkout(None, notes)
    assert state["closed"], "their event must be closed, not used"
    assert not getattr(r, "_consumer_paid_in_full", False)
    assert "(ticket 5232)" in notes[-1]


def test_epay_taps_inside_pay_when_its_text_answers_the_hit_test(monkeypatch, pay):
    """MEASURED 2026-10-22 (flow6): 'Pay €35.02' was plainly on screen but the verified
    tap refused it ('covered or scrolled out'); its own text answers the hit-test."""
    s = PaySheets()
    refuse = {"on": True}

    def tap_el(udid, e, els=None, scroll=True):
        if caf._idbd.name(e).startswith("Pay €") and refuse["on"]:
            return False, "not reachable (covered or scrolled out)"
        return PaySheets.tap_el(s, udid, e, els, scroll)

    def tap_point(udid, x, y):
        s.taps.append("Pay (inside)")
        s.state = "done"
        return True, "idb"

    monkeypatch.setattr(caf._idbd, "_probe",
                        lambda udid, e: el("Pay", caf._idbd.frame(e)[0] + 5, caf._idbd.frame(e)[1] + 2, 30, 14))
    monkeypatch.setattr(caf._idbd, "tap_point", tap_point)
    for fn in ("describe_all", "tap"):
        monkeypatch.setattr(caf._idbd, fn, getattr(s, fn))
    monkeypatch.setattr(caf._idbd, "tap_el", tap_el)
    r = FlowRunner.__new__(FlowRunner)
    r.devices = {"consumer": "IPHONE"}
    notes = []
    assert r._consumer_epay(notes), notes
    assert s.taps == ["ePayment", "Pay (inside)"]
