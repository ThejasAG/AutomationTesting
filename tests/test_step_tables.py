"""The run report's calculation tables read from each step's '[ok]' line (automation/
reporting/step_tables.py). The lines are the ones run 1107222a (flow_full_event, all 21
segments passed, 2026-11-04) wrote."""
from automation.reporting import scenario_report as sr
from automation.reporting.step_tables import tables_of
from automation.scenarios import invoice_check as ic


def table(notes, title):
    found = [t for t in tables_of(notes) if t["t"] == title]
    assert len(found) == 1, [t["t"] for t in tables_of(notes)]
    return found[0]


def test_a_pre_order_shows_only_its_payment_calculation():
    notes = [
        "[ok] @invite_guests:5+Noolu Naga — invited 5 guest(s) + Noolu Naga; Persons 1 → 7 (43.6s)",
        "[ok] @c_add:Penne Pollo — added 1 × Penne Pollo (10.9s)",
        "[ok] @c_add:Pizza Margheritas*2[large, veg] — added 2 × Pizza Margheritas (large, veg); "
        "tile 0 → 2 (19.5s)",
        "[ok] @to_checkout — opened checkout (8.7s)",
        "[ok] @pay_stripe — tapped 'Pay €273.57' (bill €273.57) (11.3s)",
    ]
    assert [t["t"] for t in tables_of(notes)] == ["Pre-payment Total"]
    pay = table(notes, "Pre-payment Total")["r"][0]
    assert pay[:4] == ["Pre-order payment", "€273.57", "€273.57", "+0.00"] and pay[-1] is True


def test_a_charge_that_differs_from_the_bill_is_red():
    row = table(["[ok] @pay_stripe — tapped 'Pay €108.60' (bill €108.5) (12.5s)"],
                "Pre-payment Total")["r"][0]
    assert row[3] == "+0.10" and row[-1] is False


def test_a_payment_step_that_wrote_its_own_table_is_not_read_again():
    own = ic.calc_note("Pre-order payment (host)", ["Bill", "Charged", "Diff"],
                       [["273.57", "273.57", "+0.00", True]])
    notes = ["[ok] @pay_stripe — tapped 'Pay €273.57' (bill €273.57) (11.3s)", own]
    assert [t["t"] for t in tables_of(notes)] == ["Pre-order payment (host)"]


def test_the_waiters_adds_show_the_amount_and_each_persons_share():
    notes = [
        "[ok] @w_add:Coca-Cola® Light 1.0l*5 > split: Guest 1, Guest 2, Guest 3, Guest 4, Guest 5 "
        "— added 5 × Coca-Cola® Light 1.0l, split among Guest 1, Guest 2, Guest 3, Guest 4, "
        "Guest 5: 0 → 5 row(s), +30.50 € on the order (52.7s)",
        "[ok] @w_add:Pizza Margheritas*2[large, veg] > assign: Guest 2, Guest 3 — added 2 × "
        "Pizza Margheritas (large, veg), assigned to Guest 2, Guest 3: 2 → 4 row(s), +90.32 € "
        "on the order (97.5s)",
    ]
    t = table(notes, "Added by the waiter — amount and each person's share")
    assert t["h"] == ["#", "Item", "Qty", "Options", "Assign / Split", "To",
                      "Added to the order", "Each person"]
    assert t["r"][0] == [1, "Coca-Cola® Light 1.0l", 5, "—", "Split",
                         "Guest 1, Guest 2, Guest 3, Guest 4, Guest 5", "€30.50", "€6.10 × 5",
                         True]
    assert t["r"][1][3:] == ["large, veg", "Assign", "Guest 2, Guest 3", "€90.32", "€45.16 × 2",
                             True]


def test_a_split_must_keep_the_lines_total():
    good = table(["[ok] @w_split:Pizza Salamees@Guest 5 > Guest 4 — split Guest 5's Pizza "
                  "Salamees with Guest 4: 166.80 € → 83.40 € each (57.4s)"],
                 "Split — Grand Total of the line before and after")
    assert good["r"] == [["Pizza Salamees", "Guest 5", "Guest 4", "€166.80", "€83.40 × 2",
                          "€166.80", "+0.00", True]]
    bad = table(["[ok] @w_split:Pizza Salamees@Guest 5 > Guest 4 — split Guest 5's Pizza "
                 "Salamees with Guest 4: 166.80 € → 90.00 € each (57.4s)"],
                "Split — Grand Total of the line before and after")
    assert bad["r"][0][-2:] == ["+13.20", False]


COMPS = [
    "[ok] @w_comp:Pizza Salamees@Guest 4*all:100 — comped 5 unit(s) of Guest 4's Pizza "
    "Salamees at 100% (manager discretion); 69.50 € → 0.00 € (124.6s)",
    "[ok] @w_comp:Pizza Margheritas@Guest 4*1:10 — comped 1 unit(s) of Guest 4's Pizza "
    "Margheritas at 10% (manager discretion); 33.99 € → 22.66 € (126.5s)",
    "[ok] @w_comp:2020 Bonacci Chianti Classico Riserva 0,75lj@Guest 3*1:25 — comped 1 unit(s) "
    "of Guest 3's 2020 Bonacci Chianti Classico Riserva 0,75lj at 25% (manager discretion); "
    "78.98 € → 52.65 € (108.3s)",
    "[ok] @w_comp:Coca-Cola® Light 1.0l@Guest 1*1:15 — comped 1 unit(s) of Guest 1's "
    "Coca-Cola® Light 1.0l at 15% (manager discretion); 6.10 € → 4.88 € (52.5s)",
]


def test_each_comp_is_calculated():
    t = table(COMPS, "Comps")
    assert t["h"] == ["#", "Item (guest)", "Units comped", "Comp", "Unit price",
                      "Comp amount", "Expected line after", "Displayed line after", "Diff",
                      "Comped line should read"]
    whole, pizza, wine, coke = t["r"]
    assert whole[1:] == ["Pizza Salamees (Guest 4)", "5 (whole line)", "100%",
                         "€13.90", "€69.50", "€0.00", "€0.00", "+0.00", "— (comped in place)",
                         True]
    assert pizza[2:] == ["1 of 3", "10%", "€11.33", "€1.13", "€22.66", "€22.66",
                         "+0.00", "€10.20", True]
    assert wine[2] == "1 of 3" and wine[5] == "€6.58" and wine[-2] == "€19.75" and wine[-1]
    # 1.22 € off the share, 15 %: 0.18 € comped -- the host's invoice printed 'comps 0.18'.
    assert coke[2:] == ["1 of 5", "15%", "€1.22", "€0.18", "€4.88", "€4.88", "+0.00",
                        "€1.04", True]
    assert "Line before" not in t["h"]


def test_the_comps_are_summed_after_their_table():
    assert [t["t"] for t in tables_of(COMPS)] == ["Comps", "Comps — Final Summary"]
    rows = table(COMPS, "Comps — Final Summary")["r"]
    assert rows[-1] == ["", "Total comped", "€77.39", None]      # 69.50 + 1.13 + 6.58 + 0.18


def test_a_whole_comp_that_leaves_the_wrong_price_is_red():
    row = table(["[ok] @w_comp:Penne Pollo@Guest 1*all:50 — comped 2 unit(s) of Guest 1's "
                 "Penne Pollo at 50% (manager discretion); 20.00 € → 12.00 € (50.0s)"],
                "Comps")["r"][0]
    assert row[4:8] == ["€10.00", "€10.00", "€12.00", "+2.00"] and row[-1] is False


def test_a_one_unit_comp_that_did_not_take_a_whole_unit_is_red():
    row = table(["[ok] @w_comp:Coca-Cola® Light 1.0l@Guest 1*1:15 — comped 1 unit(s) of "
                 "Guest 1's Coca-Cola® Light 1.0l at 15% (manager discretion); 6.10 € → "
                 "5.19 € (52.5s)"], "Comps")["r"][0]
    assert row[-1] is False and "whole number of units" in row[-2]


def test_steps_that_calculate_nothing_make_no_table():
    notes = [
        "[ok] @w_qty:Guest 5/Pizza Salamees =6 — Guest 5's Pizza Salamees: 5 → 6 (63.6s)",
        "[ok] @w_move:Pizza Margheritas@Guest 2 > Noolu Naga — moved Guest 2's Pizza "
        "Margheritas to Noolu Naga (48.5s)",
        "[ok] @w_gsplit:Coca-Cola® Light 1.0l@Noolu Naga > Guest 3 — split Noolu Naga's "
        "Coca-Cola® Light 1.0l with Guest 3: Noolu Naga's card reads ['x 2/2'] (149.1s)",
        "[ok] @w_void:Pizza Salamees@Guest 5=5 — voided 1 unit(s) of Guest 5's Pizza Salamees "
        "(entry error): Pizza Salamees x 6/2 → Pizza Salamees x 5/2 (255.5s)",
        "[ok] @kitchen_ready — ticket 5606: selected 4 item(s) (PennePollo) → Ready (41.4s)",
        "[ok] send to kitchen — tapped SEND; the order was sent (button cleared) (7.4s)",
        "[ok] @c_confirm_order — CONFIRM ORDER sent the order: 'Order Confirmed' (9.9s)",
        "[ok] @notify_payment — tapped NOTIFY PAYMENT, then Yes; payment notified (119.3s)",
        "    · @w_split:Pizza Salamees@Guest 5 > Guest 4: tapped 'Apply' at (604, 544)",
        "    [APP CRASH] the business app quit; relaunched it",
    ]
    assert tables_of(notes) == []


def test_the_closed_table_checks_the_outstanding_balance():
    t = table(["[ok] @close_table — the bill is fully paid and the app closed the order "
               "itself: 5606  COMPLETED 10:38 - 11:38 (0.0s)"], "Outstanding Balance Check")
    assert t["r"][0][:2] == ["Outstanding balance", "€0.00"] and t["r"][0][-1] is True


def test_the_verdict_lists_every_sum_of_the_run_that_did_not_match():
    notes = [
        "[mismatch] @invoice_verdict — @w_invoice — On Premises payments: 280.47 + 88.31 + 0.00 "
        "= 368.78 against Total 367.74 — 1.04 unexplained",
        "[mismatch] @invoice_verdict — @c_invoice — On Premises payments: 39.05 = 39.05 against "
        "Total 48.02 — 8.97 unexplained",
        "[ok] @invoice_verdict — the invoices were checked; 2 calculation(s) did not match "
        "(red above) (0.0s)",
    ]
    t = table(notes, "Calculation checks of the whole run")
    assert [r[1] for r in t["r"]] == ["@w_invoice", "@c_invoice"]
    assert all(r[-1] is False for r in t["r"])


def test_a_line_that_cannot_be_read_keeps_its_words():
    t = table(["[ok] @w_add:Penne Pollo*3 > split: Guest 1, Guest 2 — already on Guest 1, "
               "Guest 2's card(s) (added by the run this one resumes) (3.0s)"],
              "Added by the waiter — amount and each person's share")
    assert "already on Guest 1" in " ".join(map(str, t["r"][0])) and t["r"][0][-1] is None


# ── the report ────────────────────────────────────────────────────────────
def _rec(num, name, reasons, status="PASS"):
    return {"num": num, "name": name, "status": status, "consumer_status": "N/A",
            "business_status": status, "error": "" if status == "PASS" else "@x — broke",
            "screenshot": "", "launch_time": 10.0, "reasons": reasons}


def test_segments_without_a_calculation_are_listed_last_as_just_passed():
    recs = [
        _rec("4", "Waiter: quantities +1 / -1, remove a product",
             ["[ok] @w_qty:Guest 5/Pizza Salamees =6 — Guest 5's Pizza Salamees: 5 → 6 (63.6s)"]),
        _rec("5", "Waiter: swipe-split, void a split item, send all to the kitchen",
             ["[ok] @w_split:Pizza Salamees@Guest 5 > Guest 4 — split Guest 5's Pizza Salamees "
              "with Guest 4: 166.80 € → 83.40 € each (57.4s)"]),
        _rec("7", "Kitchen: mark every item ready", ["[ok] @kitchen_ready — ticket 5606: Ready"],
             status="FAIL"),
        _rec("11", "Waiter: four comps", COMPS[:1]),
    ]
    html = sr.render_run_report(recs, title="Full event")
    order = [html.index(f"{n}. ") for n in ("5", "7", "11", "4")]
    assert order == sorted(order)                       # 5, 7 (failed), 11, then 4
    assert html.index("Passed — no calculation in these segments") < html.index("4. Waiter")
    assert "Quantity changes" not in html and "Steps</div>" not in html
    assert 'color:#27ae60">4</div>' not in html and 'color:#27ae60">3</div><div class="lbl">Passed' in html


def test_the_segment_title_is_bold_with_what_it_does_under_it():
    html = sr.render_run_report([_rec("11", "Waiter: four comps", COMPS[:1])], title="T")
    assert "11. Waiter<div" in html and "four comps</div>" in html
