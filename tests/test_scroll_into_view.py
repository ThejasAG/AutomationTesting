"""An element idb can see but that sits below the fold must be scrolled to, not abandoned.

Measured case: the 'NylaiKitchen2' restaurant card — frame y=802 h=160, so its CENTRE
(what idb taps) is y=882, below an 852pt screen. idb saw
it; Appium's depth-capped snapshot did not, so falling through to the resolver failed the
step with "No element matches" while the element was plainly in the tree.
"""
from automation.intelligence.scenario_runner import ScenarioRunner


class FakeDriver:
    def __init__(self, y_start=882, step=300):   # 802 + 160/2 = the tapped centre
        self.y = y_start
        self.step = step
        self.swipes = []

    def get_window_size(self):
        return {"width": 393, "height": 852}

    def execute_script(self, name, args=None):
        self.swipes.append((name, (args or {}).get("direction")))
        self.y -= self.step          # the list scrolls the card up into view


def _runner(d):
    r = object.__new__(ScenarioRunner)
    r.d = d
    r._idb_element = lambda phrase, arr=None: (200, d.y)
    r._wait_settle = lambda *a, **k: None
    return r


def test_it_swipes_until_the_card_is_on_screen():
    d = FakeDriver()
    pt = _runner(d)._scroll_into_view("NylaiKitchen2")
    assert pt is not None and pt[1] <= 852
    assert d.swipes and all(dirn == "down" for _, dirn in d.swipes)
    # must SCROLL the list, never swipe the window — a bare swipe opened
    # the side drawer and hung the step for its full timeout.
    assert all(name == "mobile: scroll" for name, _ in d.swipes)


def test_it_gives_up_rather_than_swiping_forever():
    d = FakeDriver(step=0)                 # nothing moves
    assert _runner(d)._scroll_into_view("NylaiKitchen2", max_swipes=3) is None
    assert len(d.swipes) == 3


def test_an_already_visible_element_is_not_swiped_at_all():
    d = FakeDriver(y_start=400)
    assert _runner(d)._scroll_into_view("NylaiKitchen2") == (200, 400)
    assert d.swipes == []


def test_settle_confirms_with_a_point_probe_not_a_second_screen_read(monkeypatch):
    """A whole-screen read is 3-5s on the iPad; the 'stopped moving' re-check is a
    describe-point at the target (0.05s) that returns the same frame."""
    from automation.scenarios import idb_driver as dv
    chip = {"AXLabel": "12:05Btn", "frame": {"x": 850, "y": 463, "width": 111, "height": 32}}
    reads = []
    monkeypatch.setattr(dv.time, "sleep", lambda s: None)
    monkeypatch.setattr(dv, "describe_all", lambda u: reads.append(u) or [chip])
    monkeypatch.setattr(dv, "to_device", lambda u, x, y: (int(x), int(y)))
    monkeypatch.setattr(dv, "describe_point", lambda u, x, y: dict(chip))
    got = dv._settle("U", "12:05Btn", [])
    assert dv.frame(got) == dv.frame(chip)
    assert len(reads) == 1


def test_settle_falls_back_to_a_full_read_when_the_point_misses(monkeypatch):
    from automation.scenarios import idb_driver as dv
    frames = iter([463, 400, 400])
    reads = []

    def read(u):
        reads.append(u)
        return [{"AXLabel": "12:05Btn",
                 "frame": {"x": 850, "y": next(frames), "width": 111, "height": 32}}]
    monkeypatch.setattr(dv.time, "sleep", lambda s: None)
    monkeypatch.setattr(dv, "describe_all", read)
    monkeypatch.setattr(dv, "to_device", lambda u, x, y: (int(x), int(y)))
    monkeypatch.setattr(dv, "describe_point", lambda u, x, y: {})   # still moving
    got = dv._settle("U", "12:05Btn", [])
    assert dv.frame(got)[1] == 400 and len(reads) == 3
