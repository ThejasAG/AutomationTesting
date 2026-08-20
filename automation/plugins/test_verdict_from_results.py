"""Guard: an agent run is judged by what RAN, not by the exit code.

Real failure this encodes: `execute()` returned
``"passed" if process.returncode == 0 else "failed"``. pytest exits 0 when it
collects ZERO tests, so PR runs that finished in under two minutes with no
evidence directory were all recorded as green. Every agent job ever run also
wrote zero `scenario_results` rows, so those runs displayed with no steps at all.

Run: PYTHONPATH=. .venv/bin/python automation/plugins/test_verdict_from_results.py
"""
import os
import tempfile

from automation.plugins.appium_framework import _parse_junit, _verdict

SUITE = ('<testsuites><testsuite name="p" tests="{t}" failures="{f}" '
         'errors="{e}" skipped="0">{cases}</testsuite></testsuites>')


def _verdict_for(xml, returncode):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "junit.xml")
    if xml is not None:
        with open(p, "w") as fh:
            fh.write(xml)
    return _verdict(_parse_junit(p), returncode)


def test_zero_tests_is_not_a_pass():
    """THE bug: pytest collected nothing and exited 0."""
    status, detail = _verdict_for(SUITE.format(t=0, f=0, e=0, cases=""), 0)
    assert status == "failed", "a run that executed no tests must not pass"
    assert "collected" in detail.lower()


def test_missing_report_is_not_a_pass():
    """No JUnit file means we cannot show anything ran — unverifiable is not green."""
    status, _ = _verdict_for(None, 0)
    assert status == "failed"


def test_real_pass_still_passes():
    cases = "".join(f'<testcase classname="e2e.test_home" name="t{i}" time="1.0"/>'
                    for i in range(20))
    status, detail = _verdict_for(SUITE.format(t=20, f=0, e=0, cases=cases), 0)
    assert status == "passed", detail
    assert "20" in detail


def test_errors_fail():
    cases = ('<testcase classname="e2e.test_home" name="test_can_navigate_to_wallet" '
             'time="3.1"><error message="WebDriverException"/></testcase>')
    status, _ = _verdict_for(SUITE.format(t=20, f=0, e=20, cases=cases), 1)
    assert status == "failed"


def test_green_tests_but_nonzero_exit_fails():
    """A crash after the last test still means the run cannot be trusted."""
    status, _ = _verdict_for(SUITE.format(t=3, f=0, e=0, cases=""), 2)
    assert status == "failed"


def test_cases_are_extracted_for_the_steps_view():
    """Per-test rows are what the dashboard renders as a run's steps."""
    cases = ('<testcase classname="e2e.test_payment" name="test_can_reach_cart" time="2.5"/>'
             '<testcase classname="e2e.test_payment" name="test_saved_card" time="1.0">'
             '<failure message="assert False"/></testcase>')
    d = tempfile.mkdtemp()
    p = os.path.join(d, "junit.xml")
    with open(p, "w") as fh:
        fh.write(SUITE.format(t=2, f=1, e=0, cases=cases))
    s = _parse_junit(p)
    assert len(s["cases"]) == 2, s
    assert s["cases"][0]["name"] == "e2e.test_payment::test_can_reach_cart"
    assert s["cases"][0]["status"] == "PASS"
    assert s["cases"][1]["status"] == "FAIL"
    assert "assert False" in s["cases"][1]["message"]


if __name__ == "__main__":
    test_zero_tests_is_not_a_pass()
    test_missing_report_is_not_a_pass()
    test_real_pass_still_passes()
    test_errors_fail()
    test_green_tests_but_nonzero_exit_fails()
    test_cases_are_extracted_for_the_steps_view()
    print("ok — verdict comes from executed tests, and per-test cases are extracted")
