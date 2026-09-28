"""Opening a booking from the hour's events list reaches rows below the fold.

Measured (waiter, 16:00 events list): the list is not sorted by start time --
4770 16:05, 4772 16:15, 4771 16:15, 4769, 4768 ... and the 16:35 booking (4773)
further DOWN. The step found the 4773 row in the tree, tapped its reported
centre -- below the visible part of the sheet -- and "the reservation did not
open"; the segment then timed out after 240s.
"""
import pytest

from automation.scenarios import cross_app_flows as caf
from automation.scenarios.cross_app_flows import FlowRunner

APP = {"AXLabel": "Vya Business", "type": "Application",
       "frame": {"x": 0, "y": 0, "width": 1210, "height": 834}}


def row(ticket, start, status="RESERVED", y=300):
    end = f"{int(start[:2]) + 1:02d}{start[2:]}"
    return {"AXLabel": f"{ticket}  {status} {start} - {end} \U000f0a70 16:28",
            "type": "GenericElement", "frame": {"x": 800, "y": y, "width": 360, "height": 80}}


class EventsList:
    """Pages of rows; a swipe reveals the next page (a virtualised list)."""

    def __init__(self, pages):
        self.pages, self.page = pages, 0
        self.swipes, self.tapped = 0, []

    def describe_all(self, udid):
        return [APP] + self.pages[min(self.page, len(self.pages) - 1)]

    def swipe(self, udid, x1, y1, x2, y2, duration=0.25):
        assert y2 < y1, "finger travels UP to reveal later rows"
        self.swipes += 1
        self.page += 1

    def tap_el(self, udid, e, els=None, scroll=True):
        self.tapped.append(caf._idbd.name(e).split()[0])
        return True, "idb, scrolled into view"


@pytest.fixture
def events(monkeypatch):
    def run(pages, slot="16:35"):
        ev = EventsList(pages)
        monkeypatch.setattr(caf._idbd, "describe_all", ev.describe_all)
        monkeypatch.setattr(caf._idbd, "swipe", ev.swipe)
        monkeypatch.setattr(caf._idbd, "tap_el", ev.tap_el)
        monkeypatch.setattr(caf.time, "sleep", lambda s: None)
        runner = FlowRunner.__new__(FlowRunner)
        runner.devices = {"waiter": "IPAD"}
        notes = []
        ok = FlowRunner._click_sidebar_row(runner, slot, ("reserved",), notes)
        return ok, notes, ev
    return run


FIRST_PAGE = [row("4770", "16:05"), row("4772", "16:15", "SERVE", 400),
              row("4771", "16:15", y=500), row("4769", "16:15", "DECLINED", 600)]


def test_a_clipped_row_is_scrolled_into_view_and_tapped(events):
    # 4773 is in the tree but below the sheet's visible area.
    ok, notes, ev = events([FIRST_PAGE + [row("4773", "16:35", y=1100)]])
    assert ok and ev.tapped == ["4773"]
    assert any("scrolled the events list" in n for n in notes)


def test_a_row_not_rendered_yet_is_found_by_scrolling_down(events):
    later = [row("4768", "16:15", "DECLINED", 300), row("4773", "16:35", y=420)]
    ok, notes, ev = events([FIRST_PAGE, later])
    assert ok and ev.tapped == ["4773"] and ev.swipes == 1
    assert any("after scrolling the list 1x" in n for n in notes)


def test_end_of_list_stops_scrolling(events):
    ok, notes, ev = events([FIRST_PAGE, FIRST_PAGE])
    assert not ok and ev.tapped == []
    assert ev.swipes == 1, "the list did not move: stop, do not swipe forever"


def test_wrong_status_is_never_opened(events):
    ok, _, ev = events([[row("4773", "16:35", "DECLINED", 300)]])
    assert not ok and ev.tapped == []


def test_a_row_too_far_down_keeps_paging_until_reachable(events, monkeypatch):
    far = FIRST_PAGE + [row("4773", "16:35", y=2600)]
    near = [row("4773", "16:35", y=400)]
    ok_after = {"n": 0}

    def tap_el(udid, e, els=None, scroll=True):
        ok_after["n"] += 1
        return (ok_after["n"] > 1), "idb" if ok_after["n"] > 1 else "not reachable"
    ev = EventsList([far, near])
    monkeypatch.setattr(caf._idbd, "describe_all", ev.describe_all)
    monkeypatch.setattr(caf._idbd, "swipe", ev.swipe)
    monkeypatch.setattr(caf._idbd, "tap_el", tap_el)
    monkeypatch.setattr(caf.time, "sleep", lambda s: None)
    runner = FlowRunner.__new__(FlowRunner)
    runner.devices = {"waiter": "IPAD"}
    notes = []
    assert FlowRunner._click_sidebar_row(runner, "16:35", ("reserved",), notes)
    assert ev.swipes == 1 and ok_after["n"] == 2


# -- statuses -------------------------------------------------------------------

def test_row_status_is_the_whole_word():
    from automation.scenarios.cross_app_flows import _status_ok
    serve = "4776  SERVE 17:05 - 18:05 \U000f0a70 I2 17:00"
    reserved = "4774  RESERVED 16:45 - 17:45 \U000f0a70 16:39"
    paid = "4777  PAYMENT DONE 18:05 - 19:05 I2 18:00"
    assert _status_ok(serve, ("serve",))
    assert not _status_ok(reserved, ("serve",)), "'reserved' contains 'serve'"
    assert _status_ok(reserved, ("reserved", "confirmationpending"))
    assert _status_ok(paid, ("payment",))
    assert _status_ok("4778 IN PROGRESS 19:05 - 20:05", ("inprogress",))


def test_open_order_accepts_the_booking_the_kitchen_readied(monkeypatch):
    # Measured: after Ready the list showed '4776 SERVE 17:05 - 18:05' and
    # @open_order (inprogress only) skipped it and timed out.
    seen = {}

    def fake_open(self, r, notes, statuses=(), what=""):
        seen["statuses"] = statuses
        return True
    monkeypatch.setattr(FlowRunner, "_open_reservation", fake_open)
    runner = FlowRunner.__new__(FlowRunner)
    assert FlowRunner._handle_special(runner, None, "@open_order", [])
    assert "serve" in seen["statuses"] and "inprogress" in seen["statuses"]


def test_the_my_orders_panel_is_checked_before_the_calendar(monkeypatch):
    runner = FlowRunner.__new__(FlowRunner)
    runner.devices = {"waiter": "IPAD"}
    runner._booked_slot = "17:05"
    runner.business_bundle = "b"
    calls = []
    monkeypatch.setattr(runner, "_dismiss_table_modal", lambda r, n: None, raising=False)
    monkeypatch.setattr(runner, "_idb_els",
                        lambda udid="": [{"label": "RoopaDcardServe", "id": "", "cy": 0}],
                        raising=False)
    # No hour list open (rows would carry their window) ...
    monkeypatch.setattr(runner, "_click_sidebar_row",
                        lambda slot, st, notes, max_pages=8, where="":
                        calls.append(("rows", where)) or False, raising=False)
    # ... so the panel's cards are read from the screen.
    monkeypatch.setattr(runner, "_open_from_panel",
                        lambda slot, st, what, notes: calls.append(("panel", slot)) or True,
                        raising=False)
    monkeypatch.setattr(runner, "_swipe_calendar",
                        lambda *a: pytest.fail("the calendar must not be scrolled"), raising=False)
    notes = []
    assert FlowRunner._open_reservation(runner, None, notes, statuses=("serve",), what="@open_order")
    assert calls == [("rows", "events list"), ("panel", "17:05")]
