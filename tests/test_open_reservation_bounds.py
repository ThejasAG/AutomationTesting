"""@open_reservation must fail fast and say why, never hang the segment.

From a live cross-app run, two segments failed and they were ONE defect:

  seg 2  @open_reservation — found 'RoopaDcardReserved' but could not open it (WDA click)
  seg 4  @open_order       — timed out after 240s (step hung; aborting segment)

Segment 4 is a CASCADE, not a second bug: @open_reservation failing meant
@assign_table and sendItemsBtn never ran, so nothing reached the kitchen, so no
InProgress card could exist for @open_order to find. The screen dump proves it —
only RoopaDcardReserved and NooluNagacardExpired were on the board.

Two things made that hard to read, and both are fixed here:

* _scroll_into_view was the ONLY unbounded Appium path @open_reservation can
  take (_swipe_element and appium_click were already bounded). A wedged WDA
  parks in find_elements/rect on this app's huge tree with no ceiling, so the
  step burned its whole 240s and was killed by the watchdog — reported as
  "step hung", which names the symptom and hides the stuck resolve.
* "(WDA click)" cannot distinguish a 30s timeout from a stale element from a
  label that stopped resolving. Those need different fixes.
"""
import time

import pytest

from automation.scenarios import cross_app_flows as F


def _runner():
    return object.__new__(F.FlowRunner)


class _Rect(dict):
    pass


def _driver(*, hang=False, window=None, rect=None, found=True):
    """A driver that can HANG, which is the condition under test."""
    class El:
        id = "el-1"
        @property
        def rect(self):
            if hang:
                time.sleep(60)          # longer than any bound under test
            return rect or {"x": 0, "y": 1216, "width": 492, "height": 144}
        def click(self):
            if hang:
                time.sleep(60)

    class D:
        def get_window_size(self):
            if hang:
                time.sleep(60)
            return window or {"width": 1210, "height": 834}
        def find_elements(self, by, sel):
            if hang:
                time.sleep(60)
            return [El()] if found else []
        def execute_script(self, *a, **k):
            if hang:
                time.sleep(60)
            return True

    class R:
        d = D()
    return R()


# ── _scroll_into_view must be bounded ───────────────────────────────────────

def test_01_a_hung_driver_does_not_hang_the_step():
    """The 240s "step hung" failure. A wedged WDA must not consume the ceiling."""
    fr, r = _runner(), _driver(hang=True)
    t0 = time.time()
    out = fr._scroll_into_view(r, "RoopaDcardReserved", tries=2)
    elapsed = time.time() - t0
    assert out is False
    assert elapsed < 45, (
        f"_scroll_into_view took {elapsed:.0f}s against a hung driver — "
        "unbounded, so the step burns its whole 240s ceiling")


def test_02_a_card_already_on_screen_returns_true_without_swiping():
    """The normal path must not regress: no gesture when nothing is needed."""
    fr = _runner()
    r = _driver(rect={"x": 0, "y": 300, "width": 492, "height": 144})
    assert fr._scroll_into_view(r, "RoopaDcardReserved", tries=3) is True


def test_03_a_missing_label_returns_false_immediately():
    fr, r = _runner(), _driver(found=False)
    t0 = time.time()
    assert fr._scroll_into_view(r, "GoneCard", tries=4) is False
    assert time.time() - t0 < 10, "a missing label should not retry slowly"


def test_04_an_offscreen_card_is_scrolled_then_reported():
    """Below the fold (y=1216 on an 834-tall screen) — the measured case."""
    fr, r = _runner(), _driver(rect={"x": 0, "y": 1216, "width": 492, "height": 144})
    # The fake driver never moves the card, so this exhausts its tries and
    # returns False — the point is that it TERMINATES rather than hanging.
    t0 = time.time()
    assert fr._scroll_into_view(r, "RoopaDcardReserved", tries=2) is False
    assert time.time() - t0 < 30


# ── the failure note must say WHY ───────────────────────────────────────────

def test_05_the_click_reason_is_recorded_not_swallowed():
    """'(WDA click)' told nobody anything. The reason must survive."""
    import inspect
    full = inspect.getsource(F.FlowRunner._open_reservation)
    assert "_last_click_error" in full, "the click failure reason is still swallowed"

    # Strip comments before asserting: the replacement is EXPLAINED in a comment
    # that quotes the old "(WDA click)" string, and a naive grep matches that
    # prose rather than the code — the same trap a source-text check hit earlier
    # in this codebase.
    code = "\n".join(l for l in full.splitlines()
                     if not l.lstrip().startswith("#"))
    assert 'could not open it "\n' not in code
    # The bare, reasonless note is gone; what remains is a fallback used only
    # when no reason was captured.
    assert '(WDA click)"' not in code, "the uninformative note is still emitted"


def test_06_a_timeout_and_a_missing_element_are_distinguishable():
    """A wedged WDA and a label that stopped resolving need different fixes, so
    the note must not report them identically."""
    import inspect
    src = inspect.getsource(F.FlowRunner._open_reservation)
    assert "no element matched that exact label" in src
    assert "wedged resolve" in src


def test_07_the_bound_survives_a_thread_that_never_returns():
    """shutdown(wait=False) matters: shutdown(wait=True) blocks on the stuck
    worker and defeats the timeout entirely — the exact bug the step runner
    documents at line 3579."""
    import inspect
    src = inspect.getsource(F.FlowRunner._scroll_into_view)
    assert "shutdown(wait=False)" in src, \
        "a stuck worker will block the very timeout meant to abandon it"
