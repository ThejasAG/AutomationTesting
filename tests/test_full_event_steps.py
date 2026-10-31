"""The steps the full-event flow (flow_full_event) added, on scripted screens.

MEASURED 2026-10-29 on the simulators while building the flow:
  * C-App menu: only the tiles near the viewport are in the tree, so a product is
    found through the menu's own search (the sticky bar's '\\ue670' glyph, then a
    TextField 'Search for an item'); a tile is '<pid>Dec', its count, '<pid>Inc';
    the first '+' opens the options sheet (rows '<opt>Btn <price>', counterPlus,
    confirmProduct); a cart of another booking asks first (deletePrevYes).
  * `idb ui text` mangles non-ASCII: 'Coca-Cola® Light 1.0l' landed as
    'Coca-Colaight 1.0l', so only the ASCII head of a name is typed.
  * B-App ADD sheet: options are '<opt>CheckBtn' on the installed build; the
    assign dialog has two 'assignProductsBtn' (left Assign, right Split).
  * My Contacts: the search bar is a container ('contactSearch'), a contact row is
    its name ('Noolu Naga'), and picking it adds the chip 'Noolucancel'.
"""
import types

import pytest

from automation.scenarios import cross_app_flows as caf
from automation.scenarios.cross_app_flows import FlowRunner


def el(name, x=0, y=0, w=100, h=40, kind="GenericElement"):
    return {"AXLabel": name, "AXIdentifier": name, "type": kind,
            "frame": {"x": x, "y": y, "width": w, "height": h}}


@pytest.fixture
def clock(monkeypatch):
    t = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: t["t"], sleep=lambda s: t.__setitem__("t", t["t"] + s)))
    return t


def install(monkeypatch, screen):
    for fn in ("describe_all", "tap", "tap_el", "tap_point", "swipe", "_idb", "fill"):
        if hasattr(screen, fn):
            monkeypatch.setattr(caf._idbd, fn, getattr(screen, fn))


# ── C-App menu: @c_add ────────────────────────────────────────────────────────

class Menu:
    """The C-App menu with its search, a product's options sheet and the cart."""

    PRODUCTS = {
        "PennePollo": {"options": {}, "groups": []},
        "Coca-Cola®Light1.0l": {"options": {}, "groups": []},
        "PizzaMargheritas": {"options": {"small": 0, "large": 10, "veg": 2.28, "nonveg": 1.03},
                             "groups": [{"small", "large"}, {"veg", "nonveg"}]},
    }

    def __init__(self, other_booking_cart=False):
        self.dragged = self.searching = False
        self.typed = ""
        self.counts = {p: 0 for p in self.PRODUCTS}
        self.sheet = None                     # {"pid", "opts", "qty"}
        self.dialog = None
        self.other_cart = other_booking_cart  # asks on the first '+'
        self._pending = None
        self.taps = []

    def describe_all(self, udid):
        out = [el("Vya Consumer", 0, 0, 402, 874, "Application"),
               el("cartImage", 20, 778, 362, 48)]
        if self.dialog == "prev":
            return out + [el("deletePrevYes", 50, 500), el("deletePrevNo", 220, 500)]
        if self.dialog == "custom":
            return out + [el("closeAddingProduct", 150, 100), el("addNewCustomSelection", 30, 700)]
        if self.sheet:
            opts = self.PRODUCTS[self.sheet["pid"]]["options"]
            return out + [el(f"{o}Btn {p:g}", 30, 300 + 44 * i) for i, (o, p) in enumerate(opts.items())] \
                + [el("counterMinus", 40, 760, 20, 20), el("counterPlus", 90, 760, 20, 20),
                   el(f"Add to cart ∙ 10.30 €", 150, 750, 230, 50), el("confirmProduct", 150, 750, 230, 50)]
        if not self.searching:
            out.append(el("pasta-category", 0, 285, 101, 42))
            if self.dragged:
                out.append(el("", 347, 45, 55, 41))
            return out
        out.append(el("", 42, 45, 321, 49, "TextField"))
        if self.typed:
            out.append(el("", 369, 59, 33, 36))
        y = 334
        for pid in self.PRODUCTS:
            if self.typed and self.typed.replace(" ", "") in pid:
                out += [el(f"{pid}Dec", 13, y, 50, 40), el(str(self.counts[pid]), 91, y + 7, 14, 25, "StaticText"),
                        el(f"{pid}Inc", 132, y, 51, 40), el(pid, 13, y + 45, 169, 18)]
                y += 300
        return out

    def _press(self, n, e=None):
        self.taps.append(n)
        if n == "":
            self.searching = True
        elif n == "":
            self.typed = ""
        elif n.endswith("Inc"):
            pid = n[:-3]
            if self.other_cart:
                self.dialog, self.other_cart = "prev", False
            elif self.dialog is None:
                if self.counts[pid]:
                    self.dialog = "custom"
                else:
                    self.sheet = {"pid": pid, "opts": set(), "qty": 1}
            self._pending = pid
        elif n == "deletePrevYes":
            self.dialog = None
            self.counts = {p: 0 for p in self.counts}
            self.sheet = {"pid": self._pending, "opts": set(), "qty": 1}
        elif n == "addNewCustomSelection":
            self.dialog = None
            self.sheet = {"pid": self._pending, "opts": set(), "qty": 1}
        elif self.sheet and "Btn " in n:
            opt = n.split("Btn ")[0]
            for g in self.PRODUCTS[self.sheet["pid"]]["groups"]:
                if opt in g:
                    self.sheet["opts"] -= g
            self.sheet["opts"].add(opt)
        elif n == "counterPlus" and self.sheet:
            self.sheet["qty"] += 1
        elif n == "confirmProduct" and self.sheet:
            groups = self.PRODUCTS[self.sheet["pid"]]["groups"]
            if all(g & self.sheet["opts"] for g in groups):
                self.counts[self.sheet["pid"]] += self.sheet["qty"]
                self.sheet = None
        return True, "idb"

    def tap_el(self, udid, e, els=None, scroll=True):
        return self._press(caf._idbd.name(e) or e.get("type"), e)

    def tap(self, udid, names, els=None, scroll=True, wait=0.0):
        have = {caf._idbd.name(e) for e in self.describe_all(udid)}
        hit = next((n for n in names if n in have), None)
        return self._press(hit) if hit else (False, "not on screen")

    def tap_point(self, udid, x, y):
        return True, "idb"

    def swipe(self, udid, x1, y1, x2, y2, duration=0.25):
        if y2 < y1:
            self.dragged = True

    def _idb(self, args, timeout=20.0):
        if args[:2] == ["ui", "text"]:
            self.typed = args[-1]
        return ""


@pytest.fixture
def menu(monkeypatch, clock):
    def make(spec, **kw):
        m = Menu(**kw)
        install(monkeypatch, m)
        fr = FlowRunner.__new__(FlowRunner)
        fr._cur_udid, fr.devices = "IPHONE", {}
        notes = []
        return fr._c_add_product(spec, notes), notes, m
    return make


def test_c_add_is_a_known_token():
    from automation.agentic.runinfo import token_ok
    assert token_ok("@c_add:Pizza Margheritas*2[large, veg]")
    assert token_ok("@w_add:Penne Pollo*3 > split: Guest 1, Guest 2")


def test_c_add_searches_picks_the_options_and_sets_the_quantity_in_the_sheet(menu):
    ok, notes, m = menu("Pizza Margheritas*2[large, veg]")
    assert ok, notes
    assert m.typed == "Pizza Margheritas"
    assert m.counts["PizzaMargheritas"] == 2
    assert m.taps.count("counterPlus") == 1, "quantity is set in the sheet, not by a 2nd '+'"
    assert "added 2 × Pizza Margheritas (large, veg); " in notes[-1] and "tile 0 → 2" in notes[-1]


def test_c_add_types_only_the_ascii_head_of_a_name(menu):
    ok, notes, m = menu("Coca-Cola® Light 1.0l")
    assert ok, notes
    assert m.typed == "Coca-Cola"
    assert m.counts["Coca-Cola®Light1.0l"] == 1


def test_c_add_names_the_missing_option_when_add_to_cart_does_nothing(menu):
    ok, notes, m = menu("Pizza Margheritas[large]")
    assert not ok
    assert "Add to cart did nothing: a required option is not chosen" in notes[-1]


def test_c_add_lists_what_the_sheet_offers_for_an_unknown_option(menu):
    ok, notes, _m = menu("Pizza Margheritas[huge]")
    assert not ok
    assert "no option 'huge'" in notes[-1] and "large" in notes[-1]


def test_c_add_empties_another_bookings_cart_first(menu):
    ok, notes, m = menu("Penne Pollo", other_booking_cart=True)
    assert ok, notes
    assert "deletePrevYes" in m.taps
    assert any("emptied it" in n for n in notes)


def test_a_second_line_of_a_dish_in_the_cart_goes_through_custom_selection(monkeypatch, clock):
    m = Menu()
    m.counts["PizzaMargheritas"] = 2
    install(monkeypatch, m)
    fr = FlowRunner.__new__(FlowRunner)
    fr._cur_udid, fr.devices = "IPHONE", {}
    notes = []
    assert fr._c_add_product("Pizza Margheritas[small, nonveg]", notes), notes
    assert "addNewCustomSelection" in m.taps and m.counts["PizzaMargheritas"] == 3


def test_search_text_stops_at_the_first_non_ascii_character():
    st = FlowRunner._search_text
    assert st("Coca-Cola® Light 1.0l") == "Coca-Cola"
    assert st("Pizza Margheritas") == "Pizza Margheritas"
    assert st("Schöfferhofer Hefeweizen") == "Sch", "the product is then picked by its id"
    assert st("Öl Pasta") == "Öl Pasta", "no usable ASCII head: the whole name"


# ── B-App ADD sheet: @w_add ───────────────────────────────────────────────────

class Ipad:
    """The waiter's order screen, its ADD NEW ITEM sheet, options pane and the
    'Assign to or split among' dialog. Prices: Penne Pollo 17.51, pizza 22.58."""

    PROFILES = ["RoopaD", "NooluNaga", "Guest1", "Guest2"]
    OPTIONS = {"PizzaMargheritas": ["small", "large", "veg", "nonveg"], "PennePollo": []}
    PRICE = {"PennePollo": 17.51, "PizzaMargheritas": 22.58}

    def __init__(self):
        self.rows = []                 # (pid, price)
        self.sheet = self.pane = self.dialog = False
        self.staged = {}               # pid -> qty
        self.ticked = set()
        self.picked = set()
        self.custom = None             # the Custom Selections panel: pid
        self.taps = []

    def describe_all(self, udid):
        out = [el("Vya Business", 0, 0, 1210, 834, "Application"), el("homeBtn", 30, 40, 36, 50),
               el("historyBtn", 26, 155, 43, 50)]
        if self.dialog:
            for i, p in enumerate(self.PROFILES):
                out.append(el(f"{p}select", 300 + 90 * i, 300, 60, 60))
                if p in self.picked:
                    out.append(el(f"{p}close", 340 + 90 * i, 290, 20, 20))
            return out + [el("assignProductsBtn", 300, 600, 200, 60),
                          el("assignProductsBtn", 650, 600, 200, 60)]
        if self.sheet:
            out += [el("addNewItemInput", 153, 134, 607, 21, "TextField"),
                    el("addNewItemClose", 715, 35, 40, 40), el("assignToBtn", 202, 704, 481, 64)]
            out += [el(f"{p}Item", 97, 321 + 68 * i, 690, 68) for i, p in enumerate(self.OPTIONS)]
            if self.custom:
                n = self.staged[self.custom]
                return out + [el("Custom Selections", 810, 138, 399, 20, "StaticText"),
                              el(f"{self.custom} 1.00 € Minus {n} Plus", 795, 163, 414, 160)]
            for i, (p, n) in enumerate(self.staged.items()):
                y = 156 + 40 * i
                out += [el(f"{p}card", 827, y, 126, 36), el("removeItem", 1100, y + 13, 30, 10),
                        el(str(n), 1137, y + 8, 9, 21, "StaticText"),
                        el("incrementQuantity", 1153, y + 13, 30, 10)]
            if self.pane:
                out += [el(f"{o}CheckBtn", 807, 300 + 44 * i, 180, 44)
                        for i, o in enumerate(self.OPTIONS[self.pane])]
                out.append(el("applyOptionBtn", 816, 684, 373, 64))
            return out
        out.append(el("addItemsBtn", 700, 760, 100, 40))
        for i, (pid, price) in enumerate(self.rows):
            out += [el(f"{pid}card", 123, 213 + 43 * i, 230, 22),
                    el(f"{price:.2f}€", 691, 213 + 43 * i, 64, 18, "StaticText")]
        return out

    def _press(self, n):
        self.taps.append(n)
        if n == "addItemsBtn":
            self.sheet = True
        elif n.endswith("Item") and self.sheet:
            pid = n[:-4]
            if self.OPTIONS[pid]:
                self.pane, self.ticked = pid, set()
            else:
                self.staged[pid] = self.staged.get(pid, 0) + 1
        elif n.endswith("CheckBtn"):
            self.ticked.add(n[:-8])
        elif n == "applyOptionBtn" and self.pane:
            if {"small", "large"} & self.ticked and {"veg", "nonveg"} & self.ticked:
                self.staged[self.pane] = self.staged.get(self.pane, 0) + 1
                self.pane = False
        elif n == "assignToBtn" and self.staged:
            self.dialog = True
        elif n == "incrementQuantity" and self.staged:
            self.custom = next(iter(self.staged))
        elif n.endswith("select"):
            self.picked.add(n[:-6])
        return True, "idb"

    def press_assign(self, x):
        for pid, qty in self.staged.items():
            share = self.PRICE[pid] * qty / (1 if x < 600 else len(self.picked))
            self.rows += [(pid, share) for _ in self.picked]
        self.sheet = self.dialog = False
        self.staged, self.picked = {}, set()
        self.taps.append("Assign" if x < 600 else "Split")
        return True, "idb"

    def tap_el(self, udid, e, els=None, scroll=True):
        if caf._idbd.name(e) == "assignProductsBtn":
            return self.press_assign(caf._idbd.frame(e)[0])
        return self._press(caf._idbd.name(e))

    def tap(self, udid, names, els=None, scroll=True, wait=0.0):
        have = {caf._idbd.name(e) for e in self.describe_all(udid)}
        hit = next((n for n in names if n in have), None)
        return self._press(hit) if hit else (False, "not on screen")

    def fill(self, udid, field, text, close_keyboard=True, info=None):
        self.searched = text
        return True, "idb"

    def read_text(self, udid, region=None, els=None, accurate=False):
        return [("X", 951, 272, 12, 12), ("1", 980, 271, 14, 13), ("+", 1008, 268, 19, 19)] \
            if self.custom else []

    def tap_point(self, udid, x, y):
        if self.custom and 1000 <= x <= 1030 and 260 <= y <= 290:
            self.staged[self.custom] += 1
            self.taps.append("+")
        elif self.custom and y < 120:                # the panel's handle
            self.custom = None
            self.taps.append("handle")
        return True, "idb"


@pytest.fixture
def ipad(monkeypatch, clock):
    def make(spec, screen=None):
        s = screen or Ipad()
        install(monkeypatch, s)
        from automation.scenarios import screen_text
        monkeypatch.setattr(screen_text, "read_text", s.read_text)
        monkeypatch.setattr(FlowRunner, "_option_ticked",
                            staticmethod(lambda udid, row, els: caf._idbd.name(row)[:-8] in s.ticked))
        fr = FlowRunner.__new__(FlowRunner)
        fr._cur_udid, fr.devices = "IPAD", {}
        notes = []
        return fr._w_add_item(None, spec, notes), notes, s
    return make


@pytest.mark.parametrize("spec", ["Penne Pollo*3", "Penne Pollo*3 > give: Guest 1",
                                  "Penne Pollo*3 > split: Guest 1"])
def test_w_add_says_how_to_write_it(ipad, spec):
    ok, notes, s = ipad(spec)
    assert not ok and "write it as" in notes[-1] and s.taps == []


def test_w_add_picks_checkbtn_options_once_and_sets_the_quantity_with_the_summary_plus(ipad):
    ok, notes, s = ipad("Pizza Margheritas*2[large, veg] > assign: Guest 1, Noolu Naga")
    assert ok, notes
    assert s.taps.count("PizzaMargheritasItem") == 1, "one tap, then the Summary's '+'"
    assert s.taps.count("largeCheckBtn") == 1 and "Assign" in s.taps
    assert s.taps.count("+") == 1 and "handle" in s.taps
    assert "homeBtn" not in s.taps and "historyBtn" not in s.taps, "the side bar is not an option"
    assert [p for p, _ in s.rows] == ["PizzaMargheritas"] * 2
    assert "0 → 2 row(s)" in notes[-1] and "+90.32 € on the order" in notes[-1]


def test_w_add_splits_with_the_right_button(ipad):
    ok, notes, s = ipad("Penne Pollo*3 > split: Guest 1, Guest 2")
    assert ok, notes
    assert "Split" in s.taps and len(s.rows) == 2
    assert "split among Guest 1, Guest 2" in notes[-1]


def test_w_add_lists_the_offered_options_for_an_unknown_one(ipad):
    ok, notes, _s = ipad("Pizza Margheritas[huge] > assign: Guest 1")
    assert not ok
    assert "no 'huge'" in notes[-1] and "large" in notes[-1] and "home" not in notes[-1]


def test_w_add_names_a_profile_that_is_not_on_the_dialog(ipad):
    ok, notes, _s = ipad("Penne Pollo > assign: Guest 9")
    assert not ok and "no 'Guest 9' on the dialog" in notes[-1]


# ── My Contacts: @invite_guests:N+Name ────────────────────────────────────────

class Contacts:
    """The reservation form's Persons stepper and the My Contacts sheet."""

    def __init__(self, phone_contacts=("Anna Haro", "Noolu Naga")):
        self.contacts = list(phone_contacts)
        self.open = False
        self.guests, self.people, self.typed = 0, [], ""
        self.persons = 1

    def describe_all(self, udid):
        out = [el("Vya Consumer", 0, 0, 402, 874, "Application")]
        if not self.open:
            return out + [el("counterMinus", 100, 500, 20, 20), el(str(self.persons), 130, 498, 14, 24, "StaticText"),
                          el("counterPlus", 160, 500, 20, 20), el("counterMinus", 260, 500, 20, 20),
                          el("0", 290, 498, 14, 24, "StaticText"), el("counterPlus", 320, 500, 20, 20)]
        out += [el("contactSearch " if self.typed else "contactSearch", 20, 129, 362, 40),
                el("guestAdd", 12, 340, 378, 63), el("inviteUsers", 20, 780, 362, 50)]
        out += [el(f"Guest {i + 1}cancel", 160 + 90 * i, 253, 30, 36) for i in range(self.guests)]
        out += [el(f"{p.split()[0]}cancel", 700 + 90 * i, 253, 30, 36) for i, p in enumerate(self.people)]
        shown = [c for c in self.contacts if not self.typed or c.startswith(self.typed)]
        return out + [el(c, 12, 520 + 60 * i, 378, 40, "Button") for i, c in enumerate(shown)]

    def _press(self, n):
        if n == "counterPlus" and not self.open:
            self.open = True
        elif n == "guestAdd":
            self.guests += 1
        elif n in self.contacts and n not in self.people:
            self.people.append(n)
        elif n == "inviteUsers":
            self.open = False
            self.persons = 1 + self.guests + len(self.people)
        return True, "idb"

    def tap_el(self, udid, e, els=None, scroll=True):
        n = caf._idbd.name(e)
        return self._press("counterPlus" if n == "counterPlus" else n)

    def tap(self, udid, names, els=None, scroll=True, wait=0.0):
        have = {caf._idbd.name(e) for e in self.describe_all(udid)}
        hit = next((n for n in names if n in have), None)
        return self._press(hit) if hit else (False, "not on screen")

    def _idb(self, args, timeout=20.0):
        if args[:2] == ["ui", "text"]:
            self.typed = args[-1]
        return ""


def invite(monkeypatch, screen, count, contacts):
    install(monkeypatch, screen)
    t = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: t["t"], sleep=lambda s: t.__setitem__("t", t["t"] + s)))
    fr = FlowRunner.__new__(FlowRunner)
    fr._cur_udid, fr.devices = "IPHONE", {"consumer": "IPHONE"}
    notes = []
    return fr._invite_guests(count, notes, contacts), notes


def test_invite_guests_and_a_contact_found_through_the_search(monkeypatch):
    c = Contacts()
    ok, notes = invite(monkeypatch, c, 2, ["Noolu Naga"])
    assert ok, notes
    assert c.typed == "Noolu" and c.people == ["Noolu Naga"]
    assert "invited 2 guest(s) + Noolu Naga; Persons 1 → 4" in notes[-1]


def test_a_contact_missing_from_the_phone_fails_with_how_to_fix_it(monkeypatch):
    ok, notes = invite(monkeypatch, Contacts(phone_contacts=("Anna Haro",)), 1, ["Noolu Naga"])
    assert not ok
    assert "is not in this phone's contacts" in notes[-1] and "simulator's Contacts" in notes[-1]


def test_the_contact_token_form_reaches_the_handler(monkeypatch):
    seen = {}
    fr = FlowRunner.__new__(FlowRunner)
    monkeypatch.setattr(FlowRunner, "_invite_guests",
                        lambda self, n, notes, contacts=(): seen.update(n=n, c=contacts) or True)
    assert FlowRunner._handle_special(fr, None, "@invite_guests:5+Noolu Naga", [])
    assert seen == {"n": 5, "c": ["Noolu Naga"]}


# ── Phase 3: @w_qty in a profile's card ───────────────────────────────────────

class Cards:
    """The payment panel: profile cards, Guest 5's open card holding the line
    'Pizza Salamees x <n>' with decrementQuantity / removeItem, the count,
    incrementQuantity (MEASURED 2026-10-29 on the installed build)."""

    def __init__(self, n=5):
        self.n = n
        self.taps = []

    def describe_all(self, udid):
        out = [el("Vya Business", 0, 0, 1210, 834, "Application")]
        for i, g in enumerate(["Guest4", "Guest5", "Guest6"]):
            out.append(el(f"G {g}accordionCard ", 834, 100 + 300 * i, 336, 49))
        if self.n:
            out += [el(f"Pizza Salamees x {self.n}", 839, 486, 229, 16, "StaticText"),
                    el("decrementQuantity" if self.n >= 2 else "removeItem", 1078, 489, 30, 10),
                    el(str(self.n), 1115, 484, 11, 21, "StaticText"),
                    el("incrementQuantity", 1133, 489, 30, 10)]
        return out

    def tap_el(self, udid, e, els=None, scroll=True):
        n = caf._idbd.name(e)
        self.taps.append(n)
        self.n += {"incrementQuantity": 1, "decrementQuantity": -1}.get(n, 0)
        if n == "removeItem":
            self.n = 0
        return True, "idb"


def qty(monkeypatch, clock, spec, n=5):
    c = Cards(n)
    install(monkeypatch, c)
    fr = FlowRunner.__new__(FlowRunner)
    fr._cur_udid, fr.devices = "IPAD", {}
    notes = []
    return fr._w_qty(spec, notes), notes, c


def test_w_qty_raises_the_line_in_that_profiles_card(monkeypatch, clock):
    ok, notes, c = qty(monkeypatch, clock, "Guest 5/Pizza Salamees +1")
    assert ok and c.n == 6 and "5 → 6" in notes[-1]


def test_w_qty_remove_steps_down_to_one_then_removes(monkeypatch, clock):
    ok, notes, c = qty(monkeypatch, clock, "Guest 5/Pizza Salamees remove", n=2)
    assert ok, notes
    assert c.taps == ["decrementQuantity", "removeItem"] and c.n == 0


def test_w_qty_on_another_profile_does_not_touch_guest_5(monkeypatch, clock):
    ok, notes, c = qty(monkeypatch, clock, "Guest 4/Pizza Salamees +1")
    assert not ok and "incrementQuantity" not in c.taps, "only Guest 4's card was opened"
    assert "shows no quantity control" in notes[-1]


@pytest.mark.parametrize("spec,ok", [("Penne Pollo@Guest 1*all", True), ("Penne Pollo@Guest 1*2", True),
                                     ("Penne Pollo@Guest 1 > Guest 2", True), ("Penne Pollo", False)])
def test_on_owner_specs_parse(spec, ok):
    assert bool(FlowRunner._W_ON_RE.match(spec)) is ok


def test_w_comp_asks_for_a_chip_the_dialog_has(monkeypatch, clock):
    fr = FlowRunner.__new__(FlowRunner)
    fr._cur_udid, fr.devices = "IPAD", {}
    notes = []
    assert fr._w_comp("Pizza Margheritas@Guest 4*1:12", notes) is False
    assert "pct one of 5, 10, 15, 25, 30, 40, 100" in notes[-1]


# ── the 'which items' Overlay ─────────────────────────────────────────────────

class Units:
    """CompAndVoidProductModal: ONE element with every id, the count among them."""

    def __init__(self, n=1, top=3):
        self.n, self.top = n, top

    def describe_all(self, udid):
        return [el("Vya Business", 0, 0, 1210, 834, "Application"),
                el(f"Select which items to Void Penne Pollo removeItem {self.n} "
                   f"incrementQuantity ApplyBtn", 0, 0, 1210, 834)]

    def read_text(self, udid, region=None, els=None, accurate=False):
        return [("Penne Pollo", 336, 339, 100, 14), (f"(× {self.n}+", 792, 329, 87, 30)]

    def tap_point(self, udid, x, y):
        if x > 860:
            self.n = min(self.top, self.n + 1)
        return True, "idb"


@pytest.mark.parametrize("want,got", [(None, 3), (2, 2), (1, 1)])
def test_units_set_counts_up_to_what_is_asked_or_all(monkeypatch, clock, want, got):
    u = Units()
    install(monkeypatch, u)
    from automation.scenarios import screen_text
    monkeypatch.setattr(screen_text, "read_text", u.read_text)
    fr = FlowRunner.__new__(FlowRunner)
    assert fr._units_set("IPAD", "ApplyBtn", want, "@w_void", []) == got


# ── Phase 7: @c_confirm_order ─────────────────────────────────────────────────

class Cart:
    def __init__(self, during_event=True):
        self.during, self.open, self.state = during_event, False, "menu"

    def describe_all(self, udid):
        out = [el("Vya Consumer", 0, 0, 402, 874, "Application")]
        if self.state == "menu":
            return out + [el("cartImage", 20, 778, 362, 48)]
        if self.state == "cart":
            return out + ([el("CONFIRM ORDER", 20, 697, 362, 56), el("cartAddItems", 20, 773, 362, 56)]
                          if self.during else [el("cartCheckout", 20, 697, 362, 56)])
        if self.state == "confirmed":
            return out + [el("orderConfirmedMenu", 50, 500, 300, 50)]
        return out + [el("Wallet", 134, 780, 134, 60)]

    def tap(self, udid, names, els=None, scroll=True, wait=0.0):
        if "cartImage" in names and self.state == "menu":
            self.state = "cart"
            return True, "idb"
        if "orderConfirmedMenu" in names and self.state == "confirmed":
            self.state = "wallet"
            return True, "idb"
        return False, "not on screen"

    def tap_el(self, udid, e, els=None, scroll=True):
        if caf._idbd.name(e) == "CONFIRM ORDER":
            self.state = "confirmed"
        return True, "idb"


def test_c_confirm_order_sends_the_order_and_closes_the_dialog(monkeypatch, clock):
    c = Cart()
    install(monkeypatch, c)
    fr = FlowRunner.__new__(FlowRunner)
    fr._cur_udid, fr.devices = "IPHONE", {}
    notes = []
    assert fr._c_confirm_order(notes), notes
    assert c.state == "wallet" and "Order Confirmed" in notes[-1]


def test_c_confirm_order_on_a_pre_order_cart_says_the_table_is_not_assigned(monkeypatch, clock):
    install(monkeypatch, Cart(during_event=False))
    fr = FlowRunner.__new__(FlowRunner)
    fr._cur_udid, fr.devices = "IPHONE", {}
    notes = []
    assert fr._c_confirm_order(notes) is False
    assert "table is not assigned" in notes[-1]


def test_w_qty_set_form_is_safe_to_repeat_in_a_resumed_segment(monkeypatch, clock):
    ok, notes, c = qty(monkeypatch, clock, "Guest 5/Pizza Salamees =6")
    assert ok and c.n == 6
    c2 = Cards(6)
    install(monkeypatch, c2)
    fr = FlowRunner.__new__(FlowRunner)
    fr._cur_udid, fr.devices = "IPAD", {}
    notes = []
    assert fr._w_qty("Guest 5/Pizza Salamees =6", notes) and c2.taps == []
    assert "already 6" in notes[-1]


def test_w_qty_remove_of_a_gone_line_passes_only_in_a_resumed_run(monkeypatch, clock):
    ok, notes, _c = qty(monkeypatch, clock, "Guest 5/Pizza Salamees remove", n=0)
    assert not ok, "a fresh run must not call a missing line removed"
    install(monkeypatch, Cards(0))
    fr = FlowRunner.__new__(FlowRunner)
    fr._cur_udid, fr.devices, fr._resume = "IPAD", {}, {"start_at": 3}
    notes = []
    assert fr._w_qty("Guest 5/Pizza Salamees remove", notes)
    assert "removed by the run this one resumes" in notes[-1]


# ── resume-safe forms (a resumed segment re-runs its steps) ───────────────────

def test_keep_form_parses():
    m = FlowRunner._W_ON_RE.match("Pizza Salamees@Guest 5=5")
    assert m and m.group("who") == "Guest 5" and m.group("keep") == "5" and not m.group("n")


def _runner_with_cards(monkeypatch, cards, resumed=True):
    fr = FlowRunner.__new__(FlowRunner)
    fr._cur_udid, fr.devices = "IPAD", {}
    if resumed:
        fr._resume = {"start_at": 5}
    monkeypatch.setattr(FlowRunner, "_owner_lines",
                        lambda self, udid, who, raw=False: cards.get(who, []))
    monkeypatch.setattr(caf._idbd, "describe_all",
                        lambda udid: pytest.fail("must decide from the cards alone"))
    return fr


def test_void_keep_form_does_nothing_when_the_line_is_already_there(monkeypatch):
    fr = _runner_with_cards(monkeypatch, {"Guest 5": ["Pizza Salamees x 5/2"]}, resumed=False)
    notes = []
    assert fr._w_void("Pizza Salamees@Guest 5=5", notes)
    assert "already at 5" in notes[-1]


def test_a_resumed_move_already_done_passes_from_the_cards(monkeypatch):
    fr = _runner_with_cards(monkeypatch, {"Guest 2": [], "Noolu Naga": ["Pizza Margheritas x 1"]})
    notes = []
    assert fr._w_grid("Pizza Margheritas@Guest 2 > Noolu Naga", "assign", notes)
    assert "already Noolu Naga's" in notes[-1]


def test_a_resumed_grid_split_already_done_passes_from_the_cards(monkeypatch):
    fr = _runner_with_cards(monkeypatch, {"Noolu Naga": ["Coca-Cola® Light 1.0l x 4/2"]})
    notes = []
    assert fr._w_grid("Coca-Cola® Light 1.0l@Noolu Naga > Guest 3", "split", notes)
    assert "already shared" in notes[-1]


def test_a_fresh_run_never_takes_a_missing_line_as_moved(monkeypatch):
    fr = _runner_with_cards(monkeypatch, {"Guest 2": [], "Noolu Naga": ["Pizza Margheritas x 1"]},
                            resumed=False)
    notes = []
    assert fr._w_grid("Pizza Margheritas@Guest 2 > Noolu Naga", "assign", notes) is False
    assert "lists no Pizza Margheritas" in notes[-1]


@pytest.mark.parametrize("now,ok", [("10:30", True), ("14:14", True), ("14:15", False), ("17:10", False)])
def test_before_guard_stops_a_run_too_late_for_the_morning_pre_order_menu(monkeypatch, now, ok):
    import datetime as real
    h, m = map(int, now.split(":"))

    class Clock(real.datetime):
        @classmethod
        def now(cls, tz=None):
            return real.datetime(2026, 10, 29, h, m)

    monkeypatch.setattr(caf, "datetime", Clock)
    fr = FlowRunner.__new__(FlowRunner)
    notes = []
    assert FlowRunner._handle_special(fr, None, "@before:14:15", notes) is ok
    if not ok:
        assert "start this flow before 14:15" in notes[-1] and "15:00" in notes[-1]


class CoveredCards(Cards):
    """Guest 5's card behind a leftover ADD sheet: the cards show once it is closed."""

    def __init__(self, n=5):
        super().__init__(n)
        self.sheet = True

    def describe_all(self, udid):
        if self.sheet:
            return [el("Vya Business", 0, 0, 1210, 834, "Application"),
                    el("addNewItemClose", 1150, 40, 40, 40), el("addNewItemInput", 834, 100, 336, 40)]
        return super().describe_all(udid)

    def tap(self, udid, ids, els=None):
        self.taps.extend(ids)
        if "addNewItemClose" in ids:
            self.sheet = False
        return True


def test_a_card_behind_a_leftover_add_sheet_is_read_after_closing_it(monkeypatch, clock):
    c = CoveredCards()
    install(monkeypatch, c)
    fr = FlowRunner.__new__(FlowRunner)
    fr._cur_udid, fr.devices = "IPAD", {}
    assert fr._owner_lines("IPAD", "Guest 5", raw=True) == ["Pizza Salamees x 5"]
    assert c.taps[0] == "addNewItemClose"


class Invoice:
    """An invoice two screens long: the Pre-Order block's Total first, the grand Total
    after Tip on the next screen, then the VAT table (InvoiceModal's order)."""
    PAGES = [[("RESERVATION", 20, 60, 120, 20), ("Pre-Order", 20, 100, 80, 20),
              ("Total", 20, 400, 40, 20), ("168.27 €", 300, 400, 70, 20)],
             [("Tip", 20, 100, 30, 20), ("2.00 €", 300, 100, 50, 20),
              ("Total", 20, 140, 40, 20), ("76.95 €", 300, 140, 60, 20),
              ("VAT 3% INCL. :", 20, 300, 120, 20), ("1.20€", 300, 300, 50, 20),
              ("VAT 14% INCL. :", 20, 330, 120, 20), ("VAT 17% INCL. :", 20, 360, 120, 20)]]

    def __init__(self):
        self.page = 0

    def read_text(self, udid, region=None, els=None, accurate=False):
        return self.PAGES[self.page]

    def swipe(self, udid, *a):
        self.page = min(self.page + 1, len(self.PAGES) - 1)


def test_the_invoice_total_is_the_grand_total_not_the_pre_order_blocks(monkeypatch, clock):
    from automation.scenarios import screen_text
    inv = Invoice()
    install(monkeypatch, inv)
    monkeypatch.setattr(screen_text, "read_text", inv.read_text)
    fr = FlowRunner.__new__(FlowRunner)
    seen = fr._read_invoice("PHONE", (0, 60, 402, 834))
    assert seen["Total"] == "Total 76.95 €" and seen["VAT 3"].startswith("VAT 3%")


def test_an_empty_idb_dump_is_not_taken_for_the_screen(monkeypatch, clock):
    """idb times out on an overloaded rig and describe_all gives []: "the sheet is
    gone" must not pass on it, and the last real screen is what comes back."""
    dumps = iter([[], [], [el("Vya Business", 0, 0, 1210, 834, "Application"),
                           el("addNewItemClose")]] + [[]] * 50)
    monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: next(dumps))
    fr = FlowRunner.__new__(FlowRunner)
    els, gone = fr._wait_els("IPAD", lambda es: not fr._has(es, "addNewItemClose"), 10.0)
    assert not gone and fr._has(els, "addNewItemClose")
