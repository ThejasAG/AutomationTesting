"""Selecting every item on the waiter's Order Summary.

Measured 2026-09-29 ('Order later -> waiter adds items -> pay in B-App'): after
@ensure_order_items added two items the header showed 'Unselect' (anything
selected replaces Select All), and 'click selectAllItemsBtn' -- the PHONE id; the
iPad's is 'selectAll' -- hunted a control that could not appear until the 240s
step timeout killed the segment.
"""
import types

import pytest

from automation.scenarios import cross_app_flows as caf
from automation.scenarios.cross_app_flows import FlowRunner


def el(name, label=None):
    return {"AXIdentifier": "", "AXLabel": label or name, "type": "StaticText",
            "frame": {"x": 900, "y": 80, "width": 80, "height": 20}}


class Screen:
    """The Order Summary header: Select All <-> Unselect, per the app's state."""

    def __init__(self, selected, items=True, select_works=True):
        self.selected, self.items, self.select_works = selected, items, select_works
        self.taps = []

    def describe_all(self, udid):
        els = [el("sendItemsBtn"), el("addItemsBtn")]
        if self.items:
            # accessibilityLabel on the header Text (OrderSummary.js:628, 642).
            els.append(el("unSelectAll") if self.selected else el("selectAll"))
        return els

    def tap(self, udid, names, els=None, scroll=True):
        here = {caf._idbd.name(e) for e in self.describe_all(udid)}
        hit = next((n for n in names if n in here), None)
        if hit is None:
            return False, "not on screen"
        self.taps.append(hit)
        if hit == "unSelectAll":
            self.selected = False
        elif hit == "selectAll" and self.select_works:
            self.selected = True
        return True, "idb"


@pytest.fixture
def run(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))

    def go(screen):
        monkeypatch.setattr(caf._idbd, "describe_all", screen.describe_all)
        monkeypatch.setattr(caf._idbd, "tap", screen.tap)
        runner = FlowRunner.__new__(FlowRunner)
        runner._cur_udid = "IPAD"
        notes = []
        return runner._select_all_items(notes), notes, clock["t"]
    return go


def test_already_selected_is_reset_then_everything_is_selected(run):
    s = Screen(selected=True)
    ok, notes, _ = run(s)
    assert ok and s.taps == ["unSelectAll", "selectAll"] and s.selected
    assert "reset the selection" in notes[-1]


def test_nothing_selected_taps_select_all(run):
    s = Screen(selected=False)
    ok, notes, _ = run(s)
    assert ok and s.taps == ["selectAll"] and s.selected
    assert notes[-1].startswith("[ok] select all items")


def test_no_selector_fails_in_seconds_not_the_step_timeout(run):
    ok, notes, took = run(Screen(selected=False, items=False))
    assert not ok and took < 10
    assert "neither Select All nor Unselect" in notes[-1]


def test_a_select_all_that_does_nothing_is_a_failure(run):
    # Disabled in Payment/Completed (opacity 0.5): the tap lands, nothing changes.
    ok, notes, _ = run(Screen(selected=False, select_works=False))
    assert not ok and "nothing was selected" in notes[-1]


def test_the_plain_steps_route_to_the_handler():
    for step in ("click selectAllItemsBtn", "click selectAll"):
        assert FlowRunner._PLAIN_STEP_TOKENS[step] == "@select_all_items"


def test_the_token_is_dispatched(monkeypatch):
    monkeypatch.setattr(FlowRunner, "_select_all_items", lambda self, notes: "called")
    assert FlowRunner._handle_special(FlowRunner.__new__(FlowRunner), None,
                                      "@select_all_items", []) == "called"


# -- is the order empty? (@ensure_order_items) --------------------------------------

def _has_items(monkeypatch, frames):
    clock = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))
    monkeypatch.setattr(caf._idbd, "describe_all",
                        lambda udid: frames[min(int(clock["t"]), len(frames) - 1)])
    runner = FlowRunner.__new__(FlowRunner)
    runner._cur_udid = "IPAD"
    r = types.SimpleNamespace(_resolve=lambda ids: pytest.fail("Appium must not be used"))
    return runner._order_has_items(r)


def test_a_pre_order_on_the_ipad_is_seen(monkeypatch):
    # It used to look for the phone's 'selectAllItemsBtn' only and add items on top.
    assert _has_items(monkeypatch, [[el("addItemsBtn"), el("selectAll", "Select All")]])
    assert _has_items(monkeypatch, [[el("addItemsBtn"), el("unSelectAll", "Unselect")]])


def test_items_that_load_after_the_add_button_still_count(monkeypatch):
    frames = [[el("addItemsBtn")], [el("addItemsBtn")], [el("addItemsBtn"), el("selectAll")]]
    assert _has_items(monkeypatch, frames)


def test_an_empty_order_is_empty(monkeypatch):
    assert not _has_items(monkeypatch, [[el("addItemsBtn")]])


# -- SEND to kitchen ------------------------------------------------------------------

class Order(Screen):
    """SEND shows while items are selected; a working post clears the selection."""

    def __init__(self, selected=True, post_works=True, works_on=1):
        super().__init__(selected)
        self.post_works, self.works_on, self.sends = post_works, works_on, 0

    def describe_all(self, udid):
        els = [e for e in super().describe_all(udid) if e["AXLabel"] != "sendItemsBtn"]
        return els + ([el("sendItemsBtn")] if self.selected else [])

    def tap(self, udid, names, els=None, scroll=True):
        if names == ["sendItemsBtn"]:
            if not self.selected:
                return False, "not on screen"
            self.sends += 1
            self.taps.append("sendItemsBtn")
            if self.post_works and self.sends >= self.works_on:
                self.selected = False
            return True, "idb"
        return super().tap(udid, names, els, scroll)


@pytest.fixture
def send(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))

    def go(order):
        monkeypatch.setattr(caf._idbd, "describe_all", order.describe_all)
        monkeypatch.setattr(caf._idbd, "tap", order.tap)
        runner = FlowRunner.__new__(FlowRunner)
        runner._cur_udid = "IPAD"
        notes = []
        return runner._send_to_kitchen(notes), notes
    return go


def test_send_is_confirmed_by_the_button_clearing(send):
    o = Order()
    ok, notes = send(o)
    assert ok and o.sends == 1 and "the order was sent" in notes[-1]


def test_nothing_selected_selects_all_then_sends(send):
    o = Order(selected=False)
    ok, notes = send(o)
    assert ok and o.taps == ["selectAll", "sendItemsBtn"]


def test_a_send_that_did_not_register_is_retried_once(send):
    o = Order(works_on=2)
    ok, notes = send(o)
    assert ok and o.sends == 2 and "2nd tap" in notes[-1]


def test_a_failed_post_is_a_failure_not_a_pass(send):
    o = Order(post_works=False)
    ok, notes = send(o)
    assert not ok and o.sends == 2 and "was not sent" in notes[-1]


def test_send_routes_to_the_handler():
    assert FlowRunner._PLAIN_STEP_TOKENS["click sendItemsBtn"] == "@send_to_kitchen"
