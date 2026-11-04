"""Paying on the waiter's order screen (Business app, PaymentDetails.js).

Measured 2026-09-29, segment 4 of 'Order later -> waiter adds items -> pay in
B-App': "@pay:epay — payment method not found (TUNE)" after 40s. The methods sit
inside the diner's card ('R RoopaDaccordionCard'), which was never opened, and
E-Payment is 'epaymentBtn' (lower-case p), which the lookup never tried.
"""
import types

import pytest

from automation.scenarios import cross_app_flows as caf
from automation.scenarios.cross_app_flows import FlowRunner


def el(name, x, y, w=60, h=30, t="GenericElement", value=None):
    e = {"AXIdentifier": "", "AXLabel": name, "type": t,
         "frame": {"x": x, "y": y, "width": w, "height": h}}
    if value is not None:
        e["AXValue"] = value
    return e


class PaymentScreen:
    def __init__(self, total="36.05", confirm_works=True, stays_open=True):
        self.total, self.confirm_works, self.stays_open = total, confirm_works, stays_open
        self.confirm_delay = 0
        self.open, self.pad, self.paid = False, False, False
        self.typed, self.inputs, self.log = "", [], []

    def describe_all(self, udid):
        els = [el("Vya Business", 0, 0, 1210, 834, "Application"),
               el("R RoopaDaccordionCard ", 834, 146, 336, 49),
               el("0", 999, 68, 21, 12, "StaticText"),              # header guest count
               el("Total", 827, 715, 56, 73, "StaticText"),
               el(f"{self.total}  €", 1079, 715, 99, 73, "StaticText")]
        if self.paid:
            els.append(el("closeTableBtn", 300, 740, 200, 60))
            return els
        if self.open:
            els += [el("epaymentBtn", 850, 300), el("cashPaymentBtn", 950, 300),
                    el("foodVoucherBtn", 1050, 300)]
            if self.inputs and self.confirm_delay:
                self.confirm_delay -= 1               # not rendered on this read yet
            elif self.inputs:
                els.append(el("paymentConfirmBtn", 1000, 560, 170, 60))
        if self.pad:
            els.append(el("userAmountInput", 850, 300, 300, 40, "TextField",
                          value=f"{self.typed or 0} €"))
            for i, d in enumerate("123456789"):
                els.append(el(d, 850 + (i % 3) * 100, 380 + (i // 3) * 60, 20, 20, "StaticText"))
            els += [el("", 850, 560, 30, 20), el("0", 950, 560, 20, 20, "StaticText"),
                    el("", 1050, 560, 30, 20), el("userInputBtn", 900, 640, 200, 60)]
        return els

    def tap_el(self, udid, e, els=None, scroll=True):
        n = e["AXLabel"]
        self.log.append(n)
        if "accordionCard" in n:
            self.open = not self.open
        elif n in ("epaymentBtn", "cashPaymentBtn", "foodVoucherBtn"):
            self.pad = True
        elif self.pad and n.isdigit():
            self.typed += n
        elif self.pad and n == "":
            self.typed += "."
        elif self.pad and n == "":
            self.typed = self.typed[:-1]
        elif n == "paymentConfirmBtn" and self.confirm_works:
            self.paid = True
        return True, "idb"

    def tap(self, udid, names, els=None, scroll=True):
        if names == ["userInputBtn"] and self.pad:
            self.log.append("userInputBtn")
            self.inputs.append(self.typed)
            self.pad = False
            self.open = self.stays_open               # measured: it stays open
            self.confirm_delay = 2                    # Confirm renders a moment later
            return True, "idb"
        return False, "not on screen"


@pytest.fixture
def pay(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))

    def go(screen, method="epay", **state):
        monkeypatch.setattr(caf._idbd, "describe_all", screen.describe_all)
        monkeypatch.setattr(caf._idbd, "tap_el", screen.tap_el)
        monkeypatch.setattr(caf._idbd, "tap", screen.tap)
        runner = FlowRunner.__new__(FlowRunner)
        runner._cur_udid = "IPAD"
        for k, v in state.items():
            setattr(runner, k, v)
        notes = []
        return runner._pay_business(method, notes), notes
    return go


def test_epay_opens_the_card_enters_the_total_and_confirms(pay):
    s = PaymentScreen()
    ok, notes = pay(s)
    assert ok and s.paid
    assert s.inputs == ["36.05"]                       # exactly the bill total
    assert s.log[0].startswith("R RoopaDaccordionCard")
    assert s.log[1] == "epaymentBtn"
    # The card stays open after Input: it must NOT be tapped (that closed it).
    assert s.log[-2] == "userInputBtn" and s.log[-1] == "paymentConfirmBtn"
    assert "total €36.05, paid €36.05 by epay" in notes[-1] and "accepted" in notes[-1]


def test_the_header_zero_is_never_typed(pay):
    s = PaymentScreen(total="40.00")
    ok, _ = pay(s)
    assert ok and s.inputs == ["40.00"]


def test_cash_tenders_over_the_bill_and_reports_the_change(pay):
    s = PaymentScreen()
    ok, notes = pay(s, "cash")
    assert ok and s.inputs == [f"{36.05 + caf.PAY_OVER_BY:.2f}"]
    assert f"change {caf.PAY_OVER_BY:.2f} €" in notes[-1]


def test_no_payment_section_fails_fast_with_the_reason(pay, monkeypatch):
    s = PaymentScreen()
    monkeypatch.setattr(s, "tap_el", lambda udid, e, els=None, scroll=True: (True, "idb"))
    ok, notes = pay(s)
    assert not ok and "no payment methods appeared" in notes[-1]


def test_the_token_goes_to_the_business_payment(monkeypatch):
    called = {}
    monkeypatch.setattr(FlowRunner, "_skip_if_settled", lambda self, step, notes: False)
    monkeypatch.setattr(FlowRunner, "_pay_business",
                        lambda self, m, notes: called.setdefault("m", m) or True)
    assert FlowRunner._handle_special(FlowRunner.__new__(FlowRunner), None, "@pay:epay", [])
    assert called["m"] == "epay"


def test_a_card_that_did_fold_is_reopened_for_confirm(pay):
    s = PaymentScreen(stays_open=False)
    ok, _ = pay(s)
    assert ok and s.paid
    assert s.log[-2].startswith("R RoopaDaccordionCard") and s.log[-1] == "paymentConfirmBtn"


# -- pay for all: the app settles and closes the booking itself -----------------------
# Measured 2026-10-01 on 4954 (Roopa D paid for 3 guests, 90.56 EUR): after Confirm
# the app showed the receipt ('TICKET CLIENT'), then My Bookings, and the booking
# read COMPLETED -- no Close Table button ever came.

def test_the_receipt_counts_as_settled():
    assert FlowRunner._settled_screen([el("TICKET CLIENT", 839, 193)]) == "receipt"
    assert FlowRunner._settled_screen([el("My Bookings", 200, 40)]) == "board"
    assert FlowRunner._settled_screen([el("My Bookings", 200, 40),
                                       el("paymentConfirmBtn", 969, 479)]) == ""


def test_close_table_accepts_a_booking_the_app_closed_itself(monkeypatch):
    monkeypatch.setattr(caf._idbd, "describe_all",
                        lambda udid: [el("TICKET CLIENT", 839, 193), el("90.56 €", 1110, -53)])
    monkeypatch.setattr(caf.time, "sleep", lambda s: None)
    runner = FlowRunner.__new__(FlowRunner)
    runner._cur_udid = "IPAD"
    monkeypatch.setattr(FlowRunner, "_pay_business",
                        lambda self, m, notes: pytest.fail("must not pay a second time"))
    notes = []
    assert runner._close_table(notes, wait=2.0)
    assert "closed the order itself (receipt shown)" in notes[-1]


# -- the card folds after Input and the amount is lost: reopen, enter again ----------

class FoldingScreen(PaymentScreen):
    """The amount shows under E-Payment once applied; the first Input folds the
    card and loses it (0 €), as described 2026-10-01."""

    def __init__(self):
        super().__init__(stays_open=False)
        self.applied, self.lose_first = 0.0, True

    def describe_all(self, udid):
        els = super().describe_all(udid)
        if self.open and not self.pad and not self.paid:
            els.append(el(f"{self.applied:g} €", 850, 335, 60, 16, "StaticText"))
        return els

    def tap(self, udid, names, els=None, scroll=True):
        typed = self.typed
        out = super().tap(udid, names, els, scroll)
        if names == ["userInputBtn"]:
            if self.lose_first:
                self.lose_first = False
            else:
                self.applied = float(typed)
            self.typed = ""
        return out


def test_a_lost_amount_is_entered_again_after_reopening_the_card(pay):
    s = FoldingScreen()
    ok, notes = pay(s)
    assert ok and s.paid
    assert s.inputs == ["36.05", "36.05"], "entered once, lost, entered again"
    assert any("the profile card folded — reopened it" in n for n in notes)
    assert any("the payment shows 0.00 € after Input of 36.05 € — entering it again" in n
               for n in notes)
    assert s.log[-1] == "paymentConfirmBtn"


# -- 2026-10-19: Confirm ACCEPTED, booking never settles (pay for all) --------------

class AcceptsButNeverSettles(FoldingScreen):
    """MEASURED 2026-10-19 on booking 5237: Confirm took the payment -- the app
    zeroed the entered 85.80 € within ~4s (onConfirmPay on a 2xx) -- but the booking
    stayed PAYMENT and Close Table never came."""

    def __init__(self):
        super().__init__()
        self.lose_first = False

    def tap_el(self, udid, e, els=None, scroll=True):
        if e["AXLabel"] == "paymentConfirmBtn":
            self.log.append("paymentConfirmBtn")
            self.applied = 0.0
            return True, "idb"
        return super().tap_el(udid, e, els, scroll)


def _fake_clock(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))


def test_a_confirm_that_clears_the_amount_is_accepted_and_remembered(monkeypatch):
    _fake_clock(monkeypatch)
    s = AcceptsButNeverSettles()
    for fn in ("describe_all", "tap_el", "tap"):
        monkeypatch.setattr(caf._idbd, fn, getattr(s, fn))
    runner = FlowRunner.__new__(FlowRunner)
    runner._cur_udid = "IPAD"
    notes = []
    assert runner._pay_business("epay", notes)
    assert s.inputs == ["36.05"] and s.log.count("paymentConfirmBtn") == 1
    assert "accepted (the app cleared the entered amount)" in notes[-1]
    assert runner._payment_accepted == 36.05


class GuestCardBelow(PaymentScreen):
    """MEASURED 2026-10-19 (run 309869d5): after Pay For, each paid-for guest's card
    prints 'Total 0€', and in content space it sits BELOW the payer's card and the
    footer -- the bottom-most 'Total' read 0.00 and 0.00 was entered."""

    def describe_all(self, udid):
        els = super().describe_all(udid)
        if self.open:
            els += [el("Total", 840, 363, 37, 18, "StaticText"),
                    el(f"{self.total} €", 1108, 363, 58, 18, "StaticText"),
                    el("G Guest1accordionCard ", 834, 760, 336, 49),
                    el("Total", 840, 830, 37, 18, "StaticText"),
                    el("0€", 1120, 830, 30, 18, "StaticText")]
        return els


def test_the_bill_is_the_payers_own_card_total_not_a_paid_for_guests_zero(pay):
    s = GuestCardBelow(total="85.80")
    ok, notes = pay(s)
    assert ok and s.inputs == ["85.80"]


def test_close_table_never_repays_an_accepted_payment(monkeypatch):
    """When one profile pays for others, staging ignores an amount equal to the bill
    (server event log: paidAmount 0 for all 13 such payments since 10-01); paying the
    same amount again is ignored again. After an ACCEPTED payment, report instead."""
    _fake_clock(monkeypatch)
    screen = [el("Vya Business", 0, 0, 1210, 834, "Application"),
              el("epaymentBtn", 850, 300), el("paymentConfirmBtn", 1000, 560, 170, 60)]
    monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: screen)
    runner = FlowRunner.__new__(FlowRunner)
    runner._cur_udid = "IPAD"
    runner._payment_accepted = 85.80
    runner._paid_for_others = ["Guest 1", "Guest 2", "Guest 3"]
    monkeypatch.setattr(runner, "_pay_business",
                        lambda *a, **k: pytest.fail("must not pay again"), raising=False)
    notes = []
    assert not runner._close_table(notes, wait=2.0)
    assert "accepted the payment of 85.80 €" in notes[-1]
    assert "staging ignores a payment equal to such a bill" in notes[-1]
    assert "Server bug, not the test" in notes[-1]


def test_paying_for_others_tenders_one_cent_over_the_bill(pay):
    """User decision 2026-10-27: staging ignores a pay-for-all E-Payment that EQUALS
    the bill (known_issues.md) but settles any amount above it, so the payer who took
    over the others' share pays the bill + 0.01 and the flow can pass."""
    s = GuestCardBelow(total="85.80")
    ok, notes = pay(s, _paid_for_others=["Guest 1", "Guest 2", "Guest 3"])
    assert ok and s.inputs == [f"{85.80 + caf.PAY_FOR_OTHERS_OVER_BY:.2f}"] == ["85.81"]
    assert "paying for others — 85.81 € for a bill of 85.80 €" in " ".join(notes)
    assert "change 0.01 €" in notes[-1]


def test_a_single_payer_still_pays_exactly_the_bill(pay):
    s = GuestCardBelow(total="85.80")
    ok, notes = pay(s)
    assert ok and s.inputs == ["85.80"] and "change" not in notes[-1]


class TwoOpenCards(PaymentScreen):
    """The participant's card, paid a moment ago, still open ABOVE the next payer's
    (Guest 5) on the panel: both show a cash button and a Confirm. The cards keep
    their own open state (Components/Accordion/index.js:13-25), and a guest's buttons
    are the paid* ones (PaymentDetails:3698-3829). The review of 2026-11-02."""

    def describe_all(self, udid):
        els = [e for e in super().describe_all(udid)
               if "accordionCard" not in e["AXLabel"]]
        els += [el("N NooluNagaaccordionCard ", 834, 146, 336, 49),
                el("cashPaymentBtn", 950, 220), el("paymentConfirmBtn", 1000, 260, 170, 60),
                el("G Guest5accordionCard ", 834, 290, 336, 49)]
        if self.open:
            els = [e for e in els if e["AXLabel"] not in ("epaymentBtn", "cashPaymentBtn",
                                                          "foodVoucherBtn", "paymentConfirmBtn")
                   or e["frame"]["y"] < 290]
            els += [el("paidCashPaymentBtn", 950, 400)]
            if any(e["AXLabel"] == "paymentConfirmBtn" and e["frame"]["y"] == 560
                   for e in super().describe_all(udid)):
                els.append(el("paymentConfirmBtn", 1000, 560, 170, 60))
            els += [el("Total", 840, 363, 37, 18, "StaticText"),
                    el(f"{self.total} €", 1108, 363, 58, 18, "StaticText")]
        return els

    def tap_el(self, udid, e, els=None, scroll=True):
        n = e["AXLabel"]
        if n == "paidCashPaymentBtn":
            self.log.append(n)
            self.pad = True
            return True, "idb"
        if n == "paymentConfirmBtn" and e["frame"]["y"] != 560:
            self.log.append("Noolu's Confirm")
            return True, "idb"
        if n == "cashPaymentBtn":
            self.log.append("Noolu's cash")
            return True, "idb"
        if "NooluNaga" in n:
            self.log.append(n)
            return True, "idb"
        return super().tap_el(udid, e, els, scroll)


def test_the_next_payer_pays_on_their_own_card_not_one_left_open_above(pay):
    s = TwoOpenCards(total="20.00")
    ok, notes = pay(s, "cash", _payer_card="Guest5accordionCard")
    assert ok and s.paid, notes
    assert "Noolu's cash" not in s.log and "Noolu's Confirm" not in s.log
    assert "paidCashPaymentBtn" in s.log and s.log[-1] == "paymentConfirmBtn"


class EmptyReadAfterConfirm(PaymentScreen):
    """Confirm did not register, and the first read after it timed out ([])."""

    def __init__(self):
        super().__init__(confirm_works=False)
        self.blank = False

    def tap_el(self, udid, e, els=None, scroll=True):
        out = super().tap_el(udid, e, els, scroll)
        if e["AXLabel"] == "paymentConfirmBtn":
            self.blank = True
        return out

    def describe_all(self, udid):
        if self.blank:
            self.blank = False
            return []
        return super().describe_all(udid)


def test_an_empty_read_after_confirm_is_not_taken_for_accepted(pay):
    ok, notes = pay(EmptyReadAfterConfirm())
    assert "did not react in 45s" in notes[-1], notes[-1]


class ReloadsAfterInput(PaymentScreen):
    """A guest's Input reloads the panel: a spinner in place of every card for ~20s,
    then the card back FOLDED (handlePayingForBusiness, PaymentDetails.js:4838)."""

    def __init__(self, clock):
        super().__init__(stays_open=False)
        self.clock, self.spin_until = clock, None

    def tap(self, udid, names, els=None, scroll=True):
        out = super().tap(udid, names, els, scroll)
        if names == ["userInputBtn"]:
            self.spin_until = self.clock["t"] + 20.0
        return out

    def describe_all(self, udid):
        if self.spin_until is not None and self.clock["t"] < self.spin_until:
            return [el("Vya Business", 0, 0, 1210, 834, "Application"),
                    el("", 900, 400, 40, 40, "ActivityIndicator")]
        return super().describe_all(udid)


def test_a_panel_reload_after_input_is_waited_out_and_the_card_reopened(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))
    s = ReloadsAfterInput(clock)
    for fn in ("describe_all", "tap_el", "tap"):
        monkeypatch.setattr(caf._idbd, fn, getattr(s, fn))
    runner = FlowRunner.__new__(FlowRunner)
    runner._cur_udid = "IPAD"
    notes = []
    assert runner._pay_business("cash", notes), notes
    assert s.paid and s.log[-1] == "paymentConfirmBtn"
    assert any("folded — reopened it" in n for n in notes)


class SlidRow(PaymentScreen):
    """The card's tiles left slid sideways by the discount step: E-Payment at x 758, off a
    card starting at 834 (MEASURED 2026-11-04, run ce8ba9e5). A tap there opens nothing."""

    def __init__(self):
        super().__init__()
        self.open, self.shift = True, 89.5

    def describe_all(self, udid):
        els = super().describe_all(udid)
        for e in els:
            if e["AXLabel"] in ("epaymentBtn", "cashPaymentBtn", "foodVoucherBtn"):
                e["frame"]["x"] -= self.shift
        return els

    def swipe(self, udid, x0, y0, x1, y1, duration=0.25):
        self.shift = min(89.5, max(0.0, self.shift + (x0 - x1)))

    def tap_el(self, udid, e, els=None, scroll=True):
        if e["AXLabel"] == "epaymentBtn" and e["frame"]["x"] < 834:
            self.log.append("off the card")
            return False, "not reachable (covered or scrolled out)"
        return super().tap_el(udid, e, els, scroll)


def test_a_payment_tile_slid_off_the_card_is_brought_back_before_the_tap(pay, monkeypatch):
    s = SlidRow()
    monkeypatch.setattr(caf._idbd, "swipe", s.swipe)
    ok, notes = pay(s)
    assert ok and s.paid, notes
    assert "off the card" not in s.log and s.shift == 0


class SlowPad(PaymentScreen):
    """A loaded iPad: each read takes 10s, and the keypad shows from the second read
    after the E-Payment tap (MEASURED 2026-11-04, run 9b339cbc: the keypad was up, the
    8s wait had looked once)."""

    def __init__(self, clock):
        super().__init__()
        self.clock, self.hidden = clock, 0

    def describe_all(self, udid):
        self.clock["t"] += 10.0
        els = super().describe_all(udid)
        if self.pad and self.hidden:
            self.hidden -= 1
            return [e for e in els if e["AXLabel"] not in ("userInputBtn", "userAmountInput")]
        return els

    def tap_el(self, udid, e, els=None, scroll=True):
        if e["AXLabel"] == "epaymentBtn":
            self.hidden = 1
        return super().tap_el(udid, e, els, scroll)


def test_a_keypad_slower_than_one_read_is_still_found(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))
    s = SlowPad(clock)
    for fn in ("describe_all", "tap_el", "tap"):
        monkeypatch.setattr(caf._idbd, fn, getattr(s, fn))
    runner = FlowRunner.__new__(FlowRunner)
    runner._cur_udid = "IPAD"
    notes = []
    assert runner._pay_business("epay", notes), notes
    assert s.paid and "keypad did not open" not in " ".join(notes)
