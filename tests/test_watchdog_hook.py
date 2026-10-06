"""Hook 1 — the passive step watchdog.

It observes; it must never decide. These tests pin that it cannot fail a step,
cannot raise, honours its kill switch, and only speaks when it has evidence.
"""
from automation.scenarios.cross_app_flows import FlowRunner


def _fr(monkeypatch=None):
    fr = FlowRunner.__new__(FlowRunner)
    fr._cur_udid = "UDID"
    fr.devices = {}
    return fr


def _w(fr, samples, spinner_all=False, errors=0, elapsed=99.0, spinners=None):
    """Hand-build a finished watchdog record."""
    import time
    return {"stop": __import__("threading").Event(), "states": samples,
            "spinners": spinners or [], "spinner_all": spinner_all,
            "samples": len(samples) + errors, "errors": errors,
            "t0": time.time() - elapsed, "first": [], "last": ["boardBtn"]}


def test_disabled_by_kill_switch(monkeypatch):
    monkeypatch.setenv("UI_WATCHDOG", "0")
    assert _fr()._start_step_watchdog("click x", {}) is None


def test_finish_on_a_none_watchdog_is_safe():
    assert _fr()._finish_step_watchdog(None, "click x", False) is None


def test_short_step_is_never_reported():
    fr = _fr()
    assert fr._finish_step_watchdog(_w(fr, [frozenset({"a"})], elapsed=3.0), "s", False) is None


def test_successful_step_over_the_window_is_only_slow_not_a_failure():
    fr = _fr()
    note = fr._finish_step_watchdog(_w(fr, [frozenset({"a"}), frozenset({"b"})]), "s", True)
    assert note and note.startswith("[SLOW LOAD]")


def test_no_progress_needs_evidence_of_no_change():
    fr = _fr()
    same = [frozenset({"bookingBoard"})] * 5
    note = fr._finish_step_watchdog(_w(fr, same), "open reservation", False)
    assert note and note.startswith("[NO UI PROGRESS]")


def test_moving_ui_is_not_reported_on_elapsed_time_alone():
    fr = _fr()
    moving = [frozenset({f"screen{i}"}) for i in range(5)]
    assert fr._finish_step_watchdog(_w(fr, moving), "s", False) is None


def test_stuck_spinner_is_reported():
    fr = _fr()
    moving = [frozenset({f"s{i}"}) for i in range(5)]
    note = fr._finish_step_watchdog(
        _w(fr, moving, spinner_all=True, spinners=["ActivityIndicator"]), "load menu", False)
    assert note and note.startswith("[STUCK LOADING]")


def test_sampling_failure_reports_monitor_unavailable_not_an_app_fault():
    fr = _fr()
    note = fr._finish_step_watchdog(_w(fr, [], errors=5), "s", False)
    assert note and note.startswith("[MONITOR UNAVAILABLE]")
    assert "not an app loading failure" in note


def test_finish_never_raises_on_a_malformed_record():
    assert _fr()._finish_step_watchdog({"garbage": True}, "s", False) is None


def test_window_is_configurable(monkeypatch):
    monkeypatch.setenv("UI_WATCHDOG_SECONDS", "45")
    assert _fr()._watchdog_window() == 45.0
    monkeypatch.setenv("UI_WATCHDOG_SECONDS", "not-a-number")
    assert _fr()._watchdog_window() == 30.0


def test_start_never_raises_even_if_the_environment_is_hostile(monkeypatch):
    # simulate the exact class of bug that shipped: a name blowing up inside start()
    import automation.scenarios.cross_app_flows as m
    monkeypatch.setattr(m, "os", None)          # os.getenv -> AttributeError
    assert _fr()._start_step_watchdog("click x", {}) is None
