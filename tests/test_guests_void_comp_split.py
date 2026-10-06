"""Handlers of 'Guests -> void / split / comp -> pay for all in B-App'.

The fakes model what the app source says each screen does, and what was MEASURED
on the iPad (booking 4947, 2026-10-01):
  * consumer Reservation.js / Components/Contacts: the adult '+' opens My Contacts,
    each 'guestAdd' tap adds a 'Guest N' chip, Invite sets Persons to 1 + guests;
  * business OrderSummary.js rows are RNGH Swipeables whose VOID / COMP / SPLIT sit
    ~10000pt off-screen until the row is swiped open (measured x=-9438);
  * the VOID / COMP / SPLIT dialogs are magnus Overlays: accessibility sees ONE
    screen-sized element labelled with every child id ('Specify VOID reason
    entryError customerChangedMind ... assignProductsBtn'), so their controls are
    found by the text they show (OCR) and tapped by position;
  * no VOID / COMP reason is preselected, and reasons / discount chips toggle;
  * profile cards read 'R RoopaDaccordionCard \\uf10c';
  * Pay For is disabled until the profile's E-Payment has been pressed.
"""
import inspect
import re
import types

import pytest

from automation.scenarios import cross_app_flows as caf
from automation.scenarios import screen_text
from automation.scenarios.cross_app_flows import FLOWS, FlowRunner

W, H = 1210, 834


def el(name, x, y, w=80, h=20, kind="StaticText", enabled=True):
    return {"AXIdentifier": "", "AXLabel": name, "type": kind, "enabled": enabled,
            "frame": {"x": x, "y": y, "width": w, "height": h}}


def app():
    return el("Vya", 0, 0, W, H, kind="Application")


class Fake:
    """Shared tap/swipe/OCR plumbing: subclasses build the screen and react to taps."""

    def __init__(self):
        self.taps, self.swipes = [], []

    def texts(self):
        """[(text, x, y, w, h, id_it_presses)] drawn on screen but hidden from idb."""
        return []

    def tap(self, udid, names, els=None, scroll=True):
        here = self.describe_all(udid)
        for n in names:
            hits = [e for e in here if caf._idbd.name(e) == n]
            if len(hits) == 1:
                return self.tap_el(udid, hits[0], here, scroll)
            if len(hits) > 1:
                return False, "ambiguous"
        return False, "not on screen"

    def tap_el(self, udid, e, els=None, scroll=True):
        x, y, w, h = caf._idbd.frame(e)
        # A collapsed Overlay is the one screen-sized element (measured: the VOID
        # dialog read as one 1210x834 element); a card's 3-word label is not one.
        if x < 0 or x > W or (w >= W and h >= H):
            return False, "not reachable"
        self.taps.append(caf._idbd.name(e))
        self.press(caf._idbd.name(e), e)
        return True, "idb"

    def tap_point(self, udid, x, y):
        for t, tx, ty, tw, th, ident in self.texts():
            if tx <= x <= tx + tw and ty <= y <= ty + th:
                if ident:
                    self.taps.append(ident)
                    self.press(ident, None)
                return True, "idb"
        return True, "idb"                       # a tap on nothing does nothing

    def read_text(self, udid, region=None, els=None, accurate=False):
        x0, y0, x1, y1 = region or (0, 0, W, H)
        return [(t, x, y, w, h) for t, x, y, w, h, _ in self.texts()
                if x0 <= x + w / 2 <= x1 and y0 <= y + h / 2 <= y1]

    def swipe(self, udid, x1, y1, x2, y2, duration=0.25):
        self.swipes.append((x1, y1, x2, y2))


@pytest.fixture
def runner(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))
    monkeypatch.setattr(FlowRunner, "_fresh_rotation", staticmethod(lambda udid: None))

    def make(fake):
        for fn in ("describe_all", "tap", "tap_el", "swipe", "tap_point"):
            monkeypatch.setattr(caf._idbd, fn, getattr(fake, fn))
        monkeypatch.setattr(screen_text, "read_text", fake.read_text)
        r = FlowRunner.__new__(FlowRunner)
        r._cur_udid = "IPAD"
        r.devices = {"consumer": "IPHONE"}
        return r
    return make


# ── consumer: invite guests ─────────────────────────────────────────────────
class Reservation(Fake):
    def __init__(self, contacts=True):
        super().__init__()
        self.adult, self.sheet, self.chips, self.contacts = 1, False, [], contacts

    def describe_all(self, udid):
        els = [app()]
        if self.sheet:
            els.append(el("My Contacts", 150, 60))
            if self.contacts:
                els.append(el("guestAdd", 20, 200, 360, 50, "Button"))
                els += [el(f"{c}cancel", 40 + 60 * i, 140, 20, 20, "Button")
                        for i, c in enumerate(self.chips)]
            else:
                els.append(el("No Contacts", 150, 300))
            els.append(el("inviteUsers", 20, 780, 360, 50, "Button"))
            return els
        # adult stepper on the left, child on the right (Reservation.js:3586, :3608)
        els += [el("counterMinus", 70, 500, 40, 40, "Button"),
                el(str(self.adult), 135, 510, 12, 20),
                el("counterPlus", 170, 500, 40, 40, "Button"),
                el("counterMinus", 250, 500, 40, 40, "Button"),
                el("0", 315, 510, 12, 20),
                el("counterPlus", 350, 500, 40, 40, "Button")]
        return els

    def press(self, name, e):
        if name == "counterPlus" and caf._idbd.frame(e)[0] < 200:
            self.sheet = True
        elif name == "guestAdd":
            self.chips.append(f"Guest {len(self.chips) + 1}")
        elif name == "inviteUsers":
            self.sheet, self.adult = False, 1 + len(self.chips)


def test_invites_three_guests_from_the_adult_plus(runner):
    s = Reservation()
    notes = []
    assert runner(s)._invite_guests(3, notes)
    assert s.taps == ["counterPlus", "guestAdd", "guestAdd", "guestAdd", "inviteUsers"]
    assert s.adult == 4 and "Persons 1 → 4" in notes[-1]


def test_no_contacts_means_no_guest_row_and_says_why(runner):
    notes = []
    assert not runner(Reservation(contacts=False))._invite_guests(3, notes)
    assert "No Contacts" in notes[-1] and "simctl privacy" in notes[-1]


# ── business: the order summary ─────────────────────────────────────────────
PROFILES = ["RoopaD", "Guest1", "Guest2", "Guest3"]
PRETTY = {"RoopaD": "Roopa D", "Guest1": "Guest 1", "Guest2": "Guest 2", "Guest3": "Guest 3"}


class Order(Fake):
    """Order Summary + its dialogs. A row: {dish, owner, served, comp, split}."""

    ROW0, ROW_H = 260, 54

    def __init__(self, dishes=("PennePollo", "PizzaSalamees"), swipe_opens=True,
                 served=False, with_options=()):
        super().__init__()
        self.rows = [dict(dish=d, owner="RoopaD", served=served, comp=None, split=False)
                     for d in dishes]
        self.void, self.open, self.modal = [], None, None
        self.reason = self.pct = None
        self.picked, self.toast = set(), ""
        self.swipe_opens, self.sheet, self.staged = swipe_opens, False, []
        self.options, self.with_options = None, set(with_options)
        self.notified, self.payer, self.pad, self.payfor_done = False, None, False, False

    def row_y(self, i):
        return self.ROW0 + i * self.ROW_H

    # what the Overlays SHOW (centred boxes per the source's w x h) --------------
    def texts(self):
        m = self.modal
        if m == "void":
            return [("Specify VOID reason", 500, 100, 209, 20, None),
                    ("Entry Error", 434, 201, 76, 14, "entryError"),
                    ("O Customer Changed Mind", 402, 253, 210, 15, "customerChangedMind"),
                    ("Apply", 580, 713, 48, 18, "assignProductsBtn")]
        if m == "comp":
            return [("Specify COMP reason", 500, 110, 209, 20, None),
                    ("Birthday", 434, 200, 70, 14, "birthday"),
                    ("O Manager Discretion", 402, 250, 160, 15, "managerDiscretion"),
                    ("Apply Discount", 400, 520, 120, 16, None),
                    ("5 %", 410, 560, 30, 16, "5Btn"), ("10 %", 470, 560, 36, 16, "10Btn"),
                    ("15 %", 540, 560, 36, 16, "15Btn"), ("25 %", 610, 560, 36, 16, "25Btn"),
                    ("Apply", 580, 700, 48, 18, "assignProductsBtn")]
        if m == "split":
            out = [("Assign to or split among…", 420, 270, 360, 22, None)]
            for i, p in enumerate(self.split_people()):
                out.append((PRETTY[p], 360 + 120 * i, 400, 60, 16, f"{p}select"))
            out.append(("Apply", 580, 540, 48, 18, "assignProductsBtn"))
            return out
        if self.toast:
            return [(self.toast, 400, 760, 400, 30, None)]
        return []

    def split_people(self):
        owner = self.rows[self.open]["owner"]
        return [p for p in PROFILES if p != owner]

    def overlay(self):
        """The ONE element accessibility reports for an Overlay."""
        ids = {"void": "Specify VOID reason entryError customerChangedMind itemUnavailable "
                       "duplicateOrder allergyDietaryConcern managerOverride "
                       "voidOtherReasonInput assignProductsBtn",
               "comp": "Specify COMP reason birthday managerDiscretion serviceRecovery "
                       "compOtherReasonInput 5Btn 10Btn 15Btn 25Btn assignProductsBtn"}
        if self.modal in ids:
            return el(ids[self.modal], 0, 0, W, H, "GenericElement")
        if self.modal == "split":
            label = " ".join(["Assign to or split among…"]
                             + [f"{p}close" if p in self.picked else f"{p}select"
                                for p in self.split_people()]
                             + [f"{p}select" for p in self.split_people() if p in self.picked]
                             + ["selectAll", "addNewGuest", "assignProductsBtn"])
            return el(label, 0, 0, W, H, "GenericElement")
        return None

    def describe_all(self, udid):
        ov = self.overlay()
        if ov is not None:
            return [app(), ov]                # an Overlay hides everything behind it
        els = [app()]
        if self.toast:
            els.append(el(self.toast, 400, 760, 400, 30))
        if self.sheet:
            els += [el("addNewItemClose", 700, 60, 40, 40, "Button"),
                    el("PASTA Item", 190, 200), el("biriyaniItem", 300, 200),
                    el("PennePolloItem", 97, 320, 600, 50, "Button"),
                    el("PizzaSalameesItem", 97, 380, 600, 50, "Button"),
                    el("PizzaRucolaeParmigianoItem", 97, 440, 600, 50, "Button"),
                    # accessibility reports enabled=True even while it is disabled
                    el("assignToBtn", 300, 760, 400, 50, "Button")]
            if self.options:
                els.append(el("applyOptionBtn", 900, 700, 200, 50, "Button"))
            for i, d in enumerate(self.staged):                        # Summary rows
                els.append(el(f"{d}card", 900, 260 + 40 * i, 200, 20))
        else:
            els.append(el("addItemsBtn", 300, 760, 120, 50, "Button"))
            if self.rows:                       # the header selector shows only with items
                els.append(el("unSelectAll", 682, 108, 101, 18))
            for i, row in enumerate(self.rows):
                y, shift = self.row_y(i), (-240 if self.open == i else 0)
                els.append(el(f"{row['dish']}card", 123 + shift, y, 412, 16, "GenericElement"))
                price = 17.51 * (1 - (row["comp"] or 0) / 100) / (2 if row["split"] else 1)
                els.append(el(f"{price:.2f}€", 704 + shift, y, 51, 18))
                if row["comp"]:
                    els += [el("COMP", 560 + shift, y, 40, 20),
                            el(f"{row['comp']}%", 605 + shift, y, 30, 20)]
                ax = 546 if self.open == i else -9438
                for j, a in enumerate(("VOID", "COMP", "SPLIT")):
                    els.append(el(a, ax + 76 * j, y - 10, 72, 37, "GenericElement"))
            if self.void:
                els.append(el("Void Products", 97, self.row_y(len(self.rows)) + 10, 692, 48))
            els += [el(f"{p[0]} {p}accordionCard ", 834, 146 + 124 * i, 336, 49,
                       "GenericElement") for i, p in enumerate(PROFILES)]
            if self.notified:
                els += [el("epaymentBtn", 850, 300, 60, 60, "Button"),
                        el("payForBtn", 1000, 300, 60, 60, "Button")]
            if self.pad:
                els += [el("numberPadClose", 900, 250, 100, 20, "Button"),
                        el("userInputBtn", 900, 700, 200, 50, "Button")]
            els.append(el("notifyPaymentBtn", 450, 760, 200, 50, "Button"))
        m = self.modal
        if m in ("assign", "payfor"):             # react-native-modal: children visible
            for i, p in enumerate(PROFILES):
                els.append(el(f"{p}select", 350 + 140 * i, 400, 80, 80, "Button"))
                if p in self.picked:
                    els.append(el(f"{p}close", 420 + 140 * i, 395, 20, 20, "Button"))
            els.append(el("selectAll", 350, 500))
            if m == "assign":
                els += [el("assignProductsBtn", 350, 560, 300, 50, "Button"),
                        el("assignProductsBtn", 700, 560, 300, 50, "Button")]
            else:
                els.append(el("applyPayment", 500, 560, 300, 50, "Button"))
        elif m == "notify":
            els += [el("No", 450, 500, 100, 40, "Button"), el("Yes", 650, 500, 100, 40, "Button")]
        return els

    def swipe(self, udid, x1, y1, x2, y2, duration=0.25):
        super().swipe(udid, x1, y1, x2, y2, duration)
        if self.swipe_opens and x1 - x2 > 150:
            for i in range(len(self.rows)):
                if self.row_y(i) - 10 <= y1 <= self.row_y(i) + 27:
                    self.open = i

    def press(self, name, e):
        self.toast = ""
        row = self.rows[self.open] if self.open is not None else None
        if name == "VOID" and row:
            self.modal, self.reason = "void", None
        elif name == "COMP" and row:
            if row["served"]:
                self.modal, self.reason, self.pct = "comp", None, None
            else:
                self.toast = "Can't apply comp before serve..."
        elif name == "SPLIT" and row:
            self.modal, self.picked = "split", set()
        elif name in ("entryError", "birthday", "customerChangedMind", "managerDiscretion"):
            self.reason = None if self.reason == name else name      # radios toggle
        elif re.fullmatch(r"\d+Btn", name):
            pct = int(name[:-3])
            self.pct = None if self.pct == pct else pct              # chips toggle
        elif name.endswith("select") and name != "selectAll":
            self.picked.add(name[:-6])
        elif name == "addItemsBtn":
            self.sheet = True
        elif name == "addNewItemClose":
            self.sheet, self.staged = False, []        # closing drops what was staged
        elif name.endswith("Item") and self.sheet:
            if name[:-4] in self.with_options:
                self.options = name[:-4]
            else:
                self.staged.append(name[:-4])
        elif name == "applyOptionBtn" and self.options:
            self.staged.append(self.options)
            self.options = None
        elif name == "assignToBtn" and self.staged:
            self.modal, self.picked = "assign", set()
        elif name == "assignProductsBtn":
            self.apply(caf._idbd.frame(e)[0] if e else 580)
        elif name == "notifyPaymentBtn":
            self.modal = "notify"
        elif name == "Yes":
            self.modal, self.notified = None, True
        elif name == "epaymentBtn":
            self.payer, self.pad = "RoopaD", True
        elif name == "numberPadClose":
            self.pad = False
        elif name == "payForBtn" and self.payer == "RoopaD":   # disabled until then
            self.modal, self.picked = "payfor", {"RoopaD"}
        elif name == "applyPayment":
            self.modal, self.payfor_done = None, True

    def apply(self, x):
        m, row = self.modal, (self.rows[self.open] if self.open is not None else None)
        if m == "void":
            if not self.reason:
                self.toast = "Please select required options"
                return
            self.void.append(self.rows.pop(self.open))
        elif m == "comp":
            if not (self.reason and self.pct):
                self.toast = "Please select all"
                return
            row["comp"] = self.pct
        elif m == "split":
            if not self.picked:
                return
            row["split"] = True
            self.rows.insert(self.open + 1, dict(row, owner=sorted(self.picked)[0]))
        elif m == "assign":
            if x > 600:          # the RIGHT one is Split
                return
            self.rows += [dict(dish=d, owner=p, served=False, comp=None, split=False)
                          for d in self.staged for p in sorted(self.picked)]
            self.sheet, self.staged = False, []
        self.modal, self.open = None, None


def test_void_swipes_the_row_and_taps_entry_error_then_apply_by_their_text(runner):
    s = Order()
    notes = []
    assert runner(s)._void_item(notes)
    assert s.taps == ["VOID", "entryError", "assignProductsBtn"]
    assert [v["dish"] for v in s.void] == ["PennePollo"]
    # the drag starts on the price, never on the radio (a tap there selects the row)
    x1, y1, x2, _ = s.swipes[0]
    assert x1 > 600 and x2 < x1 - 300
    assert "Void Products" in notes[-1]


def test_a_swipe_that_does_not_open_the_row_fails_without_tapping(runner):
    s = Order(swipe_opens=False)
    notes = []
    assert not runner(s)._void_item(notes)
    assert s.taps == [] and len(s.swipes) == 3
    assert "did not show" in notes[-1]


def test_closed_rows_buttons_are_off_screen_and_ignored(runner):
    s = Order()
    r = runner(s)
    els = s.describe_all("IPAD")
    assert r._row_actions(els, r._order_rows(els)[0]) == {}
    s.open = 1
    els = s.describe_all("IPAD")
    rows = r._order_rows(els)
    assert r._row_actions(els, rows[0]) == {}
    assert set(r._row_actions(els, rows[1])) == {"VOID", "COMP", "SPLIT"}


def test_add_for_all_selects_every_profile_and_presses_assign_not_split(runner):
    s = Order()
    notes = []
    assert runner(s)._add_item_for_all(None, notes)
    assert s.taps[:2] == ["addItemsBtn", "PizzaRucolaeParmigianoItem"]   # not on the order yet
    assert {"RoopaDselect", "Guest1select", "Guest2select", "Guest3select"} <= set(s.taps)
    added = [x for x in s.rows if x["dish"] == "PizzaRucolaeParmigiano"]
    assert sorted(x["owner"] for x in added) == sorted(PROFILES)
    assert "4 row(s)" in notes[-1]


def test_a_dish_with_options_is_applied_before_assign(runner):
    s = Order(with_options=("PizzaRucolaeParmigiano",))
    notes = []
    assert runner(s)._add_item_for_all(None, notes)
    assert s.taps[:4] == ["addItemsBtn", "PizzaRucolaeParmigianoItem", "applyOptionBtn",
                          "assignToBtn"]


def test_fast_add_items_stages_two_dishes_and_assigns_them_to_the_first_profile(runner):
    s = Order(dishes=(), with_options=("PennePollo",))
    notes = []
    assert runner(s)._add_items_fast(notes) == "ok"
    assert s.taps == ["addItemsBtn", "PennePolloItem", "applyOptionBtn",
                      "PizzaSalameesItem", "assignToBtn", "RoopaDselect", "assignProductsBtn"]
    assert sorted(x["dish"] for x in s.rows) == ["PennePollo", "PizzaSalamees"]
    assert "for Roopa D (idb)" in notes[-1]


def test_fast_add_items_leaves_a_covered_add_button_to_the_appium_path(runner, monkeypatch):
    """A LogBox toast over addItemsBtn: the verified tap refuses it (measured case)."""
    s = Order(dishes=())
    r = runner(s)
    real = s.tap_el

    def covered(udid, e, els=None, scroll=True):
        if caf._idbd.name(e) == "addItemsBtn":
            return False, "not reachable"
        return real(udid, e, els, scroll)
    monkeypatch.setattr(s, "tap_el", covered)            # the fake's own tap() uses it
    monkeypatch.setattr(caf._idbd, "tap_el", covered)
    notes = []
    assert r._add_items_fast(notes) == "untouched" and not s.sheet and s.taps == []


def test_fast_add_items_closes_the_sheet_when_it_stops_before_assign(runner):
    s = Order(dishes=())
    s.with_options = {"PennePollo"}
    r = runner(s)
    real = s.press
    s.press = lambda name, e: None if name == "applyOptionBtn" else real(name, e)
    notes = []
    assert r._add_items_fast(notes) == "retry"
    assert not s.sheet and "using the Appium path" in notes[-1]


def test_split_picks_one_other_profile_by_its_name_and_confirms_the_pick(runner):
    s = Order(dishes=("PizzaSalamees", "PennePollo", "PennePollo", "PennePollo",
                      "PennePollo"))
    r = runner(s)
    r._assigned_all = "PennePollocard"
    notes = []
    assert r._split_item(notes)
    assert s.taps == ["SPLIT", "Guest1select", "assignProductsBtn"]   # owner is hidden
    assert sum(1 for x in s.rows if x["dish"] == "PennePollo") == 5
    assert "with Guest 1: 4 → 5 rows" in notes[-1]


def test_comp_before_serve_is_reported_with_the_apps_reason(runner):
    s = Order(served=False)
    notes = []
    assert not runner(s)._comp_item(notes)
    assert "before serve" in notes[-1]


def test_comp_birthday_ten_percent_taps_each_toggle_once(runner):
    s = Order(served=True)
    notes = []
    assert runner(s)._comp_item(notes)
    assert s.taps == ["COMP", "birthday", "10Btn", "assignProductsBtn"]
    assert s.rows[0]["comp"] == 10
    assert "17.51€ → 15.76€" in notes[-1]


def test_ocr_never_takes_25_percent_for_5_percent(runner):
    s = Order(served=True)
    r = runner(s)
    s.modal = "comp"
    ok, how = r._ocr_tap("IPAD", r._box(s.describe_all("IPAD"), "comp"), "5 %")
    assert ok and s.taps == ["5Btn"]


@pytest.mark.parametrize("seen, text, hit", [
    ("10 %", "10 %", True), ("10%", "10 %", True),
    ("40 0/0", "40 %", True),              # MEASURED: Vision reads '%' as '0/0'
    ("25%", "5 %", False), ("15 %", "5 %", False), ("1C", "100 %", False),
    ("O Customer Changed Mind", "Customer Changed Mind", True),   # radio circle as 'O'
    ("Apply Discount", "Apply", False), ("Apply", "Apply", True),
])
def test_ocr_text_matching(seen, text, hit):
    assert FlowRunner._text_matches(seen, text) is hit


def test_ocr_falls_back_to_accurate_when_fast_misses_the_chip(runner, monkeypatch):
    """MEASURED on booking 4949: fast OCR skipped '5 %', '10 %', '15 %'."""
    s = Order(served=True)
    r = runner(s)
    s.modal = "comp"
    full = s.read_text

    def read(udid, region=None, els=None, accurate=False):
        out = full(udid, region)
        return out if accurate else [t for t in out if t[0] not in ("5 %", "10 %", "15 %")]
    monkeypatch.setattr(screen_text, "read_text", read)
    ok, how = r._ocr_tap("IPAD", r._box(s.describe_all("IPAD"), "comp"), "10 %")
    assert ok and s.taps == ["10Btn"] and "accurate" in how


class Settling(Fake):
    """Order screen after Confirm Payment: Close Table shows up a few seconds later."""

    def __init__(self, appears_after=3):
        super().__init__()
        self.reads, self.appears_after, self.closed = 0, appears_after, False

    def describe_all(self, udid):
        self.reads += 1
        if self.closed:
            return [app(), el("My Bookings", 123, 26, 640, 30)]
        els = [app(), el("PizzaSalameescard", 123, 213, 412, 16, "GenericElement")]
        if self.reads > self.appears_after:
            els.append(el("closeTableBtn", 129, 702, 444, 64, "GenericElement"))
        return els

    def press(self, name, e):
        if name == "closeTableBtn":
            self.closed = True


def test_close_table_waits_for_the_button_then_confirms_the_order_closed(runner):
    s = Settling(appears_after=4)
    notes = []
    assert runner(s)._close_table(notes)
    assert s.taps == ["closeTableBtn"] and "the order closed" in notes[-1]


def test_close_table_pays_by_epay_again_when_confirm_did_not_register(runner, monkeypatch):
    """MEASURED on 4954: Due 0.00 €, Confirm Payment still up, no Close Table.
    The user's rule: do the E-Payment again (not Confirm alone), then close."""
    s = Settling(appears_after=10 ** 6)
    real = s.describe_all

    def describe_all(udid):
        els = real(udid)
        return els if s.appears_after < 10 ** 6 else els + [
            el("Due 0.00 €", 850, 560, 120, 20),
            el("paymentConfirmBtn", 900, 600, 200, 40, "GenericElement")]
    s.describe_all = describe_all
    r = runner(s)
    monkeypatch.setattr(caf._idbd, "describe_all", describe_all)
    paid = []

    def pay(method, notes):
        paid.append(method)
        s.appears_after = s.reads           # the second payment goes through
        return True
    monkeypatch.setattr(r, "_pay_business", pay)
    notes = []
    assert r._close_table(notes)
    assert paid == ["epay"] and s.taps == ["closeTableBtn"]
    assert "paymentConfirmBtn" not in s.taps
    assert any("paying by E-Payment again" in n for n in notes)


def test_close_table_reopens_a_folded_card_to_check_and_repays(runner, monkeypatch):
    """Close Table missing and Confirm hidden inside a FOLDED card: open the card,
    see the payment is not done, pay again, then close."""
    s = Settling(appears_after=10 ** 6)
    state = {"open": False}
    real = s.describe_all

    def describe_all(udid):
        els = real(udid) + [el("R RoopaDaccordionCard ", 834, 146, 336, 49, "GenericElement")]
        if state["open"] and s.appears_after == 10 ** 6:
            els += [el("epaymentBtn", 850, 300, 60, 60, "Button"),
                    el("Outstanding 90.56 €", 850, 560, 160, 20)]
        return els
    s.describe_all = describe_all
    real_press = s.press

    def press(name, e):
        if "accordionCard" in name:
            state["open"] = not state["open"]
        real_press(name, e)
    s.press = press
    r = runner(s)
    monkeypatch.setattr(caf._idbd, "describe_all", describe_all)
    paid = []

    def pay(method, notes):
        paid.append(method)
        s.appears_after = s.reads
        return True
    monkeypatch.setattr(r, "_pay_business", pay)
    notes = []
    assert r._close_table(notes)
    assert paid == ["epay"] and s.taps[-1] == "closeTableBtn"
    assert any("payment is not done" in n for n in notes)


def test_close_table_does_not_pay_twice_when_nothing_is_left_to_pay(runner, monkeypatch):
    """The payment went through but Close Table is slow: wait, never re-pay."""
    s = Settling(appears_after=30)          # later than the first 15s window
    r = runner(s)
    monkeypatch.setattr(r, "_pay_business", lambda m, n: pytest.fail("paid twice"))
    notes = []
    assert r._close_table(notes)
    assert s.taps == ["closeTableBtn"]
    assert any("nothing is left to pay" in n for n in notes)


def test_payment_buttons_alone_never_trigger_a_second_payment(runner, monkeypatch):
    """On 4954 the card kept showing E-Payment/Cash/Voucher for seconds after a
    payment that HAD registered. Buttons are not evidence of an unpaid bill."""
    s = Settling(appears_after=30)
    real = s.describe_all
    s.describe_all = lambda udid: real(udid) + [
        el("R RoopaDaccordionCard ", 834, 146, 336, 49, "GenericElement"),
        el("epaymentBtn", 850, 300, 60, 60, "Button"),
        el("cashPaymentBtn", 920, 300, 60, 60, "Button")]
    r = runner(s)
    monkeypatch.setattr(caf._idbd, "describe_all", s.describe_all)
    monkeypatch.setattr(r, "_pay_business", lambda m, n: pytest.fail("paid twice"))
    notes = []
    assert r._close_table(notes)
    assert s.taps[-1] == "closeTableBtn" and "paymentConfirmBtn" not in s.taps


def test_close_table_fails_plainly_when_the_bill_is_not_paid(runner):
    s = Settling(appears_after=10 ** 6)
    notes = []
    assert not runner(s)._close_table(notes)
    assert s.taps == [] and "did not appear" in notes[-1]


def test_notify_payment_confirms_with_yes(runner):
    s = Order(served=True)
    notes = []
    assert runner(s)._notify_payment(notes)
    assert s.taps == ["notifyPaymentBtn", "Yes"] and s.notified


def test_pay_for_falls_back_to_e_payment_when_pay_for_is_dead(runner):
    """The user's order is Pay For first. On a build where Pay For is dead until the
    profile is the payer (the Fake models that), the step falls back: E-Payment,
    close the keypad, Pay For again -- then selects everyone and applies."""
    s = Order(served=True)
    s.notified = True
    r = runner(s)
    notes = []
    assert r._pay_for_all(notes)
    assert s.taps[:4] == ["payForBtn", "epaymentBtn", "numberPadClose", "payForBtn"]
    assert {"Guest1select", "Guest2select", "Guest3select"} <= set(s.taps)
    assert "RoopaDselect" not in s.taps          # the payer is preselected
    assert s.taps[-1] == "applyPayment" and s.payfor_done
    assert r._payer_card == "RoopaDaccordionCard"
    assert "Roopa D pays for Guest 1, Guest 2, Guest 3" in notes[-1]


def test_pay_for_needs_one_tap_when_the_profile_is_already_the_payer(runner):
    s = Order(served=True)
    s.notified, s.payer = True, "RoopaD"
    notes = []
    assert runner(s)._pay_for_all(notes)
    # The user's order: Pay For straight away -- no E-Payment in this step at all.
    assert s.taps[0] == "payForBtn" and "epaymentBtn" not in s.taps
    assert s.taps.count("payForBtn") == 1 and s.taps[-1] == "applyPayment"


def test_pay_for_retries_when_the_first_tap_did_not_land(runner, monkeypatch):
    """Measured: '[FAIL] @pay_for_all — tapped Pay For ... did not open (58.2s)'
    straight after the keypad closed. The first Pay For tap was refused as not
    reachable (the panel was still moving) and nothing retried it."""
    s = Order(served=True)
    s.notified = True
    r = runner(s)
    real, refused = s.tap_el, {"n": 0}

    def first_refused(udid, e, els=None, scroll=True):
        if caf._idbd.name(e) == "payForBtn" and refused["n"] == 0:
            refused["n"] += 1
            return False, "not reachable"
        return real(udid, e, els, scroll)
    monkeypatch.setattr(s, "tap_el", first_refused)
    monkeypatch.setattr(caf._idbd, "tap_el", first_refused)
    notes = []
    assert r._pay_for_all(notes)
    assert s.taps.count("payForBtn") == 1 and s.payfor_done
    assert any("opened on try 2" in n for n in notes)


def test_pay_for_failure_says_whether_the_tap_landed(runner, monkeypatch):
    s = Order(served=True)
    s.notified = True
    r = runner(s)
    real = s.tap_el

    def never(udid, e, els=None, scroll=True):
        if caf._idbd.name(e) == "payForBtn":
            return False, "not reachable"
        return real(udid, e, els, scroll)
    monkeypatch.setattr(s, "tap_el", never)
    monkeypatch.setattr(caf._idbd, "tap_el", never)
    notes = []
    assert not r._pay_for_all(notes)
    assert "3 tries; last: Pay For not tapped: not reachable" in notes[-1]


def test_pay_for_reopens_a_card_the_keypad_folded(runner, monkeypatch):
    """Closing the keypad can fold Roopa's card, taking Pay For with it: re-open the
    card, then tap Pay For -- and never tap the card while Pay For shows (folds it)."""
    s = Order(served=True)
    s.notified = True
    r = runner(s)
    state = {"folded": False}
    real_all, real_press = s.describe_all, s.press

    def describe_all(udid):
        els = real_all(udid)
        return [e for e in els if not (state["folded"] and caf._idbd.name(e) == "payForBtn")]

    def press(name, e):
        if name == "numberPadClose":
            state["folded"] = True
        elif "accordionCard" in name:
            state["folded"] = not state["folded"]
        real_press(name, e)
    monkeypatch.setattr(s, "describe_all", describe_all)
    monkeypatch.setattr(caf._idbd, "describe_all", describe_all)
    s.press = press
    real_tap_el = s.tap_el

    def tap_el(udid, e, els=None, scroll=True):
        # The shared fake refuses 3-word labels as a collapsed Overlay; the card's
        # label ('R RoopaDaccordionCard <glyph>') is a real, tappable card.
        if "accordionCard" in caf._idbd.name(e):
            s.taps.append(caf._idbd.name(e))
            s.press(caf._idbd.name(e), e)
            return True, "idb"
        return real_tap_el(udid, e, els, scroll)
    monkeypatch.setattr(caf._idbd, "tap_el", tap_el)
    notes = []
    ok = r._pay_for_all(notes)
    assert ok, s.taps
    i = s.taps.index("numberPadClose")
    assert "accordionCard" in s.taps[i + 1] and s.taps[i + 2] == "payForBtn"
    assert s.payfor_done


# ── every @token a flow uses has a handler ──────────────────────────────────
def test_every_flow_token_is_dispatched():
    src = inspect.getsource(FlowRunner._handle_special)
    exact = set(re.findall(r'"(@[\w:]+)"', src))
    prefixes = {t for t in exact if t.endswith(":")}
    for flow in FLOWS.values():
        for seg in flow["segments"]:
            for step in seg["steps"]:
                step = FlowRunner._PLAIN_STEP_TOKENS.get(step, step)
                if not step.startswith("@"):
                    continue
                assert step in exact or any(step.startswith(p) for p in prefixes), \
                    f"{flow['id']}: {step} has no handler in _handle_special"


def test_new_flow_is_listed_for_the_suite():
    f = FLOWS["flow_guests_void_comp_split"]
    assert [s["role"] for s in f["segments"]] == ["consumer", "waiter", "kitchen", "waiter"]
    steps = f["segments"][0]["steps"]
    assert steps.index("@invite_guests:3") < steps.index("@book_appointment")
    assert "@pre_order" not in steps and "@order_later" in steps
    w2 = f["segments"][3]["steps"]
    assert w2.index("click serveItemsBtn") < w2.index("@comp_item") < w2.index("@notify_payment")
