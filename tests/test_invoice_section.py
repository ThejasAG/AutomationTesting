"""The report's ONE invoice (automation/reporting/invoice_section.py), in the layout of the
user's invoice sample (2026-11-05: "keep only one whole invoice like this ... dont give
like host invoice, participant invoice, waiter invoice"). Figures of END TO END run
3b33a2fe; the participant's name is made up."""
from automation.reporting import invoice_section, scenario_report as sr
from automation.scenarios import invoice_check as ic

TOTALS = ["Check", "How", "Calculated", "Displayed", "Diff"]
VAT = ["Rate", "Excl. Tax", "VAT", "ATI", "Excl. Tax + VAT", "Most VAT at the rate"]
EVENT = "Event invoice (iPad)"
HOST = "Invoice of the host (phone)"


def _event(payments_ok=False, printed="310.11"):
    return [
        ic.calc_note(f"{EVENT} — Pre-Order: totals", TOTALS, [
            ["grand total", "VAT rows 106.16 + 45.46 + 230.45", "382.07", "382.07", "+0.00", True],
            ["dishes", "9 dishes 382.07", "382.07", "382.07", "+0.00", True],
            ["payments", "382.07 (card + cash + voucher)", "382.07", "382.07", "+0.00", True],
            ["against the payments", "273.57 + 108.50", "382.07", "382.07", "+0.00", True]]),
        ic.calc_note(f"{EVENT} — Pre-Order: VAT", VAT, [
            ["VAT 3%", "103.65", "2.51", "106.16", "106.16", "3.09", True]]),
        ic.calc_note(f"{EVENT} — On Premises: totals", TOTALS, [
            ["grand total", "VAT rows 124.68 + 77.93 + 196.95 + tip 5.00", "404.56", "404.56",
             "+0.00", True],
            ["payments", f"{printed} + 0.00 + 0.00 (card + cash + voucher), Due 0.01, "
             "overpaid on purpose 10.01", printed, "404.56", "-94.45", payments_ok],
            ["against the payments", "84.84 + 241.41 + 78.31", "404.56", "404.56", "+0.00", True],
            ["Tip against the tips given", "2.00 + 3.00", "5.00", "5.00", "+0.00", True]]),
        ic.calc_note(f"{EVENT} — On Premises: VAT", VAT, [
            ["VAT 17%", "173.28", "23.67", "196.95", "196.95", "28.62", True]]),
        ic.calc_note(f"{EVENT} — whole invoice", ["Block", "Total"],
                     [["Pre-Order", "382.07", None], ["On Premises", "404.56", None]]),
        "[ok] @close_table — tapped Close Table; the order closed (6.8s)",
    ]


def _host(dishes_ok=True):
    return [
        ic.calc_note(f"{HOST} — Pre-Order: totals", TOTALS, [
            ["grand total", "VAT rows 58.11", "273.57", "273.57", "+0.00", True],
            ["against the payments", "273.57", "273.57", "273.57", "+0.00", True]]),
        ic.calc_note(f"{HOST} — On Premises: totals", TOTALS, [
            ["grand total", "VAT rows 14.72 + tip 2.00", "84.84", "84.84", "+0.00", True],
            ["dishes", "9 dishes 83.16 - 2 comps 0.32 + tip 2.00", "84.84",
             "84.84" if dishes_ok else "85.84", "+0.00" if dishes_ok else "-1.00", dishes_ok],
            ["against the payments", "84.84", "84.84", "84.84", "+0.00", True]]),
        "[ok] @c_invoice — the invoice: Total 84.84€ (142.8s)",
    ]


def _run(event=True, **kw):
    pay = ["Bill displayed", "Charged by Stripe", "Diff"]
    waiter = ["Bill", "Tendered", "Change", "Confirm Payment"]
    recs = [
        {"num": "1", "name": "Host (C-App): book, pre-order, pay", "status": "PASS",
         "reasons": [ic.calc_note("Pre-order payment (host)", pay, [["273.57", "273.57", "+0.00", True]])]},
        {"num": "2", "name": "Participant (C-App, own iPhone): accept, pre-order, pay",
         "status": "PASS",
         "reasons": [ic.calc_note("Pre-order payment (participant)", pay,
                                  [["108.50", "108.50", "+0.00", True]])]},
        {"num": "17", "name": "Host (C-App): pay for two guests, coupon, tip, E-Payment",
         "status": "PASS", "reasons": [
             ic.calc_note("Pay for others (host)", ["Pays for", "Total displayed"],
                          [["Guest 1, Guest 2", "92.04", None]]),
             ic.calc_note("Coupon 10% (host)", ["Total before", "Coupon", "Total after coupon",
                                                "Displayed", "Diff"],
                          [["92.04", "-10%", "82.84", "82.84", "+0.00", True]]),
             ic.calc_note("Tip 2.00 (host)", ["Total before", "Tip", "Total with tip",
                                              "Displayed", "Diff"],
                          [["82.84", "+2.00", "84.84", "84.84", "+0.00", True]]),
             ic.calc_note("E-Payment (host)", pay, [["84.84", "84.84", "+0.00", True]])]},
        {"num": "18", "name": "Waiter: participant pays for two guests; the rest by cash",
         "status": "PASS", "reasons": [
             ic.calc_note("Tip 3.00 (Asha Rao)", ["Total before", "Tip", "Total with tip",
                                                  "Displayed", "Diff"],
                          [["238.41", "+3.00", "241.41", "241.41", "+0.00", True]]),
             ic.calc_note("Pay for others (Asha Rao)", ["Pays for", "Total displayed"],
                          [["Guest 3, Guest 4", "241.41", None]]),
             ic.calc_note("Payment by E-Payment (Asha Rao)", waiter,
                          [["241.41", "241.42", "0.01", "settled", True]]),
             ic.calc_note("Payment by Cash (Guest 5)", waiter,
                          [["78.31", "88.31", "10.00", "settled", True]])]},
        {"num": "20", "name": "Host (C-App): review the restaurant, open the invoice",
         "status": "PASS", "reasons": _host(kw.pop("dishes_ok", True))},
    ]
    if event:
        recs.insert(4, {"num": "19", "name": "Waiter: the event invoice, close the table",
                        "status": "PASS", "reasons": _event(**kw)})
    return recs


def test_the_event_invoice_is_the_one_invoice_and_every_invoice_leaves_its_row():
    out = invoice_section.build(_run())
    assert out["at"] == 4 and out["section"]["title"] == "Event Invoice vs Stored Payments"
    assert {t for t in out["drop"][4] if "Event invoice" in t} == {
        f"{EVENT} — Pre-Order: totals", f"{EVENT} — Pre-Order: VAT",
        f"{EVENT} — On Premises: totals", f"{EVENT} — On Premises: VAT",
        f"{EVENT} — whole invoice"}
    assert "Outstanding Balance Check" in out["drop"][4]   # it is the invoice's Outstanding
    assert out["drop"][5] == {f"{HOST} — Pre-Order: totals", f"{HOST} — On Premises: totals"}


def test_it_holds_the_samples_checks_in_the_samples_order():
    html = sr._render_invoice(invoice_section.build(_run())["section"])
    order = ["Event Invoice vs Stored Payments", "Invoice Total vs Stored Total:",
             "Paid for (Invoice):", "VAT (Invoice vs Calculated):", "Invoice lines (Invoice vs Stored):",
             "Invoice adds up (VAT rows + tip):", "<b>Tip:</b>", "<b>Outstanding:</b>",
             "<b>Invoice vs Stored Payments:</b>"]
    at = [html.index(x) for x in order]
    assert at == sorted(at)
    assert ">Rate<" in html and ">Invoice Excl<" in html and ">Calc ATI<" in html
    assert ">Item<" in html and ">Qty<" in html and ">Field<" in html


def test_its_total_is_set_against_what_the_run_paid():
    s = invoice_section.build(_run())["section"]
    assert s["total"] == {"invoice": 786.63, "stored": 786.63, "ok": True}
    assert s["lines"] == [["Pre-Order dishes", "9", 382.07, 382.07, True],
                          ["On Premises dishes + tip €5.00", "—", 404.56, 404.56, True]]
    assert s["adds"] == {"calc": 786.63, "total": 786.63, "ok": True}
    assert s["paid_for"] == [("Guest 1, Guest 2", "Host"),
                             ("Guest 3, Guest 4", "Asha Rao (participant)")]
    html = sr._render_invoice(s)
    assert "Expected 4: Guest 1, Guest 2 (by Host), Guest 3, Guest 4 (by Asha Rao, participant)" in html


def test_payment_lines_short_of_the_total_are_information_and_more_is_red():
    s = invoice_section.build(_run())["section"]
    assert s["printed"] == {"total": 692.18, "of": 786.63, "over": False, "short": True}
    html = sr._render_invoice(s)
    assert "not printed: &euro;94.45 booked on guests" in html
    assert "FAIL" not in html and "&#10007;" not in html
    over = sr._render_invoice(invoice_section.build(_run(printed="410.00"))["section"])
    assert '<b>Paid for (Invoice):</b> <span style="color:#e74c3c' in over


def test_the_due_is_the_change_owed_back_on_the_e_payment():
    o = invoice_section.build(_run())["section"]["outstanding"]
    assert o == {"due": 0.01, "explained": True, "closed": True, "ok": True}
    html = sr._render_invoice(invoice_section.build(_run())["section"])
    assert "Invoice = &euro;0.01 Due (change owed back) | Stored = &euro;0.00 | the table closed" in html


def test_a_diners_own_invoice_with_a_red_sum_keeps_its_tables():
    assert 5 in invoice_section.build(_run())["drop"]
    bad = invoice_section.build(_run(dishes_ok=False))
    assert 5 not in bad["drop"]             # not hidden: it stays where it was read
    html = sr.render_run_report(_run(dishes_ok=False), title="T")
    assert "Host Invoice Totals (on premises)" in html


def test_without_an_event_invoice_the_diners_own_one_is_the_invoice():
    out = invoice_section.build(_run(event=False))
    assert out["section"]["title"] == "Host's Invoice vs Stored Payments"
    assert out["at"] == 4


def test_the_report_draws_one_invoice_and_no_invoice_tables():
    html = sr.render_run_report(_run(), title="T")
    assert html.count("Event Invoice vs Stored Payments") == 1
    for gone in ("Event Invoice Totals", "Event Invoice VAT", "Host Invoice", "Invoice Summary",
                 "Outstanding Balance Check"):
        assert gone not in html, gone
    # The host's invoice segment has no sums of its own left: it lists its steps, last.
    assert html.index("Passed — no calculation") < html.index("20. Host")
