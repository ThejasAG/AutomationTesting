"""Typing must be verified, not assumed.

Two measured failure modes on this app, both of which look downstream like "bad
credentials" rather than "the text never arrived":

  * noReset leaves the previous session's value in the field and send_keys APPENDS —
    producing 'emp2A@xorstack.rstack.comemp2A@xo@xorst…' and "Please enter valid email
    address" from an app whose credentials were perfectly fine.
  * RN TextInput drops characters mid-burst: send_keys('emp2A@…') was measured leaving
    'emA@…' — the 'p' and '2' swallowed.
"""
import pytest

from automation.intelligence.scenario_runner import ScenarioRunner


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    import automation.intelligence.scenario_runner as SR
    monkeypatch.setattr(SR.time, "sleep", lambda *_: None)


class Field:
    """A text input that can misbehave the way this app's inputs do.

    flaky_for : how many BURST sends drop characters before it settles. The
                character-by-character fallback still lands, which is what makes
                escalation the fix.
    permanent : drops characters in BOTH modes — an input that never takes the text.
    readable  : False means get_attribute raises (some inputs expose no value at all).
    """

    def __init__(self, initial="", flaky_for=0, permanent=False, readable=True, drop="p2A"):
        self.value = initial
        self.flaky_for = flaky_for
        self.permanent = permanent
        self.readable = readable
        self.drop = drop
        self.bursts = 0

    def click(self):
        pass

    def clear(self):
        self.value = ""

    def _mangle(self, text):
        return "".join(c for c in text if c not in self.drop)

    def send_keys(self, text):
        if len(text) == 1:                       # char-by-char
            self.value += self._mangle(text) if self.permanent else text
            return
        self.bursts += 1
        drops = self.permanent or self.bursts <= self.flaky_for
        self.value += self._mangle(text) if drops else text

    def get_attribute(self, _name):
        if not self.readable:
            raise RuntimeError("this input exposes no value")
        return self.value


def fill(field, text="emp2A@xorstack.com"):
    r = object.__new__(ScenarioRunner)
    return r._fill(field, text), field


def test_typing_goes_one_character_at_a_time():
    """Never a burst. A burst drops characters on this app's RN inputs AND reads back
    as correct, so the retry never fires — the field displayed 'eorstack.com' while
    get_attribute() reported the full address."""
    (ok, got), f = fill(Field())
    assert ok and got == "emp2A@xorstack.com"
    assert f.bursts == 0


def test_a_stale_value_is_cleared_instead_of_appended_to():
    """The exact bug: noReset + send_keys = concatenation, and every retry made it worse."""
    (ok, got), f = fill(Field(initial="emp2A@xorstack.com"))
    assert ok and got == "emp2A@xorstack.com"
    assert "comemp" not in got


def test_an_input_that_mangles_bursts_is_unaffected():
    """flaky_for only mangles BURST sends. Since we never burst, such an input now
    types cleanly on the first attempt instead of needing two retries."""
    (ok, got), f = fill(Field(flaky_for=1))
    assert ok and got == "emp2A@xorstack.com"
    assert f.bursts == 0


def test_it_escalates_to_one_character_at_a_time():
    (ok, got), f = fill(Field(flaky_for=2))
    assert ok and got == "emp2A@xorstack.com"


def test_a_field_that_stays_mangled_fails_the_step():
    """The observed failure: the field holds SOMETHING, but not what was typed
    ('emA@…', or the concatenated 'emp2A@xorstack.rstack.comemp2A@…'). Better a loud
    failure here than a mangled value submitted as a credential."""
    (ok, got), f = fill(Field(permanent=True))
    assert ok is False
    assert got and got != "emp2A@xorstack.com"


def test_a_field_reporting_empty_is_trusted_not_failed():
    """KNOWN LIMIT. An empty read-back is ambiguous — the input may simply not expose
    its value (secure fields, some RN inputs do this) — so it is trusted, exactly as
    cross_app_orchestrator._fill_field does. That means text which never lands at all
    is NOT caught here; only text that lands wrong is. Widening this would false-fail
    every masked field, which is the worse trade."""
    f = Field()
    f.send_keys = lambda t: None                  # swallows everything, reports empty
    r = object.__new__(ScenarioRunner)
    ok, got = r._fill(f, "emp2A@xorstack.com")
    assert ok is True and got == ""


def test_an_unreadable_field_is_assumed_typed():
    """Some inputs expose no readable value at all. Refusing to proceed there would
    break steps that work today, so an unreadable field is trusted."""
    (ok, got), f = fill(Field(readable=False))
    assert ok is True


def test_a_masked_secure_field_counts_as_a_match():
    f = Field()
    f.send_keys = lambda t: setattr(f, "value", "•" * len(t))
    r = object.__new__(ScenarioRunner)
    ok, got = r._fill(f, "2A_emp@Nylaikitchen2")
    assert ok and "•" in got


def test_the_type_step_fails_loudly_on_a_mangled_field():
    import inspect
    import automation.intelligence.scenario_runner as SR
    src = inspect.getsource(SR)
    i = src.index("typed_ok, got = self._fill(")
    tail = src[i:i + 700]
    assert "if not typed_ok:" in tail and "ok=False" in tail
