"""Guard: `capture <label> as <name>` + `verify calculation <name> ± n = <name>`.

The XP scenarios were written as:
    capture XP balance as before
    ... book an event ...
    capture XP balance as after
    verify calculation before + 20 = after

but no capture intent existed. The line fell through to the TAP resolver, matched
nothing meaningful, and the calculation that depended on it could never have real
values — so three scenarios could not do what they claimed.

Run: PYTHONPATH=. .venv/bin/python automation/intelligence/test_capture_calculation.py
"""
from automation.intelligence.scenario_runner import ScenarioRunner


def _runner(numbers):
    """numbers: [(value, surrounding_text)] as _screen_numbers() would return."""
    r = ScenarioRunner.__new__(ScenarioRunner)
    r._captured = {}
    r._screen_numbers = lambda: numbers
    return r


def test_capture_stores_the_labelled_number():
    r = _runner([(120.0, "XP balance 120"), (7.0, "7 upcoming")])
    res = r._capture_number("capture XP balance as before")
    assert res.ok, res.detail
    assert r._captured["before"] == 120.0, r._captured


def test_capture_picks_the_number_matching_the_label():
    """Not merely the first number on screen."""
    r = _runner([(7.0, "7 upcoming"), (120.0, "XP balance 120")])
    r._capture_number("capture XP balance as before")
    assert r._captured["before"] == 120.0, r._captured


def test_before_plus_20_equals_after_passes():
    r = _runner([(120.0, "XP balance 120")])
    r._capture_number("capture XP balance as before")
    r._screen_numbers = lambda: [(140.0, "XP balance 140")]
    r._capture_number("capture XP balance as after")
    res = r._verify_calculation("verify calculation before + 20 = after")
    assert res is not None and res.ok, getattr(res, "detail", "no result")


def test_wrong_delta_fails():
    """The whole point: a WRONG award must fail, not quietly pass."""
    r = _runner([(120.0, "XP balance 120")])
    r._capture_number("capture XP balance as before")
    r._screen_numbers = lambda: [(125.0, "XP balance 125")]
    r._capture_number("capture XP balance as after")
    res = r._verify_calculation("verify calculation before + 20 = after")
    assert res is not None and not res.ok, "a 5-point award must not satisfy +20"


def test_cancellation_deducts_30():
    r = _runner([(200.0, "XP balance 200")])
    r._capture_number("capture XP balance as before")
    r._screen_numbers = lambda: [(170.0, "XP balance 170")]
    r._capture_number("capture XP balance as after")
    res = r._verify_calculation("verify calculation before - 30 = after")
    assert res is not None and res.ok, getattr(res, "detail", "no result")


def test_missing_label_fails_loudly():
    r = _runner([(7.0, "7 upcoming")])
    res = r._capture_number("capture XP balance as before")
    assert not res.ok
    assert "could not find" in res.action.lower()


def test_malformed_capture_is_rejected():
    r = _runner([(1.0, "x")])
    res = r._capture_number("capture nonsense")
    assert not res.ok


if __name__ == "__main__":
    test_capture_stores_the_labelled_number()
    test_capture_picks_the_number_matching_the_label()
    test_before_plus_20_equals_after_passes()
    test_wrong_delta_fails()
    test_cancellation_deducts_30()
    test_missing_label_fails_loudly()
    test_malformed_capture_is_rejected()
    print("ok — capture stores numbers and before/after arithmetic is really checked")
