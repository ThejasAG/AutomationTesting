"""Guard: pressing Stop must actually stop an in-process cross-app flow run.

The Stop button used to be COSMETIC for these runs. `RunnerService.stop_run`
wrote status='stopped' to the database and nothing else, while the flow executed
in a daemon thread that never checked anything. Two consequences a user sees:

  1. The run keeps driving the simulators for minutes after Stop. The devices stay
     busy and the next run collides with it.
  2. The thread's own `finally` later recomputes the verdict and overwrites
     'stopped' with passed/failed — the run un-stops itself in the dashboard.

Cancellation is COOPERATIVE on purpose. A Python thread cannot be safely killed,
and killing this one would leave live Appium sessions unclosed and the simulator
wedged for the next run. The runner checks an Event between steps and segments and
unwinds through its normal finally, quitting every driver on the way out.

Run: PYTHONPATH=. .venv/bin/python -m pytest tests/test_flow_stop.py -q
"""
import inspect
import threading
import time

import pytest

from automation.scenarios import cross_app_flows as caf
from automation.scenarios.cross_app_flows import (
    FlowRunner,
    flow_run_is_active,
    request_flow_stop,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Never leak a registry entry between tests."""
    with caf._ACTIVE_FLOW_RUNS_LOCK:
        caf._ACTIVE_FLOW_RUNS.clear()
    yield
    with caf._ACTIVE_FLOW_RUNS_LOCK:
        caf._ACTIVE_FLOW_RUNS.clear()


def _runner(run_id="r1"):
    return FlowRunner(run_id, {"id": "f", "name": "F", "segments": []}, {}, {})


def test_stopping_an_unknown_run_is_a_no_op():
    """A queued run, a finished run, or one on a distributed agent has no thread here.

    It must report False rather than raise — the caller still marks the database,
    which is what stops those.
    """
    assert request_flow_stop("not-a-real-run") is False
    assert flow_run_is_active("not-a-real-run") is False


def test_a_registered_run_can_be_stopped():
    r = _runner("live-run")
    with caf._ACTIVE_FLOW_RUNS_LOCK:
        caf._ACTIVE_FLOW_RUNS["live-run"] = r._cancel

    assert flow_run_is_active("live-run") is True
    assert r.cancelled is False
    assert request_flow_stop("live-run") is True
    assert r.cancelled is True, "the runner must observe the stop request"


def test_stop_is_visible_to_the_thread_that_is_running():
    """The Event must cross threads — that is the entire point.

    Stop arrives on a FastAPI request thread; the flow runs on a daemon thread.
    """
    r = _runner("x")
    with caf._ACTIVE_FLOW_RUNS_LOCK:
        caf._ACTIVE_FLOW_RUNS["x"] = r._cancel
    seen = []

    def _flow_thread():
        for _ in range(200):
            if r.cancelled:
                seen.append("stopped")
                return
            time.sleep(0.01)
        seen.append("ran to completion")

    t = threading.Thread(target=_flow_thread, daemon=True)
    t.start()
    time.sleep(0.05)
    request_flow_stop("x")
    t.join(timeout=5)

    assert seen == ["stopped"], f"thread did not observe the stop: {seen}"


def test_runner_checks_for_cancellation_between_steps():
    """A step can run for STEP_TIMEOUT (240s), so the check must come BEFORE one starts."""
    src = inspect.getsource(FlowRunner._run_segment)
    assert "if self.cancelled" in src, (
        "_run_segment must check for cancellation inside its step loop; without it "
        "Stop cannot take effect until the whole segment finishes")


def test_a_stopped_run_is_not_reported_as_passed_or_failed():
    """The finally block must not overwrite 'stopped' with a computed verdict."""
    src = inspect.getsource(FlowRunner.run)
    assert 'run.status = "stopped"' in src, (
        "a cancelled run must persist status='stopped'; otherwise the verdict "
        "block overwrites the Stop button's own write and the run un-stops itself")
    assert src.index("if self.cancelled") < src.index('run.status = "failed"'), (
        "the cancelled check must come BEFORE the pass/fail verdict, or a stopped "
        "run is reported as failed")


def test_the_run_deregisters_itself_when_it_finishes():
    """A stale entry would make a finished run look stoppable."""
    src = inspect.getsource(FlowRunner.run)
    assert "_ACTIVE_FLOW_RUNS.pop(self.run_id, None)" in src, (
        "run() must deregister in its finally, on every exit path")


def test_sessions_are_still_quit_on_the_stop_path():
    """Cooperative cancel exists so teardown still happens.

    Killing the thread would leave Appium sessions open and the simulator wedged
    for the next run; the finally that quits them must be reachable after a stop.
    """
    src = inspect.getsource(FlowRunner.run)
    finally_block = src[src.index("finally:"):]
    assert "d.quit()" in finally_block, (
        "drivers must be quit in the finally so a stopped run frees its devices")


def test_stop_run_signals_the_in_process_flow():
    """RunnerService.stop_run must do more than write to the database."""
    from automation.runner.service import RunnerService
    src = inspect.getsource(RunnerService.stop_run)
    assert "request_flow_stop" in src, (
        "stop_run must signal the in-process flow runner; marking the database "
        "alone left the thread driving the simulators to the end of the flow")


def test_a_stranded_running_segment_is_reconciled():
    """No segment row may be left at 'running' once the run is over.

    A segment is persisted as 'running' BEFORE each step so Live Steps can show
    what is executing; that row is only overwritten when the step returns. Stop
    pressed during a long step ('open app' can take minutes) left the row spinning
    forever — the dashboard showed a STOPPED header above a RUNNING segment badge,
    which reads as "Stop did nothing". Nothing else ever revisits these rows.
    """
    src = inspect.getsource(FlowRunner.run)
    finally_block = src[src.index("finally:"):]
    assert 'in ("running", "queued")' in finally_block, (
        "run()'s finally must reconcile segment rows still marked running/queued")
    assert 'terminal = "STOPPED" if self.cancelled else "FAIL"' in finally_block, (
        "a stranded row must become STOPPED on the stop path and FAIL otherwise — "
        "never left running, and never silently PASS")


def test_the_in_flight_step_marker_is_cleared():
    """The UI spins on the '▶' marker, so it must not survive the run."""
    src = inspect.getsource(FlowRunner.run)
    assert 'startswith("▶")' in src, (
        "the '▶' in-flight marker must be stripped from a stranded row, or the "
        "dashboard keeps animating 'running: <step>' under a terminal badge")


def test_the_startup_reaper_also_reconciles_segment_rows():
    """A backend killed mid-run never reaches run()'s finally at all."""
    import automation.api.main as api_main
    src = inspect.getsource(api_main._reap_orphaned_runs)
    assert "ScenarioResult" in src, (
        "_reap_orphaned_runs must reap stranded SEGMENT rows too; reaping only the "
        "parent TestRun leaves a finished run showing a RUNNING segment")
