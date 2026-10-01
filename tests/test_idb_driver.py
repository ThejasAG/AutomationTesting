"""The fast idb driver: every tap is verified under the point, every fill read back.

Measured on the business iPad: Appium element commands cost 1-12s each (typing
one name ~44s); idb does the same in 2-5s. The two things that make idb unsafe
if used blindly are pinned here:
  * content-space frames: the 16:15 chip reported a frame that on screen was
    the SAVE button -> never tap until describe-point returns the target;
  * auto-capitalisation: 'probe' typed as 'Probe' -> Shift first, then read back.
"""
import pytest

from automation.scenarios import idb_driver as dv


def el(name, x, y, w=100, h=40, t="GenericElement", value=None):
    e = {"AXLabel": name, "type": t, "frame": {"x": x, "y": y, "width": w, "height": h}}
    if value is not None:
        e["AXValue"] = value
    return e


APP = {"AXLabel": "", "type": "Application", "frame": {"x": 0, "y": 0, "width": 400, "height": 800}}


class Screen:
    """A fake idb. `layers` decides what describe-point returns at a point:
    the LAST element (topmost) whose frame contains it."""

    def __init__(self, els, scroll_shift=0, overlay=None, text_filter=None):
        self.els = [APP] + els
        self.overlay = overlay           # drawn on top of everything
        self.scroll_shift = scroll_shift  # a swipe moves scrollable elements up by this
        self.text_filter = text_filter or (lambda s: s)
        self.calls, self.focused = [], None

    def _at(self, x, y):
        layers = [e for e in self.els[1:] + ([self.overlay] if self.overlay else [])
                  if e["frame"]["x"] <= x <= e["frame"]["x"] + e["frame"]["width"]
                  and e["frame"]["y"] <= y <= e["frame"]["y"] + e["frame"]["height"]
                  and not e.get("_hidden")]
        return layers[-1] if layers else {}

    def __call__(self, args, timeout=20.0):
        self.calls.append(list(args))
        cmd = args[1]
        if cmd == "describe-all":
            import json
            return json.dumps(self.els + ([self.overlay] if self.overlay else []))
        if cmd == "describe-point":
            import json
            return json.dumps(self._at(float(args[-2]), float(args[-1])))
        if cmd == "tap":
            hit = self._at(float(args[-2]), float(args[-1]))
            self.focused = hit if hit.get("type") in dv.FIELD_TYPES else None
            return ""
        if cmd == "swipe":
            for e in self.els[1:]:
                if e.get("_scrolls"):
                    e["frame"]["y"] -= self.scroll_shift
                    e["_hidden"] = e["frame"]["y"] + e["frame"]["height"] > 700
            return ""
        if cmd == "key-sequence" and self.focused is not None:
            self.focused["AXValue"] = ""
            return ""
        if cmd == "text" and self.focused is not None:
            self.focused["AXValue"] = (self.focused.get("AXValue") or "") + self.text_filter(args[2])
            return ""
        return ""


@pytest.fixture
def screen(monkeypatch):
    def make(*a, **k):
        s = Screen(*a, **k)
        monkeypatch.setattr(dv, "_idb", s)
        monkeypatch.setattr(dv, "to_device", lambda udid, x, y, els=None: (int(x), int(y)))
        monkeypatch.setattr(dv.time, "sleep", lambda s: None)
        return s
    return make


def taps(s):
    return [c for c in s.calls if c[1] == "tap"]


def test_visible_unique_element_is_tapped(screen):
    s = screen([el("saveBtn", 100, 600)])
    assert dv.tap("U", ["saveBtn"]) == (True, "idb")
    assert taps(s) == [["ui", "tap", "--udid", "U", "150", "620"]]


def test_element_under_something_else_is_never_tapped(screen):
    # The 16:15 chip's content-space frame is really where Save is drawn.
    chip = el("16:15Btn", 100, 600)
    save = el("saveBtn", 50, 590, w=300, h=60)
    s = screen([chip, save])
    ok, why = dv.tap("U", ["16:15Btn"], scroll=False)
    assert not ok and not taps(s), "a blind tap here would press Save"


def test_scrolled_out_element_is_swiped_into_view_then_tapped(screen):
    field = el("mobileInputBtn", 100, 740, t="TextField")
    field["_scrolls"], field["_hidden"] = True, True
    s = screen([field], scroll_shift=240)
    ok, how = dv.tap("U", ["mobileInputBtn"])
    assert ok and "scrolled" in how
    assert any(c[1] == "swipe" for c in s.calls)
    assert taps(s)[-1][-1] == str(int(740 - 240 + 20))


def test_covered_element_that_does_not_move_is_left_to_appium(screen):
    btn = el("addNewEvent", 100, 700, w=50, h=50)
    blocker = el("banner", 60, 690, w=200, h=80)
    s = screen([btn, blocker])
    ok, _ = dv.tap("U", ["addNewEvent"])
    assert not ok and not taps(s)
    assert sum(1 for c in s.calls if c[1] == "swipe") <= 1, "one swipe proves it is not scrolling"


def test_logbox_strip_is_not_swiped_at(screen):
    btn = el("addNewEvent", 100, 700, w=50, h=50)
    toast = el("Warning: Each child…", 10, 690, w=390, h=48)
    s = screen([btn], overlay=toast)
    ok, _ = dv.tap("U", ["addNewEvent"])
    assert not ok and not any(c[1] in ("swipe", "tap") for c in s.calls)


def test_a_named_element_inside_the_target_is_not_the_target(screen):
    # The card image runs under the tab bar; its centre lands on 'Wallet'.
    card = el("NylaiKitchen2", 28, 700, w=346, h=160, t="Image")
    wallet = el("Wallet", 134, 780, w=134, h=60, t="Button")
    s = screen([card, wallet])
    ok, _ = dv.tap("U", ["NylaiKitchen2"], scroll=False)
    assert not ok and not taps(s), "tapping here opens the Wallet"


def test_an_unnamed_child_counts_as_the_target(screen):
    btn = el("addNewEvent", 100, 600, w=60, h=60)
    icon = el("", 110, 610, w=40, h=40)
    s = screen([btn, icon])
    assert dv.tap("U", ["addNewEvent"])[0]


def test_two_reachable_elements_with_the_name_is_ambiguous(screen):
    s = screen([el("Any", 10, 100), el("Any", 10, 300)])
    ok, why = dv.tap("U", ["Any"])
    assert not ok and "ambiguous" in why and not taps(s)


def test_missing_element(screen):
    screen([el("other", 0, 0)])
    assert dv.tap("U", ["saveBtn"]) == (False, "not on screen")


def test_fill_types_exactly_with_shift_first_and_closes_keyboard(screen):
    f = el("firstName", 10, 100, t="TextField", value="first name")
    s = screen([f])
    assert dv.fill("U", "firstName", "roopa") == (True, "roopa")
    order = [c[1] for c in s.calls if c[1] in ("tap", "key-sequence", "key", "text")]
    assert order[:4] == ["tap", "key-sequence", "key", "text"]
    shift = next(c for c in s.calls if c[1] == "key")
    assert shift[2] == dv.KEY_SHIFT
    assert s.calls[-1][1:3] == ["key", dv.KEY_RETURN], "keyboard closed last"


def test_fill_retries_when_characters_are_dropped(screen):
    f = el("email", 10, 100, t="TextField", value="")
    drops = iter([lambda t: t.replace("p2", ""), lambda t: t])
    s = screen([f], text_filter=lambda t: next(drops, lambda x: x)(t))
    ok, got = dv.fill("U", "email", "emp2A@xorstack.com")
    assert ok and got == "emp2A@xorstack.com"
    assert sum(1 for c in s.calls if c[1] == "text") == 2


def test_fill_gives_up_and_says_so(screen):
    f = el("email", 10, 100, t="TextField", value="")
    screen([f], text_filter=lambda t: t.upper())
    ok, got = dv.fill("U", "email", "abc")
    assert not ok and got != "abc"


def test_secure_field_accepts_bullets_of_the_right_length(screen):
    f = el("password", 10, 100, t="SecureTextField", value="")
    screen([f], text_filter=lambda t: "•" * len(t))
    assert dv.fill("U", "password", "secret")[0]


def test_fill_refuses_a_non_field(screen):
    s = screen([el("firstName", 10, 100, t="StaticText")])
    assert dv.fill("U", "firstName", "x") == (False, "")
    assert not taps(s)


def test_screen_checks():
    assert dv.busy([el("In progress", 0, 0)])
    assert dv.busy([el("Loading menu…", 0, 0, t="StaticText")])
    assert not dv.busy([el("Menu", 0, 0, t="StaticText")])
    assert dv.crash_text([el("Render Error: undefined is not an object", 0, 0, t="StaticText")])
    assert dv.crash_text([el("Home", 0, 0, t="StaticText")]) is None
    assert [dv.name(e) for e in dv.logbox_buttons([el("Dismiss", 0, 0), el("x", 0, 0)])] == ["Dismiss"]


def test_swipe_screen_matches_mobile_swipe_direction(screen):
    s = screen([])
    assert dv.swipe_screen("U", "down")
    sw = next(c for c in s.calls if c[1] == "swipe")
    x1, y1, x2, y2 = map(int, sw[-4:])
    assert x1 == x2 == 200 and y2 > y1, "'down' = the finger travels down"


def test_idb_unavailable_is_a_clean_miss(monkeypatch):
    def boom(args, timeout=20.0):
        raise FileNotFoundError("idb")
    monkeypatch.setattr(dv, "_idb", boom)
    assert dv.tap("U", ["x"]) == (False, "idb unavailable")
    assert dv.fill("U", "x", "y") == (False, "")
    assert dv.describe_all("U") == []


# -- typing credentials efficiently (asked 2026-10-01: "too many attempts") --------

def test_a_field_that_already_holds_the_text_is_not_retyped(screen):
    f = el("emailValue", 10, 100, t="TextField", value="waiter@example.com")
    s = screen([f])
    info = {}
    assert dv.fill("U", "emailValue", "waiter@example.com", info=info) == (True, "waiter@example.com")
    assert info["skipped"] and not any(c[1] in ("tap", "text", "key-sequence") for c in s.calls)


def test_a_field_slow_to_update_is_not_cleared_and_retyped(screen):
    # The app shows the typed text a moment later: the first read-back sees the
    # old (empty) value. That used to count as a miss and start attempt 2.
    f = el("emailValue", 10, 100, t="TextField", value="")
    s = screen([f])
    real_value = dv._value
    reads = {"n": 0}

    def lagging(udid, field):
        reads["n"] += 1
        return "" if reads["n"] == 1 else real_value(udid, field)
    import pytest as _pt
    mp = _pt.MonkeyPatch()
    mp.setattr(dv, "_value", lagging)
    try:
        info = {}
        assert dv.fill("U", "emailValue", "waiter@example.com", info=info)[0]
        assert info["attempts"] == 1
        assert sum(1 for c in s.calls if c[1] == "text") == 1, "typed once"
    finally:
        mp.undo()


def test_a_masked_field_is_always_typed(screen):
    f = el("passwordValue", 10, 100, t="SecureTextField", value="••••••")
    s = screen([f], text_filter=lambda t: "•" * len(t))
    info = {}
    assert dv.fill("U", "passwordValue", "secret", info=info)[0]
    assert not info["skipped"] and sum(1 for c in s.calls if c[1] == "text") == 1


def test_a_hung_swipe_does_not_crash_the_step(monkeypatch):
    import subprocess
    def hang(args, timeout=20.0):
        raise subprocess.TimeoutExpired(args, timeout)
    monkeypatch.setattr(dv, "_idb", hang)
    monkeypatch.setattr(dv, "to_device", lambda udid, x, y, els=None: (int(x), int(y)))
    dv.swipe("U", 10, 200, 10, 100, 1.8)          # must not raise
