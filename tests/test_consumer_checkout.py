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
