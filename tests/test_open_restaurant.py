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


SHEET = (0, 62, 402, 778)          # the search sheet, as idb sees it (MEASURED 2026-10-28)


class Home:
    """Home's feed. The card shows after `scrolls_needed` page scrolls, or only once
    'Load More Restaurants' was tapped `loads_needed` times, or only in Favourites.
    With `search=True` Home also has its search bar; `search_finds` says whether the
    search lists the restaurant. The open search sheet is ONE idb element ('Search'):
    its input and results are only readable as text (OCR)."""

    def __init__(self, scrolls_needed=0, loads_needed=0, only_in_favourites=False, never=False,
                 search=False, search_finds=True, search_open=False):
        self.scrolls_needed, self.loads_needed = scrolls_needed, loads_needed
        self.only_in_favourites, self.never = only_in_favourites, never
        self.search, self.search_finds = search, search_finds
        self.searching = search_open   # the search sheet is up (left open by a run)
        self.typed = ""
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
        if self.searching:
            return list(TOP) + ["homeSearchBar"]          # Home stays behind the sheet
        out = list(TOP) if self.depth == 0 else ["Wallet", "Menu", "LasanRestaurant"]
        if self.search and self.depth == 0:
            out.append("homeSearchBar")
        if self.card_visible():
            out.append("NylaiKitchen2")
        if self.depth >= 2 and self.loads < self.loads_needed:
            out.append("Load More Restaurants")
        return out

    def describe_all(self, udid):
        out = [el("Vya Consumer", 0, 0, 402, 874, "Application")] + [el(n) for n in self.names()]
        if self.searching and not self.opened:
            out.append(el("Search", *SHEET, kind="GenericElement"))
        return out

    def read_text(self, udid, region=None, els=None, accurate=False):
        """OCR of the sheet: the input row, then the result line and the result."""
        if not self.searching or self.opened:
            return []
        out = [(self.typed or "What are you looking for ?", 56, 85, 187, 15)]
        if self.typed:
            n = 1 if self.search_finds else 0
            out.append((f"{'I' if n else 'O'} result(s) found for << {self.typed} >>", 16, 189, 254, 13))
            if n:
                out.append(("Nylai Kitchen2", 67, 231, 94, 14))
        return out

    def tap_point(self, udid, x, y):
        if self.searching and x < 40 and y < 110:          # the sheet's back arrow
            self.taps.append("search back")
            self.searching, self.typed = False, ""
        elif self.searching and 220 <= y <= 250 and self.typed and self.search_finds:
            self.taps.append("Nylai Kitchen2 result")
            self.opened = True
        elif self.searching and y < 110:
            self.taps.append("search input")
        return True, "idb"

    def idb(self, args, timeout=20.0):
        if args[:2] == ["ui", "text"]:
            self.typed = args[2]
        elif args[:2] == ["ui", "key-sequence"]:
            self.typed = ""
        return ""

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
        elif hit == "homeSearchBar":
            self.searching = True
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
        monkeypatch.setattr(caf._idbd, "tap_point", h.tap_point)
        monkeypatch.setattr(caf._idbd, "_idb", h.idb)
        from automation.scenarios import screen_text
        monkeypatch.setattr(screen_text, "read_text", h.read_text)
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
    assert "Home, idb)" in notes[-1]


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
    assert "Favourites" in notes[-1] and "[ok]" in notes[-1]


def test_nowhere_fails_with_the_screen_inside_the_step_timeout(home):
    ok, notes, h = home(never=True)
    assert not ok and "favoriteButton" in h.taps
    assert "not found on Home (scrolling down, Load More) nor in Favourites" in notes[-1]
    assert caf.time.time() <= FlowRunner.RESTAURANT_BUDGET_S + 15 < caf.STEP_TIMEOUT


# ── by Home's search first (2026-10-28) ──────────────────────────────────────

def test_the_search_opens_the_restaurant_without_scrolling_home(home):
    """MEASURED 2026-10-27: 12 runs, the card always behind 2 scrolls + Load More,
    36-113s. The search answers by name."""
    ok, notes, h = home(scrolls_needed=2, loads_needed=1, search=True)
    assert ok and h.taps == ["homeSearchBar", "search input", "Nylai Kitchen2 result"]
    assert h.typed == "Nylai Kitchen2"           # the display name: the card id finds nothing
    assert h.depth == 0 and h.loads == 0
    assert "searched 'Nylai Kitchen2', tapped its result" in notes[-1]
    assert caf.time.time() < 15


def test_a_search_sheet_left_open_by_an_earlier_run_is_used_as_it_is(home):
    ok, notes, h = home(search=True, search_open=True)
    assert ok and "homeSearchBar" not in h.taps and "tapped its result" in notes[-1]


def test_when_the_search_does_not_list_it_home_is_scrolled_as_before(home):
    ok, notes, h = home(scrolls_needed=2, loads_needed=1, search=True, search_finds=False)
    assert ok and h.loads == 1
    assert h.taps.index("search back") < h.taps.index("Load More Restaurants")
    assert "search did not open it" in notes[-1] and "Load More 1×" in notes[-1]


def test_search_and_scrolling_share_one_budget_inside_the_step_timeout(home):
    ok, notes, h = home(never=True, search=True, search_finds=False)
    assert not ok and "search did not open it" in notes[-1]
    assert caf.time.time() <= FlowRunner.RESTAURANT_BUDGET_S + 15 < caf.STEP_TIMEOUT
