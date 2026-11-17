"""automation/formulas.py: every money formula, exact to the cent (the user, 2026-11-17)."""
import re
from pathlib import Path

from automation import formulas as F


def test_a_cent_is_a_difference_float_noise_is_not():
    assert F.same(0.1 + 0.2, 0.30) is True
    assert F.same(10.20, 10.21) is False
    assert F.same(None, 1.0) is None
    assert F.diff(10.20, 10.21) == 0.01 and str(F.diff(1.0, 1.0)) == "0.0"


def test_vat_is_taken_out_of_the_price():
    assert round(F.vat_in(125.56, 14), 2) == 15.42
    assert F.vat_within_rate(15.42, 125.56, 14)
    assert not F.vat_within_rate(15.43, 125.56, 14)       # the real 1-cent invoice bug
    assert F.vat_row_adds(110.14, 15.42, 125.56)
    assert not F.vat_row_adds(110.14, 15.43, 125.56)


def test_invoice_sums():
    assert F.vat_rows_total([125.56, 20.00], tip=2.0) == 147.56
    assert F.dishes_total([10.0, 5.5], comps=[0.18], tip=1.0) == 16.32
    assert F.after_discount(50.0, 7.5) == 42.5
    assert F.payments_match(100.0, 100.0)
    assert F.payments_match(105.0, 100.0, due=5.0)          # change owed back
    assert F.payments_match(102.0, 100.0, overpaid=2.0)     # paid above on purpose
    assert not F.payments_match(100.01, 100.0)


def test_coupon_tip_change():
    assert F.after_offer(100.0, pct=31.5) == 68.5
    assert F.after_offer(10.0, fixed=2.0) == 8.0
    assert F.after_offer(10.0) is None
    assert F.with_tip(40.51, 3) == 43.51
    assert F.change(50.0, 43.51) == 6.49


def test_split_is_the_fair_shares():
    assert F.fair_shares(10.20, 2) == [5.10, 5.10]
    assert F.fair_shares(10.21, 2) == [5.11, 5.10]
    assert F.shares_ok([5.10, 5.10], 10.20)
    assert not F.shares_ok([5.10, 5.20], 10.20)
    assert not F.shares_ok([], 10.20)
    assert F.even_share(10.20, 2) == 5.10


def test_void_and_comp():
    assert F.void_expected(33.99, 3, 2) == 22.66
    assert F.void_value(33.99, 22.66) == 11.33
    assert F.comp_whole_line(10.0, 15) == (1.5, 8.5)
    unit, held, whole, comp, after = F.comp_units(6.10, 4.88, 1, 15)
    assert round(unit, 2) == 1.22 and whole and round(held) == 5
    assert comp == 0.18 and after == 4.88
    assert F.comp_value(6.10, 4.88, 15, whole_line=False) == 0.18
    assert F.comp_value(10.0, 8.5, 15, whole_line=True) == 1.5


def test_grand_total_around_a_split_void_comp():
    assert F.grand_total_expected("split", 404.56, None) == 404.56
    assert F.grand_total_expected("void", 404.56, 17.51) == 387.05
    assert F.grand_total_expected("comp", 404.56, 0.18) == 404.38
    assert F.grand_total_expected("void", None, 17.51) is None


def test_the_money_files_keep_no_tolerance_of_their_own():
    """The cent tolerance lives only in formulas.py."""
    files = ["automation/scenarios/invoice_check.py", "automation/reporting/step_tables.py",
             "automation/reporting/invoice_section.py", "automation/reporting/scenario_report.py",
             "automation/scenarios/cross_app_flows.py", "automation/intelligence/bill_validator.py",
             "automation/intelligence/scenario_runner.py"]
    for f in files:
        code = "\n".join(line.split("#", 1)[0] for line in Path(f).read_text().splitlines())
        assert not re.search(r"\b0\.005\b", code), f
        assert not re.search(r"(abs\([^)]*\)|diff) ?[<>]=? ?0\.01\b", code), f
