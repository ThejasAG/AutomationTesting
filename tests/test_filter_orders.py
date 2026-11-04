"""The My Orders status-filter handler (@filter_orders:<status>).

Covers automation/scenarios/cross_app_flows.py::FlowRunner._filter_orders,
dispatched from `@filter_orders:<status>` in _handle_special.

The Filter sheet (Screens/Orders/index.js's `Filter`, Components/Modal/index.js)
is a magnus Overlay: its status options and Apply carry no accessibility id
(locator_map.md's Overlay trap), so they are picked by OCR text via the same
_ocr_tap already used for @void_item/@comp_item/@split_item. These tests pin:
the status is tapped before Apply, a status not on the sheet fails without
ever tapping Apply, and the handler only reports success once the 'N
result(s) shown for' line — rendered OUTSIDE the overlay, in the normal tree —
actually appears, not just that both OCR taps landed.
"""
import pytest

from automation.scenarios import cross_app_flows as F
from automation.scenarios import screen_text


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    monkeypatch.setattr(F.time, "sleep", lambda *_: None)


def _el(label):
    return {"AXIdentifier": "", "AXLabel": label, "type": "StaticText",
            "frame": {"x": 0, "y": 0, "width": 100, "height": 40}}


def runner():
    fr = object.__new__(F.FlowRunner)
    fr._cur_udid = "UDID"
    return fr


def test_dispatch_reaches_the_handler_with_the_status_argument():
    fr = runner()
    calls = []
    fr._filter_orders = lambda r, notes, status="Reserved": calls.append(status) or True
    assert F.FlowRunner._handle_special(fr, None, "@filter_orders:Reserved", [])
    assert calls == ["Reserved"]


def test_picks_the_status_then_apply_and_confirms_the_narrowed_line(monkeypatch):
    fr = runner()
    taps = []
    monkeypatch.setattr(screen_text, "read_text", lambda *a, **k: [
        ("Reserved", 100, 200, 60, 20), ("Apply", 100, 500, 60, 20)])
    monkeypatch.setattr(F._idbd, "tap_point", lambda udid, x, y: taps.append((x, y)) or (True, "tapped"))
    monkeypatch.setattr(F._idbd, "describe_all",
                         lambda udid: [_el("2 result(s) shown for « Reserved »")])
    notes = []
    assert fr._filter_orders(None, notes, "Reserved") is True
    assert len(taps) == 2                       # the status chip, then Apply
    assert "result(s) shown for" in " ".join(notes)


def test_status_not_on_the_sheet_fails_without_tapping_apply(monkeypatch):
    fr = runner()
    taps = []
    monkeypatch.setattr(screen_text, "read_text", lambda *a, **k: [("Order", 10, 10, 40, 10)])
    monkeypatch.setattr(F._idbd, "tap_point", lambda udid, x, y: taps.append((x, y)) or (True, "tapped"))
    notes = []
    assert fr._filter_orders(None, notes, "Reserved") is False
    assert taps == []
    assert "is not on the dialog" in " ".join(notes)


def test_apply_tapped_but_the_list_never_narrows_is_a_failure(monkeypatch):
    fr = runner()
    monkeypatch.setattr(screen_text, "read_text", lambda *a, **k: [
        ("Reserved", 10, 10, 40, 10), ("Apply", 10, 50, 40, 10)])
    monkeypatch.setattr(F._idbd, "tap_point", lambda udid, x, y: (True, "tapped"))
    monkeypatch.setattr(F._idbd, "describe_all", lambda udid: [_el("My Orders")])  # no result line, ever
    notes = []
    assert fr._filter_orders(None, notes, "Reserved") is False
    assert "never appeared" in " ".join(notes)


def test_apply_not_found_fails_with_the_status_already_picked_noted(monkeypatch):
    fr = runner()
    taps = []

    def fake_read_text(udid, region=None, els=None, accurate=False):
        # 'Reserved' is on the sheet; 'Apply' never is (a stuck/mis-rendered sheet).
        return [("Reserved", 10, 10, 40, 10)]
    monkeypatch.setattr(screen_text, "read_text", fake_read_text)
    monkeypatch.setattr(F._idbd, "tap_point", lambda udid, x, y: taps.append((x, y)) or (True, "tapped"))
    notes = []
    assert fr._filter_orders(None, notes, "Reserved") is False
    assert len(taps) == 1                        # the status chip landed
    assert "Apply" in " ".join(notes)
