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


def test_a_slow_server_is_waited_for_not_tapped_twice(monkeypatch):
    """MEASURED 2026-10-17: BOOK NOW left the form and the Wallet came 1.5-2 min
    later (flow3 220s, book demo 126s). Re-tapping then risks a second booking."""
    import types
    clock = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))
    form = [{"label": "bookAppoitment", "id": "", "cx": 200, "cy": 800}]
    taps = []
    runner = FlowRunner.__new__(FlowRunner)
    monkeypatch.setattr(runner, "_clear_logbox", lambda *a, **k: 0, raising=False)
    monkeypatch.setattr(runner, "_idb_tap", lambda x, y, *a: taps.append((x, y)) or True,
                        raising=False)

    def screen(*_a):
        # Replays flow3: BOOK NOW lingers ~10s after the tap, the Wallet arrives at
        # ~2 min. The old 3-attempt loop tapped BOOK NOW again here (review 2026-10-19).
        if not taps or clock["t"] < 10:
            return form
        if clock["t"] < 120:
            return [{"label": "Reservation", "id": "", "cx": 0, "cy": 0}]   # submitting
        return [{"label": "walletUpcomingSearchInput", "id": "", "cx": 0, "cy": 0}]

    monkeypatch.setattr(runner, "_idb_els", screen, raising=False)
    notes = []
    assert runner._book_appointment(None, notes), notes
    assert len(taps) == 1
    assert any("creating the booking" in n for n in notes)


def test_a_failed_booking_request_fails_fast_without_a_second_tap(monkeypatch):
    """Staging fails the request after the tap and the form comes back (Reservation.js
    .catch -> loader off). Waiting out 150s for a Wallet that never comes wastes the
    step; tapping again risks a second booking if the server did make the first."""
    import types
    clock = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))
    form = [{"label": "bookAppoitment", "id": "", "cx": 200, "cy": 800}]
    taps = []
    runner = FlowRunner.__new__(FlowRunner)
    monkeypatch.setattr(runner, "_clear_logbox", lambda *a, **k: 0, raising=False)
    monkeypatch.setattr(runner, "_idb_tap", lambda x, y, *a: taps.append((x, y)) or True,
                        raising=False)
    screen = lambda *_a: (form if not taps or clock["t"] < 10 or clock["t"] > 40     # noqa: E731
                          else [{"label": "Reservation", "id": "", "cx": 0, "cy": 0}])
    monkeypatch.setattr(runner, "_idb_els", screen, raising=False)
    notes = []
    assert not runner._book_appointment(None, notes)
    assert len(taps) == 1 and "form came back" in notes[-1]
    assert clock["t"] < 60, "fails fast, not after the 150s wait"


def test_the_second_menu_tap_still_targets_the_booked_slot(monkeypatch):
    """Tap 1 lands while the Wallet list settles; on the re-read the booked 13:00
    card has moved to the top. Every booking carries the same 'RoopaDmenuOrderCard',
    so "the Menu nearest the old y" (640) would now be the OLDER 10:00 booking's
    (review 2026-10-19). The second tap must re-pick by the booked time."""
    import types
    clock = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))
    settled = [el("walletUpcomingSearchInput", 60), el("NylaiKitchen2Card", 120),
               el("13:00", 160), el("RoopaDmenuOrderCard", 300),
               el("10:00", 500), el("RoopaDmenuOrderCard", 640)]
    taps = []
    screens = lambda: WALLET if not taps else settled if len(taps) == 1 else [el("PennePolloInc")]  # noqa: E731
    monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: screens())
    monkeypatch.setattr(caf._idbd, "tap_el",
                        lambda udid, e, els=None, scroll=True:
                        taps.append((caf._idbd.name(e), caf._idbd.frame(e)[1])) or (True, "idb"))
    runner = FlowRunner.__new__(FlowRunner)
    runner.devices = {"consumer": "PHONE"}
    runner._booked_slot = "13:00"
    notes = []
    assert runner._after_booking("preOrderBooking", notes), notes
    assert taps == [("RoopaDmenuOrderCard", 640), ("RoopaDmenuOrderCard", 300)]
    assert "second tap" in notes[-1]


def test_no_second_tap_once_the_wallet_card_is_gone(monkeypatch):
    """The first tap navigated late: the Wallet card is gone. A second tap would
    land on (and scroll) the menu page that is opening."""
    import types
    clock = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))
    taps = []
    loading = [el("Menu loading", 100)]
    screens = lambda: WALLET if not taps else (loading if clock["t"] < 12 else [el("PennePolloInc")])  # noqa: E731
    monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: screens())
    monkeypatch.setattr(caf._idbd, "tap_el",
                        lambda udid, e, els=None, scroll=True: taps.append(e) or (True, "idb"))
    runner = FlowRunner.__new__(FlowRunner)
    runner.devices = {"consumer": "PHONE"}
    runner._booked_slot = "13:00"
    notes = []
    assert runner._after_booking("preOrderBooking", notes), notes
    assert len(taps) == 1 and "opened late" in notes[-1]


def test_pre_order_waits_for_the_wallet_to_list_the_new_booking(monkeypatch):
    """MEASURED 2026-10-19 (flow1, run bcac5b28): BOOK NOW confirmed in 6s and the
    Wallet came up before its list loaded -- search box and coupons, no cards. One
    read failed the step in 0.3s."""
    import types
    clock = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))
    loading = [el("couponBlock", 40), el("walletUpcomingSearchInput", 60), el("Wallet", 850)]
    taps = []

    def screen(udid):
        if taps:
            return [el("PennePolloInc")]
        return loading if clock["t"] < 3 else WALLET

    monkeypatch.setattr(caf._idbd, "describe_all", screen)
    monkeypatch.setattr(caf._idbd, "tap_el",
                        lambda udid, e, els=None, scroll=True:
                        taps.append((caf._idbd.name(e), caf._idbd.frame(e)[1])) or (True, "idb"))
    runner = FlowRunner.__new__(FlowRunner)
    runner.devices = {"consumer": "PHONE"}
    runner._booked_slot = "13:00"
    notes = []
    assert runner._after_booking("preOrderBooking", notes), notes
    assert taps == [("RoopaDmenuOrderCard", 640)]


def test_pre_order_taps_inside_a_button_whose_text_answers_the_hit_test(monkeypatch):
    """MEASURED 2026-10-21 (flow1/flow2): the open event's Pre-Order button was fully
    on screen, but describe-point at its centre returns its inner text, so tap_el
    refused it twice ('covered or scrolled out'). _tap_inside accepts a child that
    lies inside the button's own frame."""
    import types
    clock = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))
    points = []
    monkeypatch.setattr(caf._idbd, "describe_all",
                        lambda udid: [el("PennePolloInc")] if points else WALLET)
    monkeypatch.setattr(caf._idbd, "tap_el",
                        lambda udid, e, els=None, scroll=True: (False, "not reachable (covered or scrolled out)"))
    # The hit-test returns the button's own 'Pre-Order' text, inside its frame.
    monkeypatch.setattr(caf._idbd, "_probe",
                        lambda udid, e: el("Pre-Order", caf._idbd.frame(e)[1] + 5,
                                           caf._idbd.frame(e)[0] + 10, 40, 16))
    monkeypatch.setattr(caf._idbd, "tap_point",
                        lambda udid, x, y: points.append((x, y)) or (True, "idb"))
    runner = FlowRunner.__new__(FlowRunner)
    runner.devices = {"consumer": "PHONE"}
    runner._booked_slot = "13:00"
    notes = []
    assert runner._after_booking("preOrderBooking", notes), notes
    assert points == [(240.0, 655.0)]                  # the 13:00 card's Menu (x=200, y=640)
    assert "inside its frame" in notes[-1]
