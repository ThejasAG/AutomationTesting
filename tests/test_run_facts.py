"""What a run really did, for the Reports page (automation/reporting/run_facts.py).

The user, 2026-11-05, on the exported report of the END TO END run: "most of the areas is
not filled" -- an empty Detail column, and an AI summary that read 'took 0.0 seconds,
device n/a' from a resumed run's row (duration None, device 'C:D03927 B:6767DB')."""
from automation.reporting import run_facts
from automation.reporting import scenario_report as sr
from automation.scenarios import invoice_check as ic


def seg(num, name, reasons, status="PASS", secs=10.0, error=""):
    return {"scenario_num": num, "scenario_name": name, "status": status,
            "reasons": reasons, "launch_time": secs, "error": error}


HOST = seg("1", "Host (C-App): book, pre-order, pay", [
    "[ok] session ready on D03927A2 (0.0s)",
    "[ok] open app — activated the app (5.3s)",
    "[ok] @c_add:Penne Pollo — added 1 × Penne Pollo (12.4s)",
    "[ok] @pay_stripe — tapped 'Pay €273.57' (bill €273.57) (36.7s)",
], secs=415.2)
WAITER = seg("19", "Waiter: the event invoice, close the table", [
    "[ok] carried over — passed in run 981e90eb; not run again (resumed from segment 20)",
    "[ok] session ready on 6767DB23 (0.0s)",
    "[ok] @w_invoice — event invoice: Total 404.56 € (44.6s)",
    ic.calc_note("Event invoice (iPad) — On Premises: totals", ["Check", "Calculated", "Displayed"],
                 [["grand total", "404.56", "404.56", True]]),
    "[mismatch] @w_invoice — On Premises payments: 310.11 = 310.11 against Total 404.56",
    "    [APP CRASH] the business app quit; relaunched it",
], secs=280.0)


def test_the_duration_is_the_segments_own_time_when_the_row_has_less():
    assert run_facts.segment_seconds([HOST, WAITER]) == 695.2
    assert run_facts.effective_duration_ms(None, [HOST, WAITER]) == 695200
    assert run_facts.effective_duration_ms(900000, [HOST, WAITER]) == 900000


def test_the_devices_are_named_per_role(monkeypatch):
    monkeypatch.setattr(run_facts, "_sim_names",
                        lambda: {"D03927A2": "iPhone 17 Pro", "6767DB23": "iPad Pro 11-inch (M5)"})
    kitchen = seg("7", "Kitchen: mark every item ready", ["[ok] session ready on 6767DB23 (0.0s)"])
    assert run_facts.devices([HOST, WAITER, kitchen]) == (
        "host: iPhone 17 Pro (D03927A2) · waiter + kitchen: iPad Pro 11-inch (M5) (6767DB23)")
    assert run_facts.devices([], "C:D03927 B:6767DB") == "C:D03927 B:6767DB"


def test_each_segment_says_what_it_did_and_what_did_not_add_up():
    assert run_facts.segment_detail(HOST) == "3 steps passed · 1 calculation checked"
    d = run_facts.segment_detail(WAITER)
    assert d.startswith("1 step passed · 1 calculation checked · 1 sum did not match: "
                        "@w_invoice — On Premises payments")
    assert d.endswith("1 app issue noted (yellow)")
    assert run_facts.carried_over([HOST, WAITER]) == 1


def test_a_failed_segment_leads_with_its_error():
    bad = seg("14", "Waiter: send", ["[ok] @open_order — opened (9s)"], status="FAIL",
              error="[FAIL] send to kitchen — no SEND")
    assert run_facts.segment_detail(bad).startswith("[FAIL] send to kitchen — no SEND (1 step")


def test_a_segment_only_carried_over_says_so():
    only = seg("2", "Participant (C-App)", ["[ok] carried over — passed in run x; not run again"])
    assert run_facts.segment_detail(only) == "passed in an earlier run of this booking (carried over)"


def test_the_rich_report_carries_the_summary_when_given():
    rec = {"num": "1", "name": "Host: pay", "status": "PASS", "reasons": HOST["reasons"]}
    html = sr.render_run_report([rec], title="T", summary="**All 21 passed.**\n\n- the host paid")
    assert ">Summary</div><p><b>All 21 passed.</b></p><p>&bull; the host paid</p>" in html
    assert "__SUMMARY__" not in sr.render_run_report([rec], title="T")
