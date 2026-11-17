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


def test_the_waiters_assigned_adds_show_the_amount_and_each_persons_share():
    notes = [
        "[ok] @w_add:Coca-Cola® Light 1.0l*5 > split: Guest 1, Guest 2, Guest 3, Guest 4, Guest 5 "
        "— added 5 × Coca-Cola® Light 1.0l, split among Guest 1, Guest 2, Guest 3, Guest 4, "
        "Guest 5: 0 → 5 row(s), +30.50 € on the order (52.7s)",
        "[ok] @w_add:Pizza Margheritas*2[large, veg] > assign: Guest 2, Guest 3 — added 2 × "
        "Pizza Margheritas (large, veg), assigned to Guest 2, Guest 3: 2 → 4 row(s), +90.32 € "
        "on the order (97.5s)",
    ]
    # The split one is a split (the split sample's layout); the assigned one stays here.
    assert [t["t"] for t in tables_of(notes)] == [
        "Direct Split (while assigning)",
        "Per-member split check — Coca-Cola® Light 1.0l split 5 ways",
        "Added by the waiter — amount and each person's share"]
    t = table(notes, "Added by the waiter — amount and each person's share")
    assert t["h"] == ["Item", "Qty", "Options", "Assign / Split", "To", "Added to the order",
                      "Each person"]
    assert t["r"] == [["Pizza Margheritas", 2, "large, veg", "Assign", "Guest 2, Guest 3",
                       "€90.32", "€45.16 × 2", True]]


SWIPE = ("[ok] @w_split:Pizza Salamees@Guest 5 > Guest 4 — split Guest 5's Pizza Salamees "
         "with Guest 4: 166.80 € → 83.40 € each (57.4s)")
PENNE = ("[ok] @w_add:Penne Pollo*3 > split: Guest 1, Guest 2 — added 3 × Penne Pollo, split "
         "among Guest 1, Guest 2: 1 → 3 row(s), +52.53 € on the order (144.0s)")


def _shares(step, rows, total, qty=None):
    return ic.calc_note("Split shares — " + step, ["Member", "Line on their card", "Price"],
                        [r + [None] for r in rows], total=total, qty=qty, n=len(rows))


def test_a_split_without_its_shares_is_filled_from_what_the_run_read():
    # Run 1107222a read only the owner's share; the rest of the line is the other's.
    gt = table([SWIPE], "Swipe Split")
    assert gt["h"] == ["Item", "Grand Total before split", "Grand Total after split"]
    assert gt["plain"] and gt["r"] == [["Pizza Salamees (Guest 5 → Guest 4)", "€166.80",
                                        "€166.80", None]]
    check = table([SWIPE], "Per-member split check — Pizza Salamees split 2 ways")
    assert check["h"] == ["Check", "Expected (each)", "Found"]
    frac, price = check["r"]
    assert frac[2] == "Guest 5's line is its share"
    assert price == ["Price per member", "€83.40 each (total €166.80)",
                     "€83.40 + €83.40 = €166.80", True]
    assert check["note"] == "Guest 5's share read on the iPad; Guest 4's is the rest of the line."
    assert "not recorded" not in str(gt) + str(check)


def test_a_split_while_assigning_without_shares_uses_its_rows_on_the_order():
    gt = table([PENNE], "Direct Split (while assigning)")
    assert gt["r"][0][1:3] == ["€52.53", "€52.53"]
    frac, price = table([PENNE], "Per-member split check — Penne Pollo split 2 ways")["r"]
    assert frac[2] == "2 new rows, one per member"
    assert price[2] == "2 shares = €52.53"


def test_a_split_whose_shares_add_up_passes():
    notes = [SWIPE, _shares("@w_split:Pizza Salamees@Guest 5 > Guest 4",
                            [["Guest 5", "Pizza Salamees x 6/2", "83.40"],
                             ["Guest 4", "Pizza Salamees x 6/2", "83.40"]], 166.80)]
    assert table(notes, "Swipe Split")["r"] == [
        ["Pizza Salamees (Guest 5 → Guest 4)", "€166.80", "€166.80", None]]
    frac, price = table(notes, "Per-member split check — Pizza Salamees split 2 ways")["r"]
    assert frac == ["Quantity fraction", "6/2 × 2 members", "2 rows with '6/2'", True]
    assert price == ["Price per member", "€83.40 each (total €166.80)",
                     "€83.40 + €83.40 = €166.80", True]
    # The shares table is read into these, not drawn on its own.
    assert not any(t["t"].startswith("Split shares") for t in tables_of(notes))


def test_shares_a_cent_apart_pass_when_they_add_up():
    # The user, 2026-11-05: 'PASS if it adds up'.
    notes = [PENNE, _shares("@w_add:Penne Pollo*3 > split: Guest 1, Guest 2",
                            [["Guest 1", "Penne Pollo x 3/2", "26.27"],
                             ["Guest 2", "Penne Pollo x 3/2", "26.26"]], 52.53, 3)]
    frac, price = table(notes, "Per-member split check — Penne Pollo split 2 ways")["r"]
    assert frac[1:] == ["3/2 × 2 members", "2 rows with '3/2'", True]
    assert price[1:] == ["€26.26 or €26.27 each (total €52.53)",
                         "€26.27 + €26.26 = €52.53", True]


def test_shares_that_do_not_add_up_are_red():
    notes = [PENNE, _shares("@w_add:Penne Pollo*3 > split: Guest 1, Guest 2",
                            [["Guest 1", "Penne Pollo x 3/2", "26.27"],
                             ["Guest 2", "Penne Pollo x 3/1", "26.27"]], 52.53, 3)]
    assert table(notes, "Direct Split (while assigning)")["r"][0][-2:] == ["€52.54", False]
    frac, price = table(notes, "Per-member split check — Penne Pollo split 2 ways")["r"]
    assert frac[-2:] == ["1 row with '3/2'", False]
    assert price[-1] is False


def test_a_toggle_split_shows_the_card_it_read():
    notes = ["[ok] @w_gsplit:Coca-Cola® Light 1.0l@Noolu Naga > Guest 3 — split Noolu Naga's "
             "Coca-Cola® Light 1.0l with Guest 3: Noolu Naga's card reads "
             "['Coca-Cola® Light 1.0l x 2/2'] (149.1s)"]
    assert [t["t"] for t in tables_of(notes)] == [
        "Toggle Split", "Per-member split check — Coca-Cola® Light 1.0l split 2 ways"]
    frac = table(notes, "Per-member split check — Coca-Cola® Light 1.0l split 2 ways")["r"][0]
    assert frac[1:3] == ["2/2 × 2 members", "'2/2' on Noolu Naga's card"]


def test_a_toggle_split_starts_from_the_owners_assigned_line():
    from automation.reporting.step_tables import run_context, tables_of
    assign = ("[ok] @w_add:Coca-Cola® Light 1.0l*2 > assign: Roopa D, Noolu Naga — added 2 × "
              "Coca-Cola® Light 1.0l, assigned to Roopa D, Noolu Naga: 5 → 7 row(s), +24.40 € "
              "on the order (81.0s)")
    toggle = ("[ok] @w_gsplit:Coca-Cola® Light 1.0l@Noolu Naga > Guest 3 — split Noolu Naga's "
              "Coca-Cola® Light 1.0l with Guest 3: Noolu Naga's card reads "
              "['Coca-Cola® Light 1.0l x 2/2'] (149.1s)")
    ctx = run_context([{"reasons": [assign]}, {"reasons": [toggle]}])
    gt, check = tables_of([toggle], ctx)
    assert gt["r"][0][1:3] == ["€12.20", "€12.20"]
    assert "before is Noolu Naga's line as assigned in segment 3" in gt["note"]
    assert check["r"][1][1:3] == ["€6.10 each (total €12.20)", "€6.10 × 2 = €12.20"]


def test_the_split_tables_are_drawn_like_the_sample():
    notes = [PENNE, _shares("@w_add:Penne Pollo*3 > split: Guest 1, Guest 2",
                            [["Guest 1", "Penne Pollo x 3/2", "26.27"],
                             ["Guest 2", "Penne Pollo x 3/2", "26.26"]], 52.53, 3)]
    rec = {"num": "3", "name": "Waiter: add", "status": "PASS", "reasons": notes}
    html = sr.render_run_report([rec], title="T")
    assert "Per-member split check — <b>Penne Pollo</b> split 2 ways" in html
    gt = html[html.index("Direct Split (while assigning)"):html.index("Per-member")]
    assert "Grand Total before split" in gt and ">Status<" not in gt


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
    # The host's invoice printing 39.05 of 48.02 (a payment booked on guests) is
    # information now (the user, 2026-11-05); the event invoice's 1.04 EXTRA is not.
    assert [r[1] for r in t["r"]] == ["@w_invoice"]
    assert all(r[-1] is False for r in t["r"])


def test_a_line_that_cannot_be_read_keeps_its_words():
    t = table(["[ok] @w_add:Penne Pollo*3 > split: Guest 1, Guest 2 — already on Guest 1, "
               "Guest 2's card(s) (added by the run this one resumes) (3.0s)"],
              "Per-member split check — Penne Pollo split 2 ways")
    assert "already on Guest 1" in " ".join(map(str, t["r"][1])) and t["r"][1][-1] is None


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


# ── pre-payment and post-payment together (step_tables.prepost) ───────────────────────
# The user, 2026-11-04, with the sample pre-order report: "pre payment and post payment
# together". Numbers from run 1107222a: the host paid 273.57, the participant 108.50; the
# host's invoice and the event invoice print them again as their Pre-Order block.
HOST_VAT = [["VAT 3%", "57.00", "1.11", "58.11", "58.11", "1.69", True],
            ["VAT 14%", "22.38", "3.13", "25.51", "25.51", "3.13", True],
            ["VAT 17%", "164.27", "25.68", "189.95", "189.95", "27.60", True]]
EVENT_VAT = [["VAT 3%", "103.65", "2.51", "106.16", "106.16", "3.09", True],
             ["VAT 14%", "39.88", "5.58", "45.46", "45.46", "5.58", True],
             ["VAT 17%", "200.69", "29.76", "230.45", "230.45", "33.48", True]]
TOTALS_HEAD = ["Check", "How", "Calculated", "Displayed", "Diff"]
VAT_HEAD = ["Rate", "Excl. Tax", "VAT", "ATI", "Excl. Tax + VAT", "Most VAT at the rate"]


def _invoice(title, total, vat, how):
    return [ic.calc_note(f"{title} — Pre-Order: totals", TOTALS_HEAD,
                         [["grand total", how, total, total, "+0.00", True]]),
            ic.calc_note(f"{title} — Pre-Order: VAT", VAT_HEAD, vat),
            ic.calc_note(f"{title} — On Premises: totals", TOTALS_HEAD,
                         [["grand total", "VAT rows", "48.02", "48.02", "+0.00", True]])]


def _run(cart=None):
    host = ["[ok] @pay_stripe — tapped 'Pay €273.57' (bill €273.57) (11.3s)"]
    if cart:
        host = [ic.calc_note("Cart before payment (host)", ["Line", "Amount"], cart)] + host
    return [
        {"num": "1", "name": "Host (C-App): book for 7, pre-order, pay", "status": "PASS",
         "reasons": host},
        {"num": "2", "name": "Participant (C-App, own iPhone): accept, pre-order, pay",
         "status": "PASS",
         "reasons": ["[ok] @pay_stripe — tapped 'Pay €108.50' (bill €108.5) (12.5s)"]},
        {"num": "19", "name": "Waiter: the event invoice", "status": "PASS",
         "reasons": _invoice("Event invoice (iPad)", "382.07", EVENT_VAT,
                             "VAT rows 106.16 + 45.46 + 230.45")},
        {"num": "20", "name": "Host (C-App): open the invoice", "status": "PASS",
         "reasons": _invoice("Invoice of the host (phone)", "273.57", HOST_VAT,
                             "VAT rows 58.11 + 25.51 + 189.95")},
    ]


def _titles(tables):
    return [t["t"] for t in tables]


def test_the_host_row_holds_the_payment_and_the_invoices_pre_order_block():
    from automation.reporting.step_tables import prepost
    plan = prepost(_run())
    host, _drop, replaced = plan[0]
    assert _titles(host) == [
        "Pre-payment Total — Host",
        "Pre-payment VAT — Host (from the host's invoice, Pre-Order block)",
        "Post-payment Total — Host (the host's invoice, Pre-Order block)",
        "Post-payment VAT — Host (the host's invoice, Pre-Order block)",
        "ATI Sum — Host", "Pre vs Post VAT Comparison — Host", "Pre vs Post — Host"]
    by = {t["t"]: t for t in host}
    assert by["Pre vs Post — Host"]["r"] == [["€273.57", "€273.57", "+0.00", True]]
    assert by["ATI Sum — Host"]["r"][-3:] == [["Calculated Sum", "€273.57", None],
                                             ["Displayed Total", "€273.57", None],
                                             ["Diff", "+0.00", True]]
    assert by["Post-payment VAT — Host (the host's invoice, Pre-Order block)"]["r"][0] == \
        ["VAT 3%", "€57.00", "€1.11", "€58.11", "€58.11", "€1.69", True]
    # This run never read the cart: the pre-order's VAT is the invoice's Pre-Order block
    # (the user, 2026-11-05), and comparing it with itself is no check.
    pre = by["Pre-payment VAT — Host (from the host's invoice, Pre-Order block)"]
    assert pre["r"][0] == ["VAT 3%", "€1.11", None]
    cmp_ = by["Pre vs Post VAT Comparison — Host"]
    assert cmp_["h"][1] == "Pre VAT (host's invoice)" and all(r[-1] is None for r in cmp_["r"])
    assert "Pre-order payment" in replaced
    # The block moved: the invoice row keeps only its On Premises tables.
    assert plan[3][1] == {"Invoice of the host (phone) — Pre-Order: totals",
                          "Invoice of the host (phone) — Pre-Order: VAT"}


def test_every_diners_pre_payments_are_set_against_the_event_invoice():
    from automation.reporting.step_tables import prepost
    plan = prepost(_run())
    part = plan[1][0]
    by = {t["t"]: t for t in part}
    assert by["Pre-payments of every diner"]["r"] == [
        ["Host", "€273.57", None], ["Participant", "€108.50", None], ["Sum", "€382.07", None]]
    assert by["Pre vs Post — every diner's pre-order"]["r"] == [
        ["€382.07", "€382.07", "+0.00", True]]
    assert plan[2][1] == {"Event invoice (iPad) — Pre-Order: totals",
                          "Event invoice (iPad) — Pre-Order: VAT"}


def test_the_carts_vat_is_compared_rate_by_rate_and_a_difference_is_red():
    from automation.reporting.step_tables import prepost
    cart = [["VAT 3%", "1.11", None], ["VAT 14%", "3.13", None], ["VAT 17%", "25.70", None],
            ["Total", "273.57", None]]
    by = {t["t"]: t for t in prepost(_run(cart))[0][0]}
    assert by["Pre-payment Total — Host"]["r"][0] == ["Cart Total (displayed)", "€273.57", None]
    assert by["Pre-payment VAT — Host"]["r"][-1] == ["Total VAT", "€29.94", None]
    assert by["Pre vs Post VAT Comparison — Host"]["r"] == [
        ["VAT 3%", "€1.11", "€1.11", "+0.00", True],
        ["VAT 14%", "€3.13", "€3.13", "+0.00", True],
        ["VAT 17%", "€25.70", "€25.68", "-0.02", False]]


def test_the_report_draws_them_in_the_pre_order_row_and_not_twice():
    html = sr.render_run_report(_run(), title="T")
    host_row = html.index("Pre-payment Total (host)")
    assert host_row < html.index("Post-payment VAT (host)") < html.index("2. Participant")
    assert html.count("Invoice VAT (pre order)") == 0  # moved, not drawn again
    # ONE invoice (the user, 2026-11-05: "keep only one whole invoice"): the event's,
    # in its own row; the host's own invoice (all its sums matched) draws no tables.
    assert html.count("Event Invoice vs Stored Payments") == 1
    assert "Host Invoice Totals" not in html


def test_a_pre_order_without_its_invoice_shows_only_what_was_paid():
    from automation.reporting.step_tables import prepost
    host = prepost(_run()[:1])[0][0]
    assert _titles(host) == ["Pre-payment Total — Host", "Pre-payment VAT — Host"]


def test_a_segment_without_sums_shows_the_steps_it_checked():
    # The user, 2026-11-05: "please must fill all details without skipping".
    from automation.reporting.step_tables import steps_table
    notes = ["[ok] session ready on 6767DB23 (0.0s)",
             "[ok] @open_event — opened the 11:44 booking (12.0s)",
             "[ok] @w_qty:Guest 5/Pizza Salamees =6 — Guest 5's Pizza Salamees: 5 → 6 (63.6s)",
             "[ok] @kitchen_ready — ticket 5626: selected 4 item(s) → Ready (41.4s)"]
    t = steps_table(notes)
    assert t["t"] == "Steps checked" and t["h"] == ["Step", "What the app showed"]
    assert t["r"] == [["Open the event", "opened the 11:44 booking", True],
                      ["Quantity: Guest 5/Pizza Salamees =6", "Guest 5's Pizza Salamees: 5 → 6", True],
                      ["Kitchen: mark ready", "ticket 5626: selected 4 item(s) → Ready", True]]
    rec = {"num": "4", "name": "Waiter: quantities", "status": "PASS", "reasons": notes}
    html = sr.render_run_report([rec], title="T")
    assert "Steps checked" in html and "Guest 5&#x27;s Pizza Salamees: 5 → 6" in html \
        or "Guest 5's Pizza Salamees: 5 → 6" in html


def test_a_steps_row_leaves_out_the_apps_id_lists():
    from automation.reporting.step_tables import steps_table
    t = steps_table(["[ok] @kitchen_ready — ticket 5626: selected 3 item(s) (PennePollo, "
                     "PizzaSalamees, mushroombiryani) → Ready → Close Order (41.4s)"])
    assert t["r"][0][1] == "ticket 5626: selected 3 item(s) → Ready → Close Order"
