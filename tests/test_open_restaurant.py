"""'click NylaiKitchen2' on the consumer Home → @open_restaurant.

MEASURED 2026-10-27: the restaurant card was not on Home's first screen (flow1, run
6d743962: Home's list renders lazily; the Appium lookup after the failed idb tap hung the
step 240s), and six scrolls later it still was not there (run 744f3e7e: other restaurants
only). The user's procedure: scroll down; when "Load More Restaurants" shows, tap it and
look again; if it is still not found, open Favourites (the 'favoriteButton' chip at the
top of Home) and search there. Never the Appium full-tree lookup.
"""
import types

import pytest

from automation.scenarios import cross_app_flows as caf
from automation.scenarios.cross_app_flows import FlowRunner

TOP = ["homeProductsBar", "homeCouponIcon", "favoriteButton", "Wallet", "Menu"]


def el(name, x=0, y=0, w=100, h=40, kind="Button"):
    return {"AXLabel": name, "AXIdentifier": name, "type": kind,
            "frame": {"x": x, "y": y, "width": w, "height": h}}


class Home:
    """Home's feed. The card shows after `scrolls_needed` page scrolls, or only once
    'Load More Restaurants' was tapped `loads_needed` times, or only in Favourites."""

    def __init__(self, scrolls_needed=0, loads_needed=0, only_in_favourites=False, never=False):
        self.scrolls_needed, self.loads_needed = scrolls_needed, loads_needed
        self.only_in_favourites, self.never = only_in_favourites, never
        self.depth = 0                 # pages scrolled down from the top
        self.loads = 0
        self.favourites = False
        self.opened = False
        self.taps = []

    def card_visible(self):
        if self.never:
            return False
        if self.only_in_favourites:
            return self.favourites
        return self.depth >= self.scrolls_needed and self.loads >= self.loads_needed

    def names(self):
        if self.opened:
            return ["Any", "anyBtn"]
        out = list(TOP) if self.depth == 0 else ["Wallet", "Menu", "LasanRestaurant"]
        if self.card_visible():
            out.append("NylaiKitchen2")
        if self.depth >= 2 and self.loads < self.loads_needed:
            out.append("Load More Restaurants")
        return out

    def describe_all(self, udid):
        return [el("Vya Consumer", 0, 0, 402, 874, "Application")] + [el(n) for n in self.names()]

    def tap(self, udid, names, els=None, scroll=True, wait=0.0):
        hit = next((n for n in names if n in self.names()), None)
        if hit is None:
            return False, "not on screen"
        self.taps.append(hit)
        if hit == "NylaiKitchen2":
            self.opened = True
        elif hit == "Load More Restaurants":
            self.loads += 1
        elif hit == "favoriteButton":
            self.favourites = True
        return True, "idb"

    def swipe(self, udid, x1, y1, x2, y2, duration=0.25):
        self.depth = self.depth + 1 if y2 < y1 else max(0, self.depth - 1)


@pytest.fixture
def home(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))

    def make(**kw):
        h = Home(**kw)
        monkeypatch.setattr(caf._idbd, "describe_all", h.describe_all)
        monkeypatch.setattr(caf._idbd, "tap", h.tap)
        monkeypatch.setattr(caf._idbd, "swipe", h.swipe)
        runner = FlowRunner.__new__(FlowRunner)
        runner._cur_udid = "IPHONE"
        runner.devices = {}
        r = types.SimpleNamespace(d=types.SimpleNamespace(
            find_elements=lambda *a: pytest.fail("must not fall back to the Appium lookup")))
        notes = []
        return runner._open_restaurant(r, notes), notes, h
    return make


def test_the_plain_step_reaches_the_searching_handler():
    assert FlowRunner._PLAIN_STEP_TOKENS["click NylaiKitchen2"] == "@open_restaurant"
    from automation.agentic.runinfo import token_ok
    assert token_ok("@open_restaurant")


def test_a_card_on_the_first_screen_is_tapped_without_scrolling(home):
    ok, notes, h = home()
    assert ok and h.depth == 0 and h.taps == ["NylaiKitchen2"]
    assert "opened the restaurant (Home, idb)" in notes[-1]


def test_a_card_below_the_fold_is_found_by_scrolling_down(home):
    ok, notes, h = home(scrolls_needed=2)
    assert ok and h.depth == 2
    assert "Home, scrolled 2×" in notes[-1]


def test_load_more_restaurants_is_tapped_and_the_search_goes_on(home):
    ok, notes, h = home(scrolls_needed=2, loads_needed=2)
    assert ok and h.taps.count("Load More Restaurants") == 2
    assert "Load More 2×" in notes[-1]


def test_not_on_home_then_favourites_is_searched(home):
    ok, notes, h = home(only_in_favourites=True)
    assert ok and "favoriteButton" in h.taps
    assert h.taps.index("favoriteButton") < h.taps.index("NylaiKitchen2")
    assert "opened the restaurant (Favourites" in notes[-1]


def test_nowhere_fails_with_the_screen_inside_the_step_timeout(home):
    ok, notes, h = home(never=True)
    assert not ok and "favoriteButton" in h.taps
    assert "not found on Home (scrolling down, Load More) nor in Favourites" in notes[-1]
    assert caf.time.time() <= FlowRunner.RESTAURANT_BUDGET_S + 15 < caf.STEP_TIMEOUT
