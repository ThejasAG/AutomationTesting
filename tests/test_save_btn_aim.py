"""Aiming the Save tap around the LogBox toast.

Measured on the iPad with the New Appointment form open:
    saveBtn  x 832..1179  y 685..735
    toast    x  10..1200  y 708..756   <- drawn on top, covers the button's centre
Appium's .click() aims at the centre (y=710), lands on the toast, and the form never
closes — that is the 150s timeout @save_appointment used to burn every run.
"""
from automation.scenarios import cross_app_flows as F


def el(label, x, y, w, h, type="GenericElement"):
    return {"id": "", "label": label, "type": type, "x": x, "y": y, "w": w, "h": h,
            "cx": int(x + w / 2), "cy": int(y + h / 2)}


SAVE = el("saveBtn", 832, 685, 347, 50)          # centre y = 710
TOAST = el("8 Deprecation warning: value provided is not …", 10, 708, 1190, 48)
SCREEN = el("", 0, 0, 1210, 834)


def runner(els):
    fr = object.__new__(F.FlowRunner)
    taps = []
    fr._idb_els = lambda *a, **k: els
    fr._idb_tap = lambda x, y, udid="": taps.append((x, y)) or True
    return fr, taps


def test_aims_above_the_toast_when_it_covers_the_button_centre():
    fr, taps = runner([SCREEN, SAVE, TOAST])
    assert fr._tap_save_btn() is True
    x, y = taps[0]
    assert y < TOAST["y"]                          # clear of the strip
    assert SAVE["y"] <= y <= SAVE["y"] + SAVE["h"]  # still on the button
    assert (x, y) == (1005, 694)                   # the coordinate verified live


def test_a_stacked_toast_listed_first_does_not_hide_the_covering_one():
    """The live iPad bug: idb lists the aps-environment strip (y 761.5) BEFORE the
    deprecation strip (y 708) that actually covers saveBtn. Checking only the first
    strip concluded nothing was in the way and kept aiming at the covered centre."""
    other = el("3 no valid “aps-environment” entitlement …", 10, 761, 1190, 48)
    fr, taps = runner([SCREEN, SAVE, other, TOAST])
    assert F.FlowRunner._logbox_strip([SCREEN, SAVE, other, TOAST]) is other   # the trap
    assert fr._tap_save_btn() is True
    assert taps == [(1005, 694)]


def test_aims_at_the_centre_when_nothing_covers_it():
    fr, taps = runner([SCREEN, SAVE])
    assert fr._tap_save_btn() is True
    assert taps == [(SAVE["cx"], SAVE["cy"])]


def test_a_toast_elsewhere_on_screen_does_not_move_the_aim():
    high = el("8 Deprecation warning: …", 10, 100, 1190, 48)
    fr, taps = runner([SCREEN, SAVE, high])
    assert fr._tap_save_btn() is True
    assert taps == [(SAVE["cx"], SAVE["cy"])]


def test_a_toast_covering_the_whole_button_keeps_the_centre():
    # Nothing better to aim at; the caller's retry + explicit failure note takes over.
    full = el("8 Deprecation warning: …", 10, 680, 1190, 60)
    fr, taps = runner([SCREEN, SAVE, full])
    assert fr._tap_save_btn() is True
    assert taps == [(SAVE["cx"], SAVE["cy"])]


def test_no_save_button_reports_failure_so_appium_fallback_runs():
    fr, taps = runner([SCREEN, TOAST])
    assert fr._tap_save_btn() is False
    assert taps == []


def test_clearing_the_toast_is_not_enough_on_its_own():
    """The toast IS detected by both existing paths — _LOGBOX matches it on "Warning:"
    and _logbox_strip finds it by geometry. Detection was never the problem: the
    dismiss tap at the strip's right edge does not close it on the iPad (measured:
    six taps at (1180, 732), still up). Hence aiming around it rather than relying on
    clearing it. If either of these ever stops holding, the aim logic needs revisiting."""
    from automation.intelligence.scenario_runner import ScenarioRunner
    assert ScenarioRunner._LOGBOX.search(TOAST["label"]) is not None
    assert F.FlowRunner._logbox_strip([SCREEN, SAVE, TOAST]) is TOAST
    assert TOAST["y"] <= SAVE["cy"] <= TOAST["y"] + TOAST["h"]   # it really does cover Save
