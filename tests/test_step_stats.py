"""Step-level pass/fail accounting.

Failing at step 7 of 8 and failing at step 1 both reported just "failed". These
numbers come from ScenarioResult.reasons, which already tags every step [ok]/[FAIL].
"""
from automation.reports.step_stats import step_stats

# Shape of a real failing run (waiter demo, 2026-09-02).
REAL = [
    "[ok] click addNewEvent",
    "[ok] @wait_form — form open",
    "[ok] type roopa in firstName",
    "    · @first_time_slot — skipped 2 slot(s) covered by the LogBox toast",
    "[ok] @first_time_slot — selected slot '17:15Btn'",
    "[SLOW LOAD] @wait_form — completed after 43.4s",
    "[FAIL] @save_appointment — tapped Save 3x and the form is still open",
    "[where] failed at step: '@save_appointment (step 11/11)'",
]


def test_only_step_lines_are_counted():
    """[where], [SLOW LOAD] and indented notes are commentary, not steps."""
    s = step_stats(REAL)
    assert (s.passed, s.failed, s.total) == (4, 1, 5)
    assert s.pass_pct == 80.0 and s.fail_pct == 20.0


def test_summary_reads_as_a_report_cell():
    assert step_stats(REAL).summary() == "4/5 steps · 80% passed"


def test_all_passed():
    s = step_stats(["[ok] a", "[ok] b"])
    assert s.pass_pct == 100.0 and s.summary() == "2/2 steps · 100% passed"


def test_all_failed():
    assert step_stats(["[FAIL] a"]).pass_pct == 0.0


def test_no_steps_is_not_zero_percent():
    """'nothing ran' and 'everything failed' are different failures — do not
    report the first as 0%."""
    s = step_stats([])
    assert s.total == 0 and s.pass_pct is None and s.fail_pct is None
    assert s.summary() == "no steps recorded"


def test_none_reasons_is_safe():
    assert step_stats(None).total == 0
