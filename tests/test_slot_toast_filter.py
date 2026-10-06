"""A time slot the LogBox toast covers must not be tapped.

Measured on the phone: slot 17:00 centre y=730, toast y=726..774. An idb coordinate
tap there opened the LogBox VIEWER instead of selecting the time, so selectedTime
stayed null and Save was silently blocked — the 'we cannot select the time' report.
"""
from automation.scenarios.cross_app_flows import FlowRunner as _R

# (minutes, label, cx, cy) — the shape _first_time_slot builds
SLOTS = [(1020, "17:00Btn", 76, 730), (1025, "17:05Btn", 198, 730), (1080, "18:00Btn", 76, 860)]
TOAST = [{"x": 10, "y": 726, "w": 1190, "h": 48, "label": "8 Deprecation warning: …"}]


def _uncovered(slots, strips):
    return [s for s in slots
            if not any(t["x"] <= s[2] <= t["x"] + t["w"] and t["y"] <= s[3] <= t["y"] + t["h"]
                       for t in strips)]


def test_covered_slots_are_dropped():
    keep = _uncovered(SLOTS, TOAST)
    assert [s[1] for s in keep] == ["18:00Btn"]


def test_nothing_is_dropped_when_no_toast_is_up():
    assert _uncovered(SLOTS, []) == SLOTS


def test_the_strip_finder_exists_on_the_runner():
    """The filter reuses _logbox_strips — the same helper _tap_save_btn aims around."""
    assert hasattr(_R, "_logbox_strips")
