"""The keyboard a `type` step raises must be closed before the next step taps.

Measured on the iPhone 16 (393x852), Business sign-in: typing into both fields left the
keyboard covering y=561..794 and scrolled the form. The following `click clickCheckBox`
reported [ok] while the box stayed empty, Sign In stayed disabled, and the run only
noticed two steps later — 5/6 steps "passed" having achieved nothing.
"""
import pytest

from automation.intelligence.scenario_runner import ScenarioRunner


class FakeDriver:
    """Keyboard closes after `closes_after` dismissal attempts; -1 = never closes."""

    def __init__(self, keyboard=True, closes_after=1, raises=False):
        self.keyboard = keyboard
        self.closes_after = closes_after
        self.raises = raises
        self.attempts = []

    def find_elements(self, by, expr):
        if self.raises:
            raise RuntimeError("session died")
        if "Keyboard" in str(expr):
            return ["kb"] if self.keyboard else []
        return []

    def execute_script(self, name, args=None):
        self.attempts.append(("mobile", (args or {}).get("keys")))
        if self.closes_after != -1 and len(self.attempts) >= self.closes_after:
            self.keyboard = False

    def hide_keyboard(self):
        self.attempts.append(("hide_keyboard", None))
        if self.closes_after != -1 and len(self.attempts) >= self.closes_after:
            self.keyboard = False


def runner(driver):
    r = object.__new__(ScenarioRunner)
    r.d = driver
    return r


def test_no_keyboard_means_nothing_to_do():
    d = FakeDriver(keyboard=False)
    assert runner(d)._dismiss_keyboard() is False
    assert d.attempts == []                      # never gestures for no reason


def test_an_open_keyboard_is_closed_and_confirmed():
    d = FakeDriver(keyboard=True, closes_after=1)
    assert runner(d)._dismiss_keyboard() is True
    assert not d.keyboard


def test_it_uses_the_return_keys_that_actually_work_on_this_build():
    """driver.hide_keyboard() alone was measured leaving the keyboard up; the
    'mobile: hideKeyboard' call with return-style keys is what closes it."""
    d = FakeDriver(keyboard=True, closes_after=1)
    runner(d)._dismiss_keyboard()
    kind, keys = d.attempts[0]
    assert kind == "mobile" and "Done" in keys and "return" in keys


def test_it_falls_back_to_hide_keyboard_when_the_first_attempt_fails():
    d = FakeDriver(keyboard=True, closes_after=2)
    assert runner(d)._dismiss_keyboard() is True
    assert [a[0] for a in d.attempts] == ["mobile", "hide_keyboard"]


def test_a_keyboard_that_refuses_to_close_reports_false_and_stops_trying():
    d = FakeDriver(keyboard=True, closes_after=-1)
    assert runner(d)._dismiss_keyboard() is False
    assert len(d.attempts) == 2                  # both strategies, then give up


def test_a_dead_session_never_raises_out_of_a_step_that_already_typed():
    d = FakeDriver(raises=True)
    assert runner(d)._dismiss_keyboard() is False


def test_the_type_step_calls_it():
    """Pins the fix at the source: a keyboard left up breaks whatever step is next,
    so it must be closed by the step that raised it, not by a step authors remember."""
    import inspect
    import automation.intelligence.scenario_runner as SR
    src = inspect.getsource(SR)
    i = src.index("typed_ok, got = self._fill(")
    assert "_dismiss_keyboard()" in src[i:i + 400]
