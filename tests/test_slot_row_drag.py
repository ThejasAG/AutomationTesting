"""The waiter form's time chips are one horizontal row of the whole day.

MEASURED 2026-10-17 (waiter demo, iPad Pro M5): 12:55 sat far right of the strip,
idb's (vertical-only) scroll-into-view refused it, Appium's click failed, and the
step ran past 240s. The row is now dragged until the chip is on screen."""
from automation.scenarios import cross_app_flows as caf
from automation.scenarios.cross_app_flows import FlowRunner


SAVE = {"AXLabel": "saveBtn", "type": "GenericElement",
        "frame": {"x": 832, "y": 685, "width": 347, "height": 50}}   # measured: the form


def test_drags_the_row_until_the_chip_is_inside_the_form(monkeypatch):
    """The form is the RIGHT-HAND panel (saveBtn x 832..1179): the chip must land
    inside it, not mid-screen over the bookings board (review 2026-10-19)."""
    row = {"x": 1700.0}        # 6 chips in: the form lists only times from now on
    swipes, taps = [], []

    def describe_all(udid):
        return [{"AXLabel": "Vya Business", "type": "Application",
                 "frame": {"x": 0, "y": 0, "width": 1210, "height": 834}}, SAVE,
                {"AXLabel": "12:55Btn", "type": "Button",
                 "frame": {"x": row["x"], "y": 600, "width": 80, "height": 36}}]

    def swipe(udid, x1, y1, x2, y2, dur=1.0):
        swipes.append((x1, x2))
        row["x"] += x2 - x1                       # a slow drag moves the row 1:1

    monkeypatch.setattr(caf._idbd, "describe_all", describe_all)
    monkeypatch.setattr(caf._idbd, "swipe", swipe)
    monkeypatch.setattr(caf._idbd, "tap",
                        lambda udid, names, els=None, scroll=True: taps.append(names[0]) or (True, "idb"))
    monkeypatch.setattr(caf.time, "sleep", lambda s: None)
    runner = FlowRunner.__new__(FlowRunner)
    ok, how = runner._slot_into_view_and_tap("IPAD", "12:55Btn")
    assert ok and taps == ["12:55Btn"], how
    assert 832 <= row["x"] and row["x"] + 80 <= 1179, "the chip is inside the form"
    assert all(x2 < x1 for x1, x2 in swipes)       # finger travels left: later slots come in
    assert all(832 <= min(x1, x2) and max(x1, x2) <= 1179 for x1, x2 in swipes), \
        "every drag stays on the form, never on the board"


def test_a_row_that_does_not_move_is_reported(monkeypatch):
    monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: [
        {"AXLabel": "Vya Business", "type": "Application",
         "frame": {"x": 0, "y": 0, "width": 1210, "height": 834}},
        {"AXLabel": "12:55Btn", "type": "Button",
         "frame": {"x": 3000, "y": 700, "width": 80, "height": 36}}])
    monkeypatch.setattr(caf._idbd, "swipe", lambda *a, **k: None)
    monkeypatch.setattr(caf.time, "sleep", lambda s: None)
    runner = FlowRunner.__new__(FlowRunner)
    ok, how = runner._slot_into_view_and_tap("IPAD", "12:55Btn")
    assert not ok and "did not move" in how


def test_the_chips_are_waited_for_before_anything_is_scrolled(monkeypatch):
    """MEASURED 2026-10-22 (flow6, run 0b9bfc7b): on a slow staging the waiter form's
    chips were not rendered at the first read; the step scrolled 3x through the Appium
    resolver and the form was gone by the end. Wait for them first."""
    import json
    import types
    from datetime import datetime, timedelta
    clock = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))
    soon = datetime.now() + timedelta(minutes=12)
    chip = f"{soon.hour:02d}:{soon.minute - soon.minute % 5:02d}Btn"
    form = [{"AXLabel": "Vya Business", "type": "Application",
             "frame": {"x": 0, "y": 0, "width": 1210, "height": 834}},
            {"AXLabel": "saveBtn", "type": "GenericElement",
             "frame": {"x": 832, "y": 685, "width": 347, "height": 50}}]
    chips = [{"AXLabel": chip, "type": "Button",
              "frame": {"x": 850, "y": 600, "width": 80, "height": 36}}]
    screen = lambda: form + (chips if clock["t"] >= 3 else [])            # noqa: E731
    monkeypatch.setattr(caf._idb_fast, "subprocess_run",
                        lambda *a, **k: types.SimpleNamespace(stdout=json.dumps(screen())))
    monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: screen())
    swipes, taps = [], []
    monkeypatch.setattr(caf._idbd, "swipe", lambda *a, **k: swipes.append(a))
    monkeypatch.setattr(caf._idbd, "tap",
                        lambda udid, names, els=None, scroll=True: taps.append(names[0]) or (True, "idb"))
    runner = FlowRunner.__new__(FlowRunner)
    runner._cur_udid, runner.devices = "IPAD", {"waiter": "IPAD"}
    notes = []
    assert runner._first_time_slot(None, notes), notes
    assert taps == [chip] and swipes == [], "found by waiting, nothing scrolled"
