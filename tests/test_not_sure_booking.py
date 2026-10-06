"""Every scenario books with duration 'Not Sure' (asked 2026-10-01).

Only a 1 hr booking shows the pre-order / order-later dialog (Reservation.js);
'Not Sure' goes straight to the Wallet. Measured on the iPhone: booking MEPHSR,
13:00, 'Any .Not Sure', landed on the Wallet, and @book_appointment -- waiting for
the dialog -- retried 3 times and failed after 116s.
"""
import pytest

from automation.scenarios import cross_app_flows as caf
from automation.scenarios.cross_app_flows import FlowRunner


def el(name, y=100, x=20, w=80, h=30):
    return {"AXIdentifier": "", "AXLabel": name, "type": "GenericElement",
            "frame": {"x": x, "y": y, "width": w, "height": h}}


WALLET = [el("walletUpcomingSearchInput", 60), el("NylaiKitchen2Card", 120),
          el("10:00", 160), el("RoopaDmenuOrderCard", 300),          # an older booking
          el("13:00", 500), el("RoopaDCancelBookingCard", 640), el("RoopaDmenuOrderCard", 640, 200)]


def test_every_booking_picks_not_sure():
    # Duration is a slider now; Not Sure is its last stop (tests/test_duration_slider.py).
    assert "@duration:not_sure" in caf._C_BOOK_PREFIX and "select 1 hr" not in caf._C_BOOK_PREFIX


@pytest.fixture
def phone(monkeypatch):
    def go(screens, slot="13:00"):
        seq = list(screens)
        taps = []
        monkeypatch.setattr(caf._idbd, "describe_all",
                            lambda udid: seq.pop(0) if len(seq) > 1 else seq[0])
        monkeypatch.setattr(caf._idbd, "tap_el",
                            lambda udid, e, els=None, scroll=True:
                            taps.append((caf._idbd.name(e), caf._idbd.frame(e)[1])) or (True, "idb"))
        monkeypatch.setattr(caf._idbd, "tap",
                            lambda udid, names, els=None, scroll=True:
                            taps.append((names[0], 0)) or (True, "idb"))
        monkeypatch.setattr(caf.time, "sleep", lambda s: None)
        runner = FlowRunner.__new__(FlowRunner)
        runner.devices = {"consumer": "PHONE"}
        runner._booked_slot = slot
        return runner, taps
    return go


def test_order_later_needs_nothing_from_the_wallet(phone):
    runner, taps = phone([WALLET])
    notes = []
    assert runner._after_booking("orderLater", notes) and taps == []
    assert notes[-1].startswith("[skip] order later")


def test_pre_order_opens_the_menu_of_this_bookings_card(phone):
    runner, taps = phone([WALLET, [el("PennePolloInc")]])
    notes = []
    assert runner._after_booking("preOrderBooking", notes)
    assert taps == [("RoopaDmenuOrderCard", 640)], "the 13:00 card, not the older 10:00 one"


def test_the_1hr_dialog_is_still_used_when_it_shows(phone):
    runner, taps = phone([[el("preOrderBooking"), el("orderLater")]])
    assert runner._after_booking("preOrderBooking", []) and taps == [("preOrderBooking", 0)]


def test_the_plain_steps_route_to_it():
    assert FlowRunner._PLAIN_STEP_TOKENS["click orderLater"] == "@order_later"
    assert FlowRunner._PLAIN_STEP_TOKENS["click preOrderBooking"] == "@pre_order"


def test_landing_on_the_wallet_confirms_the_booking(monkeypatch):
    runner = FlowRunner.__new__(FlowRunner)
    monkeypatch.setattr(runner, "_idb_els",
                        lambda: [{"label": "walletUpcomingSearchInput", "id": "", "cx": 0, "cy": 0}],
                        raising=False)
    notes = []
    assert runner._book_appointment(None, notes)
    assert runner._booked_to_wallet
