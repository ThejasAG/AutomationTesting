"""The overlap detector, checked against the three real bugs of 2026-09-02.

Each took hours of reading frames by hand; each is a rectangle intersection.
"""
from automation.inspector.overlap import covered_by, off_screen, report, rect_of


def el(label, x, y, w, h):
    return {"AXLabel": label, "frame": {"x": x, "y": y, "width": w, "height": h}}


# Real geometry, iPad Pro 11 landscape, New Appointment form open.
SAVE = el("saveBtn", 832, 685, 347, 50)
TOAST = el("8 Deprecation warning: value provided is not…", 10, 708, 1190, 48)
# Real geometry, iPhone, same form.
SLOT = el("17:00Btn", 40, 706, 72, 48)
PHONE_TOAST = el("3 no valid aps-environment…", 10, 726, 373, 48)
# Real geometry, consumer home.
CARD = el("NylaiKitchen2", 28, 802, 346, 160)


def test_the_toast_over_saveBtn_is_found():
    """Save was 'tapped' every run and the form never closed."""
    hits = covered_by(SAVE, [SAVE, TOAST])
    assert hits and hits[0][0] is TOAST
    assert rect_of(TOAST).contains(rect_of(SAVE).cx, rect_of(SAVE).cy)


def test_the_toast_over_the_time_slot_is_found():
    """Tapping the slot opened the LogBox viewer instead of selecting a time."""
    assert covered_by(SLOT, [SLOT, PHONE_TOAST])


def test_the_offscreen_card_is_found():
    """cy=882 on an 852pt screen — reported as 'no element matches'."""
    assert off_screen(CARD, 393, 852)
    assert not off_screen(SLOT, 393, 852)


def test_an_element_under_the_header_counts_as_out_of_reach():
    """Scrolled to y=29 it was technically visible; the tap hit the status bar."""
    top = el("NylaiKitchen2", 28, -51, 346, 160)      # centre y = 29
    assert not off_screen(top, 393, 852)
    assert off_screen(top, 393, 852, safe_margin=110)


def test_a_parent_container_is_layout_not_occlusion():
    """Otherwise every element is 'covered' by its own screen and the report is noise."""
    screen = el("root", 0, 0, 1194, 834)
    assert covered_by(SAVE, [SAVE, screen]) == []


def test_a_clean_screen_reports_nothing():
    assert report([el("a", 10, 10, 40, 40), el("b", 100, 100, 40, 40)], 393, 852) == []


def test_report_names_the_blocker_and_says_it_hits_the_tap_point():
    out = report([SAVE, TOAST], 1194, 834)
    save = next(r for r in out if r["label"] == "saveBtn")
    covered = [i for i in save["issues"] if i["kind"] == "covered"]
    assert covered and "INCLUDING its tap point" in covered[0]["detail"]


def test_elements_without_a_usable_frame_are_skipped():
    assert rect_of({"AXLabel": "x", "frame": {}}) is None
    assert report([{"AXLabel": "x", "frame": {"x": 0, "y": 0, "width": 0, "height": 0}}],
                  393, 852) == []
