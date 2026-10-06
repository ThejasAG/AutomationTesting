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

    def go(screen, method="epay"):
        monkeypatch.setattr(caf._idbd, "describe_all", screen.describe_all)
        monkeypatch.setattr(caf._idbd, "tap_el", screen.tap_el)
        monkeypatch.setattr(caf._idbd, "tap", screen.tap)
        runner = FlowRunner.__new__(FlowRunner)
        runner._cur_udid = "IPAD"
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
    assert any("did not stay on the payment (0 €) — entering it again" in n for n in notes)
    assert s.log[-1] == "paymentConfirmBtn"
