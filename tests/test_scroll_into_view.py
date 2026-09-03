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
