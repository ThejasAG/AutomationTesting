"""Opening a booking from the waiter's right-hand My Orders panel.

Its cards carry ticket, status and window on screen, but accessibility sees only
`${el?.id}` -- 'undefined' for a reserved booking -- so the text is read from the
screen. Measured on the iPad (fast recognition):
    4779  RESERVED     '17:55 19:55 | Yl'
    4781  RESERVED     '18:05- 19:05 | Yl'
    4782  IN PROGRESS  '18:15 19:15'
"""
import types

import pytest

from automation.scenarios import cross_app_flows as caf
from automation.scenarios import screen_text
from automation.scenarios.cross_app_flows import FlowRunner

APP = {"AXLabel": "Vya Business", "type": "Application",
       "frame": {"x": 0, "y": 0, "width": 1210, "height": 834}}


def el(name, x, y, w, h, t="GenericElement"):
    return {"AXLabel": name, "type": t, "frame": {"x": x, "y": y, "width": w, "height": h}}


PANEL = [APP, el("My Orders", 822, 26, 124, 30, "StaticText"),
         el("undefined", 796, 162, 414, 130), el("undefined", 796, 291, 414, 130),
         el("6aba600002eef7001dcf5e70", 796, 420, 414, 132)]

TEXT = [("4779", 830, 205, 40, 14), ("RESERVED", 1070, 208, 90, 10),
        ("17:55 19:55 | Yl", 820, 240, 100, 14),
        ("4781", 830, 334, 40, 14), ("RESERVED", 1070, 339, 90, 10),
        ("18:05- 19:05 | Yl", 820, 368, 100, 14),
        ("4782", 830, 465, 40, 14), ("IN PROGRESS", 1050, 468, 110, 10),
        ("18:15 19:15", 820, 500, 90, 14)]


@pytest.fixture
def panel(monkeypatch):
    def go(slot, statuses, text=TEXT):
        taps = []
        monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: PANEL)
        monkeypatch.setattr(screen_text, "read_text", lambda udid, region=None, els=None: text)
        monkeypatch.setattr(caf._idbd, "tap_el",
                            lambda udid, e, els=None, scroll=True: taps.append(e["frame"]["y"]) or (True, "idb"))
        monkeypatch.setattr(caf._idbd, "swipe", lambda *a, **k: None)
        monkeypatch.setattr(caf.time, "sleep", lambda s: None)
        runner = FlowRunner.__new__(FlowRunner)
        runner.devices = {"waiter": "IPAD"}
        monkeypatch.setattr(runner, "_reservation_opened", lambda n, w, s, where: True, raising=False)
        notes = []
        ok = runner._open_from_panel(slot, statuses, "@open_reservation", notes)
        return ok, notes, taps
    return go


def test_the_reserved_booking_at_the_slot_is_opened(panel):
    ok, notes, taps = panel("18:05", ("reserved", "confirmationpending"))
    assert ok and taps == [291]
    assert "4781 RESERVED" in notes[-2] and notes[-1] == "    · booking ticket 4781"


def test_a_dropped_dash_still_reads_the_start_time(panel):
    ok, _, taps = panel("17:55", ("reserved",))
    assert ok and taps == [162]


def test_the_wrong_status_is_not_opened(panel):
    ok, notes, taps = panel("18:15", ("reserved", "confirmationpending"))
    assert not ok and taps == []
    assert any("IN PROGRESS" in n for n in notes)


def test_serve_is_not_read_out_of_reserved(panel):
    # 'RESERVED' contains 'SERVE': @open_order must not open a reserved booking.
    ok, _, taps = panel("18:05", ("serve", "inprogress", "payment"))
    assert not ok and taps == []


def test_in_progress_is_opened_for_open_order(panel):
    ok, _, taps = panel("18:15", ("serve", "inprogress", "payment"))
    assert ok and taps == [420]


def test_no_screen_reader_says_why_it_falls_back(panel):
    # It used to fall back silently, so a 106s calendar scroll had no explanation.
    ok, notes, taps = panel("18:05", ("reserved",), text=[])
    assert not ok and taps == []
    assert notes == ["    · My Orders panel: could not read its text — trying the calendar"]


# Measured 2026-09-29 (screenshot of the waiter board, panel text as read, in app
# points): 4794 RESERVED 12:50 is the 5th card. The step scrolled the calendar
# for 106s instead -- no accessibility card element was needed to see it here.
TODAY = [("4789", 830, 180, 40, 14), ("IN PROGRESS", 1050, 182, 110, 10),
         ("12:05 - 13:05", 830, 215, 100, 14), ("12:05", 1130, 214, 40, 14),
         ("4791", 830, 310, 40, 14), ("RESERVED", 1070, 312, 90, 10),
         ("12:20- 13:20 | YI", 830, 345, 100, 14), ("12:19", 1130, 345, 40, 14),
         ("4790", 830, 440, 40, 14), ("RESERVED", 1070, 442, 90, 10),
         ("12:20- 14:20 | YI", 830, 475, 100, 14), ("12:18", 1130, 475, 40, 14),
         ("4792", 830, 570, 40, 14), ("SERVE", 1090, 572, 60, 10),
         ("12:25- 13:25", 830, 605, 100, 14), ("12:31", 1130, 605, 40, 14),
         ("4794", 830, 700, 40, 14), ("RESERVED", 1070, 702, 90, 10),
         ("12:50- 13:50 | YI", 830, 735, 100, 14), ("12:43", 1130, 735, 40, 14)]


def test_todays_booking_is_opened_from_the_text_without_card_elements(monkeypatch):
    points = []
    bare = [APP, el("My Orders", 822, 26, 124, 30, "StaticText")]   # no card elements
    monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: bare)
    monkeypatch.setattr(screen_text, "read_text", lambda udid, region=None, els=None: TODAY)
    monkeypatch.setattr(caf._idbd, "tap_point",
                        lambda udid, x, y: points.append((x, y)) or (True, "idb"))
    monkeypatch.setattr(caf._idbd, "swipe", lambda *a, **k: None)
    monkeypatch.setattr(caf.time, "sleep", lambda s: None)
    runner = FlowRunner.__new__(FlowRunner)
    runner.devices = {"waiter": "IPAD"}
    monkeypatch.setattr(runner, "_reservation_opened", lambda n, w, s, where: True, raising=False)
    notes = []
    assert runner._open_from_panel("12:50", ("reserved", "confirmationpending"),
                                   "@open_reservation", notes)
    assert points == [(880.0, 742.0)]                 # the 12:50 window of card 4794
    assert "4794 RESERVED" in notes[-2] and notes[-1] == "    · booking ticket 4794"


def test_serve_card_is_not_taken_for_reserved_today(monkeypatch):
    monkeypatch.setattr(caf._idbd, "describe_all",
                        lambda udid: [APP, el("My Orders", 822, 26, 124, 30, "StaticText")])
    monkeypatch.setattr(screen_text, "read_text", lambda udid, region=None, els=None: TODAY)
    monkeypatch.setattr(caf._idbd, "tap_point", lambda *a: pytest.fail("must not tap"))
    monkeypatch.setattr(caf._idbd, "swipe", lambda *a, **k: None)
    monkeypatch.setattr(caf.time, "sleep", lambda s: None)
    runner = FlowRunner.__new__(FlowRunner)
    runner.devices = {"waiter": "IPAD"}
    notes = []
    assert not runner._open_from_panel("12:25", ("reserved",), "@open_reservation", notes)
    assert "4792 reads SERVE" in notes[0]


# -- an empty read on a landscape screen retries the other rotation ------------------
# Measured 2026-10-06: the split dialog read "nothing" right after the same box had
# read the profile names; @split_item then failed with "'Apply' is not on the dialog".

def test_an_empty_read_tries_the_other_landscape_rotation(monkeypatch, tmp_path):
    from PIL import Image
    from automation.scenarios import idb_coords, idb_driver
    monkeypatch.setattr(screen_text, "ocr_binary", lambda: "/fake/ocr")
    monkeypatch.setattr(idb_driver, "describe_all", lambda udid: [APP])
    monkeypatch.setattr(idb_coords, "mode", lambda udid, els=None: "cw")
    turns, calls = [], {"n": 0}
    real_upright = idb_coords.upright
    monkeypatch.setattr(idb_coords, "upright",
                        lambda img, m: turns.append(m) or real_upright(img, m))

    def run(args, **kw):
        if args[0] == "xcrun":                       # the screenshot: a portrait framebuffer
            Image.new("RGB", (834, 1210), "white").save(args[-1])
            return types.SimpleNamespace(stdout="")
        calls["n"] += 1                              # the OCR binary
        text = "" if calls["n"] == 1 else "0.1\t0.1\t0.2\t0.05\tApply\n"
        return types.SimpleNamespace(stdout=text)
    monkeypatch.setattr(screen_text.subprocess, "run", run)
    found = screen_text.read_text("IPAD", (300, 200, 900, 600))
    assert turns == ["cw", "ccw"] and [t[0] for t in found] == ["Apply"]


def test_a_read_that_works_is_not_repeated(monkeypatch):
    from PIL import Image
    from automation.scenarios import idb_coords, idb_driver
    monkeypatch.setattr(screen_text, "ocr_binary", lambda: "/fake/ocr")
    monkeypatch.setattr(idb_driver, "describe_all", lambda udid: [APP])
    monkeypatch.setattr(idb_coords, "mode", lambda udid, els=None: "ccw")
    calls = {"n": 0}

    def run(args, **kw):
        if args[0] == "xcrun":
            Image.new("RGB", (834, 1210), "white").save(args[-1])
            return types.SimpleNamespace(stdout="")
        calls["n"] += 1
        return types.SimpleNamespace(stdout="0.1\t0.1\t0.2\t0.05\tApply\n")
    monkeypatch.setattr(screen_text.subprocess, "run", run)
    assert screen_text.read_text("IPAD")[0][0] == "Apply" and calls["n"] == 1


def _scrolling_panel(monkeypatch, pages, start):
    """A panel whose visible text depends on how far it is scrolled. `pages` is a
    list of TEXT pages top->bottom; it starts at index `start`. A swipe moves one
    page (clamped at both ends, like the real list)."""
    pos = {"i": start}
    taps = []
    monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: PANEL)
    monkeypatch.setattr(screen_text, "read_text",
                        lambda udid, region=None, els=None: pages[pos["i"]])

    def swipe(udid, x1, y1, x2, y2, dur=0.25):
        step = 1 if y2 < y1 else -1                    # finger up = later bookings
        pos["i"] = max(0, min(len(pages) - 1, pos["i"] + step))
    monkeypatch.setattr(caf._idbd, "swipe", swipe)
    monkeypatch.setattr(caf._idbd, "tap_el",
                        lambda udid, e, els=None, scroll=True: taps.append(pos["i"]) or (True, "idb"))
    monkeypatch.setattr(caf.time, "sleep", lambda s: None)
    runner = FlowRunner.__new__(FlowRunner)
    runner.devices = {"waiter": "IPAD"}
    monkeypatch.setattr(runner, "_reservation_opened", lambda n, w, s, where: True, raising=False)
    return runner, taps, pos


def _page(ticket, start):
    return [(ticket, 830, 205, 40, 14), ("RESERVED", 1070, 208, 90, 10),
            (f"{start} 19:55", 820, 240, 100, 14)]


def test_a_booking_above_where_the_panel_was_left_is_found(monkeypatch):
    """The panel was left scrolled to the bottom by an earlier step; the booking is
    on the FIRST page. Searching only downwards missed it."""
    pages = [_page("4790", "12:50"), _page("4791", "13:10"), _page("4792", "13:30")]
    runner, taps, pos = _scrolling_panel(monkeypatch, pages, start=2)
    notes = []
    assert runner._open_from_panel("12:50", ("reserved",), "@open_reservation", notes)
    assert taps == [0]
    assert any("checking above" in n for n in notes)


def test_a_missing_booking_searches_both_ends_then_says_why(monkeypatch):
    pages = [_page("4790", "12:50"), _page("4791", "13:10")]
    runner, taps, _ = _scrolling_panel(monkeypatch, pages, start=0)
    notes = []
    assert not runner._open_from_panel("16:00", ("reserved",), "@open_reservation", notes)
    assert taps == []
    assert "trying the calendar" in notes[-1]
