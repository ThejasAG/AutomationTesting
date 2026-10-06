"""The chef's job on a ticket: select every item -> Ready -> Close Order.

Source (vya-business, Components/KitchenCards + Screens/Home/kitchen.js):
  * an item with modifiers has a dot: a Radio labelled `${name}Btn`, spaces removed;
  * Ready is disabled until an item is selected (disabled={!readyActive(id)}) and
    marks ONLY the selected items completed (updatePrepare);
  * when every item is completed the button becomes Close Order (orderCloseBtn),
    which removes the ticket from the board.

Measured failure this replaces (ticket 4772): the step picked products by name
("anything ending in Btn that is not a known control"), took the new sidebar
`inventoryBtn` for a product, tapped it, left the board -- and then reported
"kitchen queue empty" over a board showing the order.
"""
import pytest

from automation.scenarios import cross_app_flows as caf
from automation.scenarios import idb_driver as dv
from automation.scenarios.cross_app_flows import FlowRunner


def _el(name, x, y, w, h, t="GenericElement"):
    return {"AXLabel": name, "type": t, "frame": {"x": x, "y": y, "width": w, "height": h}}


class Board:
    """A kitchen board that behaves like the app (layout measured on ticket 4772)."""

    def __init__(self, items=("TagliatellealSalmoneBtn", "SpaghettiallaPuttanescaBtn"),
                 ticket="4772", ready_works=True, dots_work=True, leftover_close=False):
        self.items, self.ticket = list(items), ticket
        self.selected, self.completed, self.closed = set(), set(), False
        self.ready_works, self.dots_work = ready_works, dots_work
        self.leftover_close = leftover_close
        self.taps = []

    def els(self):
        out = [{"AXLabel": "Vya Business", "type": "Application",
                "frame": {"x": 0, "y": 0, "width": 1210, "height": 834}},
               _el("My Orders", 123, 26, 124, 52, "StaticText"),
               _el("homeBtn", 30, 40, 36, 50), _el("inventoryBtn", 20, 155, 56, 50),
               _el("historyBtn", 26, 270, 44, 50), _el("menuBtn", 31, 385, 34, 50),
               _el("kitchenAllBtn", 560, 34, 50, 35)]
        if self.leftover_close:
            out += [_el("4100", 153, 164, 38, 18, "StaticText"),
                    _el("orderPrintBtn", 153, 424, 64, 48), _el("orderCloseBtn", 286, 424, 148, 48)]
            return out
        if self.closed:
            return out
        out.append(_el(self.ticket, 153, 164, 38, 18, "StaticText"))
        y = 320
        for it in self.items:
            if it not in self.completed:
                out.append(_el(it, 155, y, 170, 14))
            y += 49
        done = (all(i in self.completed for i in self.items) if self.items
                else getattr(self, "closed_ready", False))
        out += [_el("orderPrintBtn", 153, 424, 64, 48),
                _el("orderCloseBtn" if done else "orderReadyBtn", 286, 424, 148, 48)]
        return out

    def tap(self, udid, e, els=None, scroll=True):
        n = dv.name(e)
        self.taps.append(n)
        if n == "orderReadyBtn" and self.ready_works and (self.selected or not self.items):
            self.completed |= self.selected
            self.selected = set()
            if not self.items:
                self.closed_ready = True
        elif n == "orderCloseBtn":
            if self.leftover_close:
                self.leftover_close = False
            else:
                self.closed = True
        elif n in self.items and self.dots_work:
            self.selected.add(n)
        return True, "idb"

    def colours(self, udid, points, els=None, radius=1.5):
        out = []
        for x, y in points:
            hit = next((e for e in self.els()[1:]
                        if dv.frame(e)[0] <= x <= dv.frame(e)[0] + dv.frame(e)[2]
                        and dv.frame(e)[1] <= y <= dv.frame(e)[1] + dv.frame(e)[3]), None)
            n = dv.name(hit)
            if n == "orderReadyBtn":
                out.append((97, 69, 119) if (self.selected or not self.items)
                           else (177, 163, 187))
            elif n in self.selected:
                out.append((97, 69, 119))
            else:
                out.append((255, 255, 255))
        return out


@pytest.fixture
def kitchen(monkeypatch):
    def make(**kw):
        b = Board(**kw)
        monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: b.els())
        monkeypatch.setattr(caf._idbd, "tap_el", b.tap)
        monkeypatch.setattr(caf._idbd, "tap",
                            lambda udid, names, els=None, scroll=True:
                            b.tap(udid, {"AXLabel": names[0]}))
        monkeypatch.setattr(caf._idbd, "sample_colors", b.colours)
        monkeypatch.setattr(caf.time, "sleep", lambda s: None)
        runner = FlowRunner.__new__(FlowRunner)
        runner.devices = {"waiter": "IPAD", "kitchen": "IPAD"}

        class R:
            class d:
                @staticmethod
                def find_elements(*a):
                    return []
        notes = []
        ok = FlowRunner._kitchen_ready(runner, R(), notes)
        return ok, notes, b
    return make


def test_selects_every_item_then_ready_then_close(kitchen):
    ok, notes, b = kitchen()
    assert ok, notes
    assert b.taps == ["TagliatellealSalmoneBtn", "SpaghettiallaPuttanescaBtn",
                      "orderReadyBtn", "orderCloseBtn"]
    assert b.closed
    assert "Ready → Close Order" in notes[-1] and "4772" in notes[-1]


def test_nav_rail_is_never_taken_for_a_product(kitchen):
    ok, notes, b = kitchen()
    assert "inventoryBtn" not in b.taps and "historyBtn" not in b.taps


def test_card_without_modifier_rows_goes_straight_to_ready(kitchen):
    # Plain items render no dot; Ready is enabled from the start in the app.
    ok, notes, b = kitchen(items=())
    assert ok, notes
    assert b.taps == ["orderReadyBtn", "orderCloseBtn"]


def test_dots_that_do_not_take_fail_loudly(kitchen):
    ok, notes, b = kitchen(dots_work=False)
    assert not ok
    assert "none shows as selected" in notes[-1]
    assert "orderReadyBtn" not in b.taps, "never press a disabled Ready"


def test_ready_that_changes_nothing_fails(kitchen):
    ok, notes, b = kitchen(ready_works=False)
    assert not ok and "no item was marked completed" in notes[-1]


def test_closing_a_leftover_ticket_is_not_success(kitchen):
    ok, notes, b = kitchen(leftover_close=True)
    assert not ok and "only closed a leftover" in notes[-1]


def test_empty_board_fails_with_the_reason(kitchen, monkeypatch):
    b = Board()
    b.closed = True
    monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: b.els())
    monkeypatch.setattr(caf.time, "sleep", lambda s: None)
    t = iter(range(0, 1000, 5))
    monkeypatch.setattr(caf.time, "time", lambda: next(t))
    runner = FlowRunner.__new__(FlowRunner)
    runner.devices = {"waiter": "IPAD"}
    notes = []
    assert FlowRunner._kitchen_ready(runner, object(), notes) is False
    assert "queue empty" in notes[-1]


def test_an_already_selected_dot_is_not_toggled_off(kitchen, monkeypatch):
    # A dot toggles; tapping a selected one unselects it.
    orig = Board.__init__

    def preselected(self, **kw):
        orig(self, **kw)
        self.selected = {"TagliatellealSalmoneBtn"}
    monkeypatch.setattr(Board, "__init__", preselected)
    ok, notes, b = kitchen()
    assert ok, notes
    assert b.taps == ["SpaghettiallaPuttanescaBtn", "orderReadyBtn", "orderCloseBtn"]


# -- this run's ticket, not the first on the board ---------------------------------
# Measured 2026-09-30: ticket 4837, stuck on the board after a failed run, was first
# in the queue; three later runs worked on it instead of the order they had sent.

def _two_cards(second_kind="orderReadyBtn"):
    app = {"AXLabel": "Vya Business", "type": "Application",
           "frame": {"x": 0, "y": 0, "width": 1210, "height": 834}}
    return [app, _el("My Orders", 123, 26, 124, 52, "StaticText"),
            _el("4837", 153, 164, 38, 18, "StaticText"), _el("PennePolloBtn", 155, 240, 170, 14),
            _el("orderPrintBtn", 153, 285, 64, 48), _el("orderReadyBtn", 285, 285, 148, 48),
            _el("4841", 503, 164, 39, 18, "StaticText"), _el("TagliatellealSalmoneBtn", 505, 240, 170, 14),
            _el("orderPrintBtn", 503, 283, 64, 48), _el(second_kind, 636, 283, 148, 48)]


def _runner(monkeypatch, els, ticket):
    monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: els)
    monkeypatch.setattr(caf.time, "sleep", lambda s: None)
    runner = FlowRunner.__new__(FlowRunner)
    runner.devices = {"waiter": "IPAD", "kitchen": "IPAD"}
    runner._booked_ticket = ticket
    return runner


def test_the_kitchen_works_on_this_runs_ticket(monkeypatch):
    runner = _runner(monkeypatch, _two_cards(), "4841")
    picked = []
    monkeypatch.setattr(FlowRunner, "_kitchen_select_items",
                        lambda self, r, udid, card, els, notes: picked.append(card["ticket"]) or [])
    FlowRunner._kitchen_ready(runner, None, [])
    assert picked == ["4841"], "4837 is first on the board but is another run's leftover"


def test_a_ticket_that_never_arrived_is_not_swapped_for_another(monkeypatch):
    runner = _runner(monkeypatch, _two_cards(), "4850")
    monkeypatch.setattr(FlowRunner, "_kitchen_select_items",
                        lambda *a: pytest.fail("must not touch another run's ticket"))
    notes = []
    assert not FlowRunner._kitchen_ready(runner, None, notes)
    assert "ticket 4850 (this run's order) is not on the kitchen board" in notes[-1]
    assert "4837" in notes[-1] and "4841" in notes[-1]


def test_a_ticket_already_prepared_is_just_closed(monkeypatch):
    # A resumed run: Ready went through last time, only Close Order is left.
    runner = _runner(monkeypatch, _two_cards("orderCloseBtn"), "4841")
    tapped = []
    monkeypatch.setattr(caf._idbd, "tap_el",
                        lambda udid, e, els=None, scroll=True:
                        tapped.append((dv.name(e), dv.frame(e)[0])) or (True, "idb"))
    notes = []
    assert FlowRunner._kitchen_ready(runner, None, notes)
    assert tapped == [("orderCloseBtn", 636)] and "already prepared" in notes[-1]
