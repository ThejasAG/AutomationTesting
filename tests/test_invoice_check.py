"""Does an open invoice add up: automation/scenarios/invoice_check.py and its steps.

The rows and amounts are the real ones where they exist: the Pre-Order rows, the VAT rows
and the Totals were read from staging on 2026-10-29 (bookings 5568/5575)."""
import types

import pytest

from automation.scenarios import cross_app_flows as caf
from automation.scenarios import invoice_check as ic
from automation.scenarios.cross_app_flows import FLOWS, FlowRunner


def page(*rows, top=100.0, gap=30.0):
    """A screen's words as the reader gives them: a label and an amount per row."""
    words = []
    for i, row in enumerate(rows):
        y = top + i * gap
        for j, text in enumerate(row):
            words.append((text, 20 + j * 200, y, 80, 16))
    return words


def blocks(*rows):
    return ic.parse(ic.stitch([page(*rows, gap=25.0)])[0])


# The participant's phone invoice of 2026-10-29: it paid 208.38 + tip 3.00 by E-Payment.
PHONE_TABLE = [("On Premises",), ("Pizza x 2", "100.00€"), ("• Discount 10%", "-10.00€"),
               ("Tip", "3.00€"), ("Paid for Guest 3",), ("Penne x 1", "+118.38€"),
               ("Total", "211.38€"), ("EC-Card / Bank Card", "211.38€"),
               ("Excl. Tax", "VAT", "ATI"),
               ("VAT 3% INCL. :", "76.60€", "2.30€", "78.90€"),
               ("VAT 14% INCL. :", "70.49€", "9.87€", "80.36€"),
               ("VAT 17% INCL. :", "43.21€", "5.91€", "49.12€")]

# The host's Pre-Order block of 2026-10-29: the dishes of the event's real invoice.
PRE_ORDER = [("Pre-Order",), ("Penne Pollo x 1", "17.51 €"),
             ("hyderabad chicken biriyani x 3", "52.65 €"), ("mushroom biryani x 2", "32.00 €"),
             ("Pizza Margheritas x 2", "45.16 €"), ("Tagliatelle dello Chef x 1", "20.95 €"),
             ("Total", "168.27 €"), ("EC-Card / Bank Card", "168.27 €"),
             ("VAT 3% INCL. :", "57.00€", "1.11€", "58.11€"),
             ("VAT 14% INCL. :", "22.38€", "3.13€", "25.51€"),
             ("VAT 17% INCL. :", "74.27€", "10.38€", "84.65€")]

# The event invoice's on-premises block: VAT rows of 2026-10-29, 5.00 of tips. The 3 % row
# as preprod-2-May18 derives it (see test_the_real_vat_rounding_bug_is_caught).
EVENT_TABLE = [("on-Permises",), ("Pizza x 2", "400.00 €"), ("Tip", "5.00 €"), ("Due", "0.00 €"),
               ("Total", "322.29 €"), ("EC-Card / Bank Card", "288.33 €"), ("Cash", "33.96 €"),
               ("Food Voucher", "0.00 €"),
               ("VAT 3% INCL. :", "108.12€", "3.24€", "111.36€"),
               ("VAT 14% INCL. :", "110.13€", "15.43€", "125.56€"),
               ("VAT 17% INCL. :", "71.17€", "9.20€", "80.37€")]


def test_the_real_pre_order_block_adds_up():
    rep = ic.check(blocks(*PRE_ORDER))
    assert rep.problems == []
    assert any("5 dishes 168.27" in line and "matches" in line for line in rep.lines)
    assert any("VAT rows 58.11 + 25.51 + 84.65" in line for line in rep.lines)


def test_the_real_phone_invoice_adds_up_with_comps_paid_for_and_tip():
    rep = ic.check(blocks(*PHONE_TABLE))
    assert rep.problems == []
    assert any("grand total" in l and "+ tip 3.00 = 211.38" in l for l in rep.lines)


def test_the_grand_total_is_the_on_premises_block_not_the_pre_order_one():
    parsed = blocks(*PRE_ORDER, *EVENT_TABLE)
    assert [b.name for b in parsed] == ["Pre-Order", "On Premises"]
    assert [b.totals[-1] for b in parsed] == [168.27, 322.29]
    assert ic.seen_of(parsed)["Total"] == "Total 322.29 €"


@pytest.mark.parametrize("rows, expected", [
    # The Total itself is wrong: the row the user cares about.
    ([("Pre-Order",), ("Penne x 1", "10.00 €"), ("Total", "12.00 €"),
      ("VAT 3% INCL. :", "9.71€", "0.29€", "10.00€")], "grand total"),
    # A dish row is off although the VAT table and Total agree.
    ([("Pre-Order",), ("Penne x 1", "11.00 €"), ("Total", "10.00 €"),
      ("VAT 3% INCL. :", "9.71€", "0.29€", "10.00€")], "dishes"),
    # Excl. + VAT does not make the ATI.
    ([("Pre-Order",), ("Penne x 1", "10.00 €"), ("Total", "10.00 €"),
      ("VAT 3% INCL. :", "9.00€", "0.29€", "10.00€")], "VAT 3%"),
    # More VAT than the rate allows on that amount.
    ([("Pre-Order",), ("Penne x 1", "10.00 €"), ("Total", "10.00 €"),
      ("VAT 3% INCL. :", "9.00€", "1.00€", "10.00€")], "above the rate"),
    # The comp is printed but the Total did not take it off.
    ([("On Premises",), ("Pizza x 1", "20.00 €"), ("• Discount 50%", "-10.00€"),
      ("Total", "20.00 €"), ("VAT 3% INCL. :", "19.42€", "0.58€", "20.00€")], "dishes"),
    # The money taken is not the Total and nothing explains it.
    ([("Pre-Order",), ("Penne x 1", "10.00 €"), ("Total", "10.00 €"),
      ("EC-Card / Bank Card", "9.00 €"),
      ("VAT 3% INCL. :", "9.71€", "0.29€", "10.00€")], "unexplained"),
])
def test_a_wrong_figure_is_named(rows, expected):
    rep = ic.check(blocks(*rows))
    assert any(expected in p for p in rep.problems), rep.problems


def test_the_real_vat_rounding_bug_is_caught():
    """The event invoice of 2026-10-29, on `main`: 3.26 of VAT on 111.36 at 3 %, where 3.24
    is the most it can be. Summing each share's rounded Excl. compounds the error; the
    source calls it VYP-106 and derives VAT from the ATI on preprod-2-May18."""
    rep = ic.check(blocks(("Pre-Order",), ("Penne x 1", "111.36 €"), ("Total", "111.36 €"),
                          ("VAT 3% INCL. :", "108.10€", "3.26€", "111.36€")))
    assert len(rep.problems) == 1
    assert "VAT 3.26 on 111.36 is above the rate (at most 3.24)" in rep.problems[0]


def test_one_cent_of_rounding_per_row_is_not_a_mismatch():
    rows = [("Pre-Order",), ("A x 1/3", "3.33 €"), ("B x 1/3", "3.33 €"), ("C x 1/3", "3.33 €"),
            ("Total", "10.00 €"), ("VAT 3% INCL. :", "9.71€", "0.29€", "10.00€")]
    assert ic.check(blocks(*rows)).problems == []


def test_a_real_overpayment_is_explained_by_due_or_by_the_run():
    rows = [("On Premises",), ("Penne x 1", "10.00 €"), ("Total", "10.00 €"), ("Cash", "20.00 €"),
            ("VAT 3% INCL. :", "9.71€", "0.29€", "10.00€")]
    assert any("unexplained" in p for p in ic.check(blocks(*rows)).problems)
    with_due = [("Due", "10.00 €") if r == ("Total", "10.00 €") else r for r in rows]
    with_due.insert(3, ("Total", "10.00 €"))
    assert ic.check(blocks(*with_due)).problems == []
    paid = {"overpaid": 10.0, "preorder": [], "table": [], "tips": []}
    assert ic.check(blocks(*rows), paid=paid).problems == []


def test_the_event_invoice_cannot_add_comps_it_does_not_print_and_says_so():
    rep = ic.check(blocks(*EVENT_TABLE), comps_printed=False)
    assert rep.problems == []
    assert any("does not print comps" in line for line in rep.lines)


def test_the_invoice_is_checked_against_what_the_run_really_paid():
    parsed = blocks(*PRE_ORDER, *EVENT_TABLE)
    paid = {"preorder": [168.27], "table": [76.95, 211.38, 33.96], "tips": [2.0, 3.0],
            "overpaid": 0.0}
    rep = ic.check(parsed, comps_printed=False, paid=paid)
    assert rep.problems == []
    assert any("76.95 + 211.38 + 33.96" in line for line in rep.lines)
    assert any("Whole invoice: Pre-Order 168.27 + On Premises 322.29 = 490.56" in line
               for line in rep.lines)
    assert any("Tip against" in p for p in ic.check(
        parsed, comps_printed=False, paid={**paid, "tips": [2.0]}).problems)
    assert any("On Premises against" in p for p in ic.check(
        parsed, comps_printed=False, paid={**paid, "table": [76.95, 211.38]}).problems)


def test_a_payment_that_was_skipped_is_not_compared_rather_than_called_wrong():
    rep = ic.check(blocks(*PRE_ORDER), paid={"incomplete": True, "preorder": [999.0]})
    assert rep.problems == []
    assert any("Not compared" in line for line in rep.lines)


def test_unread_figures_are_not_a_pass():
    assert any("could not read" in p for p in ic.check(blocks(
        ("Pre-Order",), ("Penne x 1", "10.00 €"), ("Total", "10.00 €"),
        ("VAT 3% INCL. :", "9.71€"))).problems)
    assert any("no Total" in p for p in ic.check(blocks(("Pre-Order",),
                                                        ("Penne x 1", "10.00 €"))).problems)
    assert ic.check([]).problems == ["no block of the invoice could be read"]
    assert any("not reached" in p for p in ic.check(blocks(*PRE_ORDER), cut=True).problems)
    assert any("scroll" in p for p in ic.check(blocks(*PRE_ORDER), gaps=1).problems)


def test_a_dish_with_a_decimal_in_its_name_has_no_money_in_it():
    assert ic._money_in("2020 Bonacci Chianti Classico Riserva 0,75lj x 3") == []
    assert ic._money_in("Coca-Cola® Light 1.0l x 4/5") == []
    assert ic._money_in("Riserva 0,75") == []
    assert ic._money_in("52.65 €") == [52.65]
    assert ic._money_in("-69.50€") == [69.50]
    assert ic._money_in("+12,00€") == [12.0]
    assert ic._money_in("Total 211.38€") == [211.38]
    assert ic._money_in("76.95") == [76.95]
    assert ic._money_in("0€") == [0.0]         # the phone's unset Balance


def test_an_unset_balance_on_the_phone_is_not_an_unreadable_row():
    rep = ic.check(blocks(("On Premises",), ("Penne x 1", "10.00€"), ("Total", "10.00€"),
                          ("EC-Card / Bank Card", "10.00€"), ("Balance", "0€"),
                          ("VAT 3% INCL. :", "9.71€", "0.29€", "10.00€")))
    assert rep.problems == []


def test_reader_noise_on_a_vat_row_is_ignored():
    """MEASURED 2026-10-29: the tab in the label read as '2:8a' on the phone."""
    parsed = blocks(("Pre-Order",), ("Penne x 1", "49.12 €"), ("Total", "49.12 €"),
                    ("VAT 17% INCL. 2:8a", "43.21€", "5.91€", "49.12€"))
    assert parsed[0].vat == {17: (43.21, 5.91, 49.12)}


def test_money_after_a_vat_table_starts_the_next_block_when_its_heading_was_misread():
    rows = [r for r in PRE_ORDER] + [r for r in EVENT_TABLE if r != ("on-Permises",)]
    assert [b.name for b in blocks(*rows)] == ["Pre-Order", "On Premises"]


# ── One invoice from its screens ─────────────────────────────────────────────

ROWS = [(f"Dish {n} x 1", f"{n}.00 €") for n in range(1, 21)]


def test_overlapping_screens_give_every_row_once():
    screens = [page(*ROWS[:12]), page(*ROWS[6:18]), page(*ROWS[12:20])]
    rows, gaps = ic.stitch(screens)
    assert gaps == 0
    assert [r.text for r in rows] == [" ".join(r) for r in ROWS]


def test_identical_rows_are_all_kept():
    """Three equal shares of a split: real rows, not screens overlapping."""
    same = [("Pizza x 1/3", "3.33 €")] * 3
    rows_in = [("A x 1", "1.00 €"), ("B x 1", "2.00 €")] + same + [("C x 1", "3.00 €"),
                                                                     ("D x 1", "4.00 €")]
    rows, gaps = ic.stitch([page(*rows_in[:5]), page(*rows_in[2:])])
    assert gaps == 0
    assert [r.money[0] for r in rows] == [1.0, 2.0, 3.33, 3.33, 3.33, 3.0, 4.0]


def test_a_row_cut_by_the_screens_edge_is_taken_from_the_next_screen():
    cut = page(*ROWS[:5]) + [("Dish 6 x", 20, 248.0, 80, 8)]        # half a row, higher up
    rows, gaps = ic.stitch([cut, page(*ROWS[3:9])])
    assert gaps == 0
    assert [r.text for r in rows] == [" ".join(r) for r in ROWS[:9]]


def test_screens_that_share_nothing_are_reported():
    rows, gaps = ic.stitch([page(*ROWS[:3]), page(*ROWS[10:13])])
    assert gaps == 1 and len(rows) == 6


def test_words_read_column_by_column_come_back_as_rows():
    """The reader's real order, MEASURED on the real invoice: all the labels, then all
    the amounts."""
    words = [("Spaghetti Bolognese x 2", 20, 100, 150, 16), ("Pizza x 1", 20, 130, 90, 14),
             ("Customization", 20, 150, 80, 14), ("24.72 €", 300, 101, 50, 14),
             ("11.33 €", 300, 131, 50, 12)]
    assert [(r.text, r.money) for r in ic.rows_of(words)] == [
        ("Spaghetti Bolognese x 2 24.72 €", [24.72]), ("Pizza x 1 11.33 €", [11.33]),
        ("Customization", [])]


# ── What the run paid, from its notes ────────────────────────────────────────

RUN_NOTES = [
    ("consumer", "[ok] @pay_stripe — tapped 'Pay €273.57' (bill €273.57) (40.0s)"),
    ("participant", "[ok] @pay_stripe — tapped 'Pay €108.50' (bill €108.5) (13.6s)"),
    ("consumer", "[ok] @c_tip:2 — Total 74.95 € + tip 2.00 € = 76.95 € (3.0s)"),
    ("consumer", "[ok] @consumer_epay — paid €76.95 by E-Payment (Visa ending 4242); "
                 "the booking shows Completed"),
    ("waiter", "[ok] @w_tip:Noolu Naga 3 — Noolu Naga's Total 208.38 € + tip 3 € = 211.38 €"),
    ("waiter", "[ok] Bill check: total €211.38, paid €211.39 by epay; Confirm Payment accepted"),
    ("waiter", "[ok] Bill check: total €33.96, paid €43.96 by cash, change €10.00; Confirm"),
]


def test_everyones_payments_come_from_the_notes():
    paid = ic.paid_from_notes(RUN_NOTES)
    assert paid["preorder"] == [273.57, 108.50]
    assert paid["table"] == [76.95, 211.38, 33.96]
    assert paid["tips"] == [2.0, 3.0]
    assert paid["overpaid"] == 10.01 and not paid["incomplete"]


def test_one_diners_payments_come_from_the_notes():
    paid = ic.paid_from_notes(RUN_NOTES, role="consumer")
    assert paid["preorder"] == [273.57]
    assert paid["table"] == [76.95]
    assert paid["tips"] == [2.0]


@pytest.mark.parametrize("line", [
    "[skip] @pay:epay — nothing left to pay: Noolu paid earlier",
    "[skip] @consumer_epay — nothing left to pay: @consumer_checkout found the booking paid",
    "    · @close_table: Close Table is not visible and the payment is not done (x) — "
    "paying by E-Payment again",
])
def test_a_payment_not_made_exactly_once_makes_the_figures_incomplete(line):
    assert ic.paid_from_notes(RUN_NOTES + [("waiter", line)])["incomplete"]


# ── The steps ────────────────────────────────────────────────────────────────

def test_the_full_event_ends_with_the_verdict_on_the_invoices():
    assert FLOWS["flow_full_event"]["segments"][-1]["steps"][-1] == "@invoice_verdict"
    assert FLOWS["flow_full_event_eve"]["segments"][-1]["steps"][-1] == "@invoice_verdict"


def verdict(lines, notes=None):
    fr = FlowRunner.__new__(FlowRunner)
    fr._run_notes = lambda: [("waiter", line) for line in lines]
    notes = [] if notes is None else notes
    return fr._invoice_verdict(notes), notes


def test_the_verdict_names_every_sum_that_did_not_match():
    # The user, 2026-11-04: "make the 21 passed" -- a mismatch is a red line, not a FAIL.
    ok, notes = verdict(["    · invoice checked: 9 sums, 1 do not match",
                         "[mismatch] @w_invoice — On Premises grand total: off by 1.00",
                         "[mismatch] @c_invoice — Pre-Order dishes: off by 0.50"])
    assert ok
    red = [n for n in notes if n.startswith("[mismatch] @invoice_verdict")]
    assert len(red) == 2 and "off by 1.00" in red[0] and "off by 0.50" in red[1]
    assert notes[-1].startswith("[ok] @invoice_verdict") and "2 calculation(s)" in notes[-1]


def test_the_verdict_passes_when_every_invoice_added_up():
    ok, notes = verdict(["    · invoice checked: 9 sums, 0 do not match"])
    assert ok and notes[-1] == "[ok] @invoice_verdict — every invoice added up"


def test_the_verdict_does_not_pass_a_run_that_checked_nothing():
    """A resume can carry the invoice segments over from code that did not check them."""
    ok, notes = verdict(["[ok] @w_invoice — event invoice: Total 322.29 €"])
    assert not ok and "no invoice was checked" in notes[-1]


def test_a_mismatch_lets_the_step_pass_so_the_run_can_finish_the_event():
    fr = FlowRunner.__new__(FlowRunner)
    parsed = blocks(("Pre-Order",), ("Penne x 1", "10.00 €"), ("Total", "12.00 €"),
                    ("VAT 3% INCL. :", "9.71€", "0.29€", "10.00€"))
    fr._invoice_read = (parsed, 0, False)
    fr._run_notes = lambda: []
    notes = []
    fr._check_invoice("@w_invoice", notes, comps_printed=False)
    assert any(n.startswith("[mismatch] @w_invoice — Pre-Order grand total") for n in notes)
    assert any(n.startswith("    · Not compared") for n in notes)


class Scrolling:
    """An invoice three screens long, as _read_invoice sees it."""

    def __init__(self, screens):
        self.screens, self.at, self.swipes = screens, 0, 0

    def read_text(self, udid, region=None, els=None, accurate=False):
        assert accurate, "money is read in accurate mode"
        return self.screens[self.at]

    def swipe(self, udid, *a):
        self.swipes += 1
        self.at = min(self.at + 1, len(self.screens) - 1)


def test_the_invoice_is_read_to_its_end_past_the_pre_order_vat_table(monkeypatch):
    """The old reader stopped at the first VAT rows: the Pre-Order block's."""
    from automation.scenarios import screen_text
    rows = PRE_ORDER + EVENT_TABLE
    inv = Scrolling([page(*rows[:10], gap=25.0), page(*rows[5:15], gap=25.0),
                     page(*rows[10:], gap=25.0)])
    monkeypatch.setattr(screen_text, "read_text", inv.read_text)
    monkeypatch.setattr(caf._idbd, "swipe", inv.swipe)
    monkeypatch.setattr(caf.time, "sleep", lambda s: None)
    fr = FlowRunner.__new__(FlowRunner)
    seen = fr._read_invoice("IPAD", (0, 40, 480, 800))
    assert seen["Total"] == "Total 322.29 €"
    parsed, gaps, cut = fr._invoice_read
    assert gaps == 0 and not cut
    assert [b.totals[-1] for b in parsed] == [168.27, 322.29]
    assert ic.check(parsed, comps_printed=False).problems == []


def test_running_out_of_screens_says_the_end_was_not_reached(monkeypatch):
    from automation.scenarios import screen_text
    screens = [page(*ROWS[i:i + 8]) for i in range(0, 12)]
    inv = Scrolling(screens)
    monkeypatch.setattr(screen_text, "read_text", inv.read_text)
    monkeypatch.setattr(caf._idbd, "swipe", inv.swipe)
    monkeypatch.setattr(caf.time, "sleep", lambda s: None)
    monkeypatch.setattr(FlowRunner, "INVOICE_SCREENS", 3)
    fr = FlowRunner.__new__(FlowRunner)
    fr._read_invoice("IPAD", (0, 40, 480, 800))
    assert fr._invoice_read[2] is True


def test_the_run_notes_are_read_with_their_segments_roles(monkeypatch):
    class Rows:
        def filter_by(self, **kw):
            assert kw == {"run_id": "R"}
            return self

        def all(self):
            return [types.SimpleNamespace(scenario_num="18", reasons=["b"]),
                    types.SimpleNamespace(scenario_num="1", reasons=["a"]),
                    types.SimpleNamespace(scenario_num="2", reasons=None)]

    class Db:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def query(self, model):
            return Rows()

    monkeypatch.setattr(caf, "SessionLocal", Db)
    fr = FlowRunner.__new__(FlowRunner)
    fr.run_id = "R"
    fr.flow = FLOWS["flow_full_event"]
    assert fr._run_notes() == [("consumer", "a"), ("waiter", "b")]
    fr.run_id = None
    assert fr._run_notes() == []


# ── Payment sums, noted and not failed; the report's tables ──────────────────

def test_a_coupon_percent_or_euro_is_read_from_its_tile():
    assert ic.offer_of("31.5% OFFER 31.5 31.5 - In 2 Days") == (31.5, None)
    assert ic.offer_of("5% OFFER") == (5.0, None)
    assert ic.offer_of("2€ OFFER") == (None, 2.0)
    assert ic.offer_of("Coupon Festival") == (None, None)


def test_a_coupon_that_takes_off_its_percent_matches():
    """MEASURED 2026-10-29 by hand: 109.41 with the 31.5 % coupon became 74.95."""
    table, problem = ic.offer_check("Coupon", 109.41, 74.95, "31.5% OFFER", "host")
    assert problem is None
    assert ic.calc_of(table)["r"] == [["109.41", "-31.5%", "74.95", "74.95", "+0.00", True]]


@pytest.mark.parametrize("shown, words", [
    (109.41, "before €109.41, calculated €74.95 but displayed €109.41 (diff +34.46)"),
    (80.00, "calculated €74.95 but displayed €80.00 (diff +5.05)"),
    (None, "could not read the Total after it"),
])
def test_a_coupon_that_does_not_take_off_its_percent_is_named(shown, words):
    table, problem = ic.offer_check("Coupon", 109.41, shown, "31.5% OFFER", "host")
    assert problem.startswith("Coupon 31.5% (host): ") and words in problem
    assert ic.calc_of(table)["r"][0][-1] is False


def test_a_euro_discount_and_an_unreadable_one():
    assert ic.offer_check("Discount", 50.0, 48.0, "2€ OFFER")[1] is None
    # The value could not be read: it must at least have fallen.
    assert ic.offer_check("Discount", 50.0, 49.0, "Festival")[1] is None
    assert "did not fall" in ic.offer_check("Discount", 50.0, 50.0, "Festival")[1]


def test_a_tip_is_added_to_the_total_or_named():
    assert ic.tip_check("host", 74.95, 2.0, 76.95)[1] is None
    problem = ic.tip_check("Noolu Naga", 208.38, 3.0, 208.38)[1]
    assert problem == ("Tip 3.00 (Noolu Naga): before €208.38, calculated €211.38 but "
                       "displayed €208.38 (diff -3.00)")


def test_a_charge_is_the_bill_or_named():
    assert ic.charge_check("Pre-order payment (host)", 273.57, 273.57)[1] is None
    assert "bill displayed €108.50 but charged €118.50" in ic.charge_check(
        "Pre-order payment (participant)", 108.50, 118.50)[1]
    table, problem = ic.charge_check("E-Payment (host)", None, 76.95, "Charged by Stripe")
    assert problem is None and ic.calc_of(table)["r"] == [["—", "76.95", "—", None]]
    assert ic.euro_after_sign("Pay €273.57") == 273.57


def test_a_calc_note_keeps_its_table_and_nothing_else_passes_for_one():
    note = ic.calc_note("T", ["a", "b"], [["1.00", "2.00", True]])
    assert note.startswith("[calc] ") and ic.calc_of(note) == {"t": "T", "h": ["a", "b"],
                                                                "r": [["1.00", "2.00", True]]}
    assert ic.calc_of("[calc] {broken") is None and ic.calc_of("[ok] x") is None


def test_an_invoice_check_gives_the_reports_tables_with_the_wrong_line_marked():
    rows = [("Pre-Order",), ("Penne x 1", "10.00 €"), ("Total", "12.00 €"),
            ("VAT 3% INCL. :", "9.71€", "0.29€", "10.00€")]
    rep = ic.check(blocks(*rows), title="Event invoice (iPad)")
    tables = {t["t"]: t for t in map(ic.calc_of, rep.tables)}
    totals = tables["Event invoice (iPad) — Pre-Order: totals"]
    assert totals["h"] == ["Check", "How", "Calculated", "Displayed", "Diff"]
    by_check = {r[0]: r for r in totals["r"]}
    assert by_check["grand total"][2:] == ["10.00", "12.00", "+2.00", False]
    assert tables["Event invoice (iPad) — Pre-Order: VAT"]["r"] == [
        ["VAT 3%", "9.71", "0.29", "10.00", "10.00", "0.29", True]]


def test_both_blocks_and_the_whole_invoice_get_tables():
    rep = ic.check(blocks(*PRE_ORDER, *EVENT_TABLE), comps_printed=False,
                   title="Event invoice (iPad)")
    titles = [ic.calc_of(t)["t"] for t in rep.tables]
    assert titles == ["Event invoice (iPad) — Pre-Order: totals",
                      "Event invoice (iPad) — Pre-Order: VAT",
                      "Event invoice (iPad) — On Premises: totals",
                      "Event invoice (iPad) — On Premises: VAT",
                      "Event invoice (iPad) — whole invoice"]


def _runner(monkeypatch, screen=()):
    fr = FlowRunner.__new__(FlowRunner)
    fr._cur_udid, fr.devices = "DEVICE", {}
    fr._wait_els = lambda udid, pred, timeout: (list(screen), pred(list(screen)))
    monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: list(screen))
    for fn in ("tap", "tap_el", "tap_point"):
        monkeypatch.setattr(caf._idbd, fn, lambda *a, **k: (True, "idb"))
    t = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: t["t"], sleep=lambda s: t.__setitem__("t", t["t"] + s)))
    return fr


def test_a_tip_the_total_does_not_take_is_noted_and_the_payment_goes_on(monkeypatch):
    fr = _runner(monkeypatch)
    fr._c_sheet_total = lambda els: 74.95
    monkeypatch.setattr(caf._idbd, "fill", lambda udid, ident, text: (True, "idb"))
    notes = []
    assert fr._c_tip("2", notes) is True
    assert "[mismatch] @c_tip:2 — Tip 2.00 (host): before €74.95, calculated €76.95 but " \
           "displayed €74.95 (diff -2.00)" in notes
    assert not any(n.startswith("[FAIL]") for n in notes)
    assert ic.paid_from_notes([("consumer", n) for n in notes])["tips"] == [2.0]


def test_a_tip_the_total_takes_is_one_matching_table(monkeypatch):
    fr = _runner(monkeypatch)
    totals = iter([74.95])
    fr._c_sheet_total = lambda els: next(totals, 76.95)
    monkeypatch.setattr(caf._idbd, "fill", lambda udid, ident, text: (True, "idb"))
    notes = []
    assert fr._c_tip("2", notes) is True
    assert not any(n.startswith("[mismatch]") for n in notes)
    assert ic.calc_of(next(n for n in notes if n.startswith("[calc]")))["r"][0][-1] is True


@pytest.mark.parametrize("after, mismatch", [(74.95, None), (80.00, "diff +5.05")])
def test_the_coupon_is_checked_against_its_percent(monkeypatch, after, mismatch):
    fr = _runner(monkeypatch, [el("31.5% OFFER 31.5 31.5 - In 2 Days")])
    totals = iter([109.41])
    fr._c_sheet_total = lambda els: next(totals, after)
    notes = []
    assert fr._c_coupon(notes) is True
    found = [n for n in notes if n.startswith("[mismatch]")]
    assert (found == []) if mismatch is None else (mismatch in found[0]), notes


def test_a_discount_the_server_refuses_is_noted_with_its_reason_and_payment_goes_on(monkeypatch):
    from automation.scenarios import screen_text
    fr = _runner(monkeypatch)
    fr._open_card = lambda udid, who, ident: ([], {}, {"AXLabel": ident})
    fr._profile_region = lambda es, who: ({}, 0, 0)
    fr._payer_card_total = lambda es, card: 100.0
    fr._screen_toast = lambda udid: "This coupon needs a bill above 500€"
    monkeypatch.setattr(screen_text, "read_text",
                        lambda udid, region=None, els=None, accurate=False:
                        [("5% OFFER", 420, 300, 80, 16)])
    notes = []
    assert fr._w_discount("Noolu Naga", notes) is True
    found = [n for n in notes if n.startswith("[mismatch]")]
    assert found == ["[mismatch] @w_discount:Noolu Naga — Discount 5% (Noolu Naga): before "
                     "€100.00, calculated €95.00 but displayed €100.00 (diff +5.00) (the app "
                     "said 'This coupon needs a bill above 500€')"]


def el(name, x=0, y=0, w=100, h=40):
    return {"AXLabel": name, "AXIdentifier": name, "type": "GenericElement",
            "frame": {"x": x, "y": y, "width": w, "height": h}}


# ── The run's HTML report ─────────────────────────────────────────────────────

def _record(status, reasons, error=""):
    return {"num": "17", "name": "Host pays", "status": status, "consumer_status": status,
            "business_status": "N/A", "reasons": reasons, "error": error,
            "launch_time": 12.0, "screenshot": ""}


def test_the_report_draws_each_table_and_reds_only_the_line_that_did_not_match():
    from automation.reporting import scenario_report as sr
    bad, problem = ic.offer_check("Coupon", 109.41, 109.41, "31.5% OFFER", "host")
    good, _ = ic.tip_check("host", 109.41, 2.0, 111.41)
    rec = _record("PASS", ["[ok] @c_coupon — applied", bad, "[mismatch] @c_coupon — " + problem,
                           good])
    html = sr.render_run_report([rec, _record("PASS", ["[ok] @open_order"])], title="Full event")
    assert "Coupon 31.5% (host)" in html and "Tip 2.00 (host)" in html
    assert html.count('<tr style="color:#e74c3c;font-weight:600">') == 1      # only that line
    assert html.count('<tr style="color:#15803d">') == 1
    assert "1. @c_coupon — Coupon 31.5% (host): before €109.41, calculated €74.95 but " \
           "displayed €109.41 (diff +34.46)" in html
    # A mismatch is a red line, not a failed segment: the run's own status stands.
    assert "FAILED" not in html
    assert 'color:#e74c3c">0</div><div class="lbl">Failed' in html
    assert 'color:#27ae60">2</div><div class="lbl">Passed' in html
    assert "[calc]" not in html


def test_a_plain_bill_check_note_draws_no_empty_box():
    from automation.reporting import scenario_report as sr
    html = sr.render_run_report([_record("PASS", [
        "[ok] Bill check: total €36.05, paid €36.05 by epay; Confirm Payment accepted"])],
        title="t")
    assert "Pre-Payment Bill Check" not in html


def test_the_invoice_step_leaves_its_tables_in_the_notes():
    fr = FlowRunner.__new__(FlowRunner)
    fr._invoice_read = (blocks(*PRE_ORDER), 0, False)
    fr._run_notes = lambda: []
    notes = []
    fr._check_invoice("@c_invoice", notes, comps_printed=True, title="Invoice of the host (phone)")
    titles = [ic.calc_of(n)["t"] for n in notes if n.startswith("[calc]")]
    assert titles == ["Invoice of the host (phone) — Pre-Order: totals",
                      "Invoice of the host (phone) — Pre-Order: VAT"]


def test_a_difference_of_nothing_never_reads_minus_zero():
    assert ic._diff(322.29, 288.33 + 33.96) == "+0.00"
