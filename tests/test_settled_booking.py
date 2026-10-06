"""The waiter's settle steps when there is nothing left to settle.

Measured on flow1 (booking 4777, 17:25): the diner pre-ordered and paid by card
in the C-App. The waiter served, tapped notifyPaymentBtn -- and the booking went
straight to 'Completed' with the app back on the board. The flow then hunted a
'Yes' this build does not have (240s hang), and @pay / closeTableBtn had no bill
and no table left to act on.
"""
import pytest

from automation.scenarios import cross_app_flows as caf
from automation.scenarios.cross_app_flows import FlowRunner


def el(name, x=0, y=0, w=100, h=40, t="GenericElement"):
    return {"AXLabel": name, "type": t, "frame": {"x": x, "y": y, "width": w, "height": h}}


APP = {"AXLabel": "Vya Business", "type": "Application",
       "frame": {"x": 0, "y": 0, "width": 1210, "height": 834}}


@pytest.fixture
def screen(monkeypatch):
    def make(els, slot="17:25"):
        monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: [APP] + els)
        monkeypatch.setattr(caf.time, "sleep", lambda s: None)
        runner = FlowRunner.__new__(FlowRunner)
        runner.devices = {"waiter": "IPAD"}
        runner._booked_slot = slot
        runner._cur_udid = "IPAD"
        return runner
    return make


def test_completed_row_in_my_orders_means_settled(screen):
    r = screen([el("4777  COMPLETED 17:25 - 18:25 \U000f0a70 I2 17:24", 800, 300, 360, 80)])
    assert "4777" in r._already_settled() and "completed" in r._already_settled()


def test_payment_done_card_in_the_booked_hour_means_settled(screen):
    r = screen([el("17:00", 20, 450, 60, 20, "StaticText"),
                el("18:00", 20, 690, 60, 20, "StaticText"),
                el("RoopaDcardPaymentDone", 200, 455, 700, 190)])
    assert r._already_settled()


def test_a_card_in_another_hour_does_not_count(screen):
    r = screen([el("17:00", 20, 450, 60, 20, "StaticText"),
                el("18:00", 20, 690, 60, 20, "StaticText"),
                el("RoopaDcardCompleted", 200, 695, 700, 190)])
    assert r._already_settled() == ""


def test_an_open_bill_is_not_settled(screen):
    r = screen([el("E-Payment"), el("Cash"), el("Total")])
    assert r._already_settled() == ""


def test_close_table_on_screen_means_the_bill_is_paid(screen):
    # OrderSummary renders closeTableBtn only when payment_completed is true.
    # Measured on 4780: PRE-ORDERED items, Total, closeTableBtn, no methods.
    r = screen([el("PRE-ORDERED"), el("Total"), el("closeTableBtn")])
    assert "already paid" in r._already_settled()


def test_a_serve_booking_is_not_settled(screen):
    r = screen([el("4777  SERVE 17:25 - 18:25 I2 17:24", 800, 300)])
    assert r._already_settled() == ""


def test_pay_is_skipped_with_the_reason_when_settled(screen):
    r = screen([el("4777  COMPLETED 17:25 - 18:25 I2 17:24", 800, 300, 360, 80)])
    notes = []
    assert FlowRunner._handle_special(r, None, "@pay:epay", notes)
    assert notes[-1].startswith("[skip] @pay:epay") and "nothing left to pay" in notes[-1]


def test_optional_step_absent_is_skipped_quickly(screen, monkeypatch):
    r = screen([el("homeBtn")])
    clock = iter(range(0, 1000))
    monkeypatch.setattr(caf.time, "time", lambda: next(clock))
    monkeypatch.setattr(r, "_smart_click",
                        lambda *a: pytest.fail("nothing to tap"), raising=False)
    notes = []
    assert r._optional_step(None, "click Yes", notes)
    assert notes[-1].startswith("[skip] click Yes")


def test_optional_step_present_is_tapped(screen, monkeypatch):
    r = screen([el("Yes", 500, 400)])
    monkeypatch.setattr(r, "_smart_click", lambda rr, step: (True, "tapped Yes (idb)", False),
                        raising=False)
    notes = []
    assert r._optional_step(None, "click Yes", notes)
    assert notes[-1] == "[ok] click Yes — tapped Yes (idb)"


def test_notify_confirmation_is_optional_in_the_flows():
    from automation.scenarios.cross_app_flows import _W_SERVE_NOTIFY
    assert "?click Yes" in _W_SERVE_NOTIFY and "click Yes" not in _W_SERVE_NOTIFY
