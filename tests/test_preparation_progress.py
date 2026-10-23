"""Preparation reports how far it has got, from the steps it already emits.

Deliberately a percentage of WORK, not of time. The same project prepares in
20 seconds or 20 minutes depending on what is cached — a cold dependency
install and an Xcode build each routinely dominate the run — so a time estimate
would be fabricated. Phase progress is a fact; an ETA would not be.

Matched on the step text the pipeline already logs, so no call site passes a
number and the two cannot drift apart.
"""
import pytest

from automation.projects.preparation import (
    PHASES, _LONG_PHASES, _phase_for, preparation_tracker as tracker,
)

# The real sequence, copied from a failing run on a second Mac.
REAL_RUN = [
    "Repository already cloned.",
    "Checking out branch 'preprod-2-May18'...",
    "Pulling latest changes...",
    "Pull complete.",
    "Detected project type: react_native",
    "automation.yaml was missing — generated a template.",
    "Running validation...",
    "Installing dependencies...",
    "Simulator 5667D289 is booted.",
    "Building the ios app (this can take several minutes)...",
    "App installed.",
    "Project ready for execution.",
]


def test_01_progress_never_goes_backwards(_):
    seen = -1
    for msg in REAL_RUN:
        ph = _phase_for(msg)
        if ph is None:
            continue
        assert ph >= seen, f"{msg!r} moved the bar backwards ({seen} -> {ph})"
        seen = ph


def test_02_a_real_run_reaches_every_meaningful_stage(_):
    phases = {_phase_for(m) for m in REAL_RUN} - {None}
    for expected in (1, 2, 3, 4, 5, 6, 7, 8):
        assert expected in phases, f"no step maps to phase {expected}"


def test_03_the_last_step_is_one_hundred_percent(_):
    assert _phase_for("Project ready for execution.") == len(PHASES) - 1


def test_04_an_unrecognised_message_does_not_move_the_bar(_):
    assert _phase_for("some log line nobody planned for") is None


def test_05_a_late_warning_cannot_drag_progress_back(_):
    """A validation warning printed during the BUILD must not rewind the bar.

    The tracker only ever moves forward; this asserts the rule that makes that
    safe rather than the implementation of it.
    """
    build = _phase_for("Building the ios app (this can take several minutes)...")
    warn = _phase_for("WARNING: node_modules Installed — not found.")
    assert warn is None or warn < build
    assert max(build, warn or 0) == build


def test_06_the_slow_stages_are_flagged(_):
    """Install and build are the two that take minutes — the UI needs to say so
    rather than appear stalled."""
    assert _phase_for("Installing dependencies...") in _LONG_PHASES
    assert _phase_for("Building the ios app (this can take several minutes)...") in _LONG_PHASES
    assert _phase_for("Running validation...") not in _LONG_PHASES


# ── the shape the dashboard actually receives ───────────────────────────────

def test_07_an_idle_project_reports_no_progress(_):
    st = tracker.status("no-such-project")
    assert st["status"] == "idle"


def test_08_status_carries_percent_and_label(monkeypatch):
    """The dashboard needs a number and words, and both must be present the
    moment a run starts — not only once it finishes."""
    pid = "test-progress-project"
    with tracker._lock:
        tracker._tasks[pid] = {
            "task_id": "t1", "project_id": pid, "status": "running",
            "steps": [], "result": None, "phase": 4,
            "phase_label": "Installing dependencies",
            "started_at": 0, "started_phase_at": 0,
        }
    try:
        st = tracker.status(pid)
        assert st["percent"] == 50, st
        assert st["phase_label"] == "Installing dependencies"
        assert st["phase_count"] == len(PHASES)
        assert st["phase_is_long"] is True, "a slow stage must be flagged"
        assert st["elapsed_seconds"] >= 0
    finally:
        with tracker._lock:
            tracker._tasks.pop(pid, None)


def test_09_a_completed_run_is_exactly_one_hundred(_):
    pid = "test-progress-done"
    with tracker._lock:
        tracker._tasks[pid] = {
            "task_id": "t2", "project_id": pid, "status": "completed",
            "steps": [], "result": {"ok": True}, "phase": 6,
            "phase_label": "Building the app",
            "started_at": 0, "started_phase_at": 0,
        }
    try:
        st = tracker.status(pid)
        assert st["percent"] == 100, "a finished run must not sit at 75%"
        assert st["phase_label"] == "Done"
        assert st["phase_is_long"] is False
    finally:
        with tracker._lock:
            tracker._tasks.pop(pid, None)


def test_10_a_failed_run_holds_where_it_broke(_):
    """A failure must NOT read as 100% — the bar should show where it stopped."""
    pid = "test-progress-failed"
    with tracker._lock:
        tracker._tasks[pid] = {
            "task_id": "t3", "project_id": pid, "status": "failed",
            "steps": [], "result": {"ok": False}, "phase": 6,
            "phase_label": "Building the app",
            "started_at": 0, "started_phase_at": 0,
        }
    try:
        st = tracker.status(pid)
        assert st["percent"] == 75, st
        assert st["phase_label"] == "Failed"
    finally:
        with tracker._lock:
            tracker._tasks.pop(pid, None)


@pytest.fixture
def _():
    return None
