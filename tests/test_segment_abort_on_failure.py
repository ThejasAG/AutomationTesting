"""Guard: a failed segment must STOP the flow, not let later segments run.

Cross-app segments hand state to each other: the waiter sends the order that the
kitchen then marks Ready. When a segment fails that handoff never happened, so
every later segment acts on the wrong app state.

The failure this locks in was measured on a live staging run (PAY1, Preorder ->
settle by CASH):

    segment 1  consumer  PASS   book + preorder + pay
    segment 2  waiter    FAIL   @open_reservation never opened the reservation
    segment 3  kitchen   PASS   <-- THE LIE

Segment 3 went green because it found a STALE queued order from an earlier run
and marked it Ready. The order segment 2 was supposed to send was never sent. A
green segment after a red one is not just noise: it inverts the verdict a person
reads, and logging in as kitchen also tore down the waiter screen the segment-2
failure had to be diagnosed from.

Run: PYTHONPATH=. .venv/bin/python -m pytest tests/test_segment_abort_on_failure.py -q
"""
import inspect

from automation.scenarios.cross_app_flows import FlowRunner


def test_run_segment_reports_whether_it_passed():
    """The caller can only stop if _run_segment tells it what happened.

    It used to return None on every path, so `for seg in segments: self._run_segment(seg)`
    had nothing to branch on and could not have stopped even in principle.
    """
    src = inspect.getsource(FlowRunner._run_segment)
    assert "return status == \"PASS\"" in src, (
        "_run_segment must return whether the segment passed — the flow loop "
        "branches on it to decide whether to keep going")
    # `from __future__ import annotations` is in effect in cross_app_flows, so
    # annotations are strings, not the type object.
    assert FlowRunner._run_segment.__annotations__.get("return") == "bool", (
        "_run_segment must be annotated -> bool so the contract is visible at the "
        "call site")


def test_run_loop_stops_on_a_failed_segment():
    """The flow loop must branch on the result and break."""
    src = inspect.getsource(FlowRunner.run)
    assert "if self._run_segment(seg)" in src, (
        "the segment loop must branch on _run_segment's result; calling it and "
        "discarding the return value is how the kitchen segment ran after the "
        "waiter segment had already failed")
    assert "break" in src, "the segment loop must break once a segment fails"


def test_remaining_segments_are_recorded_as_skipped():
    """Skipped segments must be PERSISTED, not silently dropped.

    A segment missing from the report reads as 'fine' to a person scanning it;
    an explicit SKIPPED badge says it never ran. _status_badge() in
    reporting/scenario_report.py already renders any non-PASS/FAIL status amber.
    """
    src = inspect.getsource(FlowRunner.run)
    assert '"SKIPPED"' in src, (
        "later segments must be persisted with status SKIPPED so the report shows "
        "they never ran instead of leaving a gap")


def test_skipped_is_not_counted_as_a_pass():
    """SKIPPED must not read as PASS anywhere the verdict is computed.

    The run verdict is `any(r.status == "FAIL")` and the dashboard counts
    `r.status == "PASS"` — both exact matches, so SKIPPED is neither. This test
    exists so a future change to a truthier-looking `!= "FAIL"` gets caught: that
    would silently turn every skipped segment into a pass.
    """
    src = inspect.getsource(FlowRunner.run)
    assert 'any(r.status == "FAIL" for r in rows)' in src, (
        "the verdict must match FAIL exactly; a `!= \"FAIL\"` test would count "
        "SKIPPED segments as passes")
