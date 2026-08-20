"""Guard: a MULTI-word step must never resolve on ONE of its words.

Real failure this encodes: the step `click book now` matched `preOrderBooking`,
because "book" is a substring of preOrder-BOOK-ing. The run silently chose
PRE-ORDER, and the next step (`click order later`) then hung for the full 150s
timeout on a screen the flow was never supposed to reach. A wrong tap is worse
than no tap — it changes app state, so the failure surfaces somewhere unrelated
and the report blames the wrong step.

Run: PYTHONPATH=. .venv/bin/python automation/intelligence/test_no_partial_guess.py
"""
from automation.intelligence.scenario_runner import ScenarioRunner, _locator_words


class FakeEl:
    def __init__(self, name): self.name = name
    def get_attribute(self, a): return self.name if a in ("name", "label") else ""
    def is_displayed(self): return True


class FakeDriver:
    """Serves elements whose `name` CONTAINS the predicate's quoted keyword."""
    def __init__(self, names): self.names = names

    def find_elements(self, _by, pred):
        import re
        kws = re.findall(r'"([^"]+)"', pred or "")
        out = []
        for n in self.names:
            if any(k.lower() in n.lower() for k in kws):
                out.append(FakeEl(n))
        return out


class FakeCatalog:
    def record_resolution(self, *a, **k): pass
    def mint(self, *a, **k): return None
    def lookup(self, *a, **k): return None


def _runner(names):
    r = ScenarioRunner.__new__(ScenarioRunner)      # no __init__: no real device
    r.d = FakeDriver(names)
    r.learned = None
    r.bid = "test"
    r._screen = "test"
    r._last_ambiguous = ""
    r.catalog = FakeCatalog()
    return r


def test_multiword_step_does_not_match_on_one_word():
    """'book now' must NOT resolve to preOrderBooking off the word 'book' alone."""
    r = _runner(["preOrderBooking", "orderLater", "favProduct"])
    m = r._resolve(_locator_words("click book now"), step="click book now")
    assert not m, (
        f"resolved to {getattr(m, 'text', None) or m.value!r} on a single word — "
        "this is the bug that silently chose pre-order")
    assert "partially matches" in (r._last_ambiguous or ""), \
        f"no explanation recorded; got {r._last_ambiguous!r}"


def test_single_word_step_still_resolves():
    """A one-word step gave us only that word — matching it is not a guess."""
    r = _runner(["bookAppoitment", "favProduct"])
    m = r._resolve(_locator_words("click bookAppoitment"), step="click bookAppoitment")
    assert m, "an exact single-word id must still resolve"


def test_multiword_step_resolves_when_all_words_present():
    """'order later' SHOULD match orderLater — it accounts for every word."""
    r = _runner(["preOrderBooking", "orderLater"])
    m = r._resolve(_locator_words("click order later"), step="click order later")
    assert m, "orderLater covers both 'order' and 'later' and must resolve"


if __name__ == "__main__":
    test_multiword_step_does_not_match_on_one_word()
    test_single_word_step_still_resolves()
    test_multiword_step_resolves_when_all_words_present()
    print("ok — multi-word steps refuse to resolve on a single word")
