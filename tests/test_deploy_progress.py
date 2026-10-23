"""Weighted progress + ETA for the Latest-build deploy.

Percentage must come from WEIGHTED work units, never from "steps logged so far": the
build is one step but most of the wall clock, so a step-count bar races to 90% and then
sits for minutes. The ETA is elapsed/done-weight extrapolated over the remaining weight,
so a slow machine reports a longer wait by itself.
"""
import pytest

from automation.api.v1.routers.builds import _Deploy


@pytest.fixture
def dep(monkeypatch):
    d = _Deploy()
    clock = {"t": 0.0}
    monkeypatch.setattr("automation.api.v1.routers.builds.time.monotonic", lambda: clock["t"])
    return d, clock


def test_a_fresh_deploy_reports_zero_not_a_fake_head_start(dep):
    d, _ = dep
    assert d.state["progress"]["percent"] == 0
    assert d.state["progress"]["eta_s"] is None


def test_percent_is_weighted_so_the_build_dominates(dep):
    d, clock = dep
    # 1 project, 2 devices: pull 1 + build 12 + 2 installs x 2 = 17 units
    total = d._W_PULL + d._W_BUILD + 2 * d._W_INSTALL
    d._progress_begin(total)
    d._advance(d._W_PULL)
    # The pull is done — one of three "steps", but only 1/17th of the work.
    assert d.state["progress"]["percent"] == pytest.approx(100 * 1 / 17, abs=1)
    d._advance(d._W_BUILD)
    assert d.state["progress"]["percent"] == pytest.approx(100 * 13 / 17, abs=1)


def test_eta_is_none_until_something_has_actually_completed(dep):
    d, clock = dep
    d._progress_begin(17)
    clock["t"] = 300.0            # five minutes into a build, nothing finished yet
    d._publish()
    assert d.state["progress"]["eta_s"] is None       # no basis -> no invented number
    assert d.state["progress"]["elapsed_s"] == 300


def test_eta_extrapolates_from_measured_rate(dep):
    d, clock = dep
    d._progress_begin(20)
    clock["t"] = 100.0
    d._advance(5)                 # 5 units in 100s -> 20s/unit, 15 units left
    assert d.state["progress"]["eta_s"] == 300


def test_a_slower_machine_reports_a_longer_wait_on_its_own(dep):
    d, clock = dep
    d._progress_begin(20)
    clock["t"] = 400.0
    d._advance(5)                 # same work, 4x the time
    assert d.state["progress"]["eta_s"] == 1200


def test_phase_tracks_what_is_happening_right_now(dep):
    d, clock = dep
    d._progress_begin(17)
    d._phase("build", "Consumer App")
    clock["t"] = 90.0
    d._publish()
    p = d.state["progress"]
    assert p["phase"] == "build" and p["detail"] == "Consumer App"
    assert p["phase_elapsed_s"] == 90          # the bar is still, but this proves it is alive


def test_phase_elapsed_resets_per_phase(dep):
    d, clock = dep
    d._progress_begin(17)
    d._phase("pull", "A")
    clock["t"] = 50.0
    d._phase("build", "A")
    clock["t"] = 60.0
    d._publish()
    assert d.state["progress"]["phase_elapsed_s"] == 10


def test_progress_never_exceeds_one_hundred(dep):
    d, _ = dep
    d._progress_begin(10)
    d._advance(999)
    assert d.state["progress"]["percent"] == 100
    assert d.state["progress"]["done"] == 10


def test_a_skipped_project_still_advances_the_bar(dep):
    """A failed pull skips that project's build+installs. If their weight were left
    pending the bar would stall at a number it could never leave."""
    d, _ = dep
    per_project = d._W_PULL + d._W_BUILD + 2 * d._W_INSTALL
    d._progress_begin(2 * per_project)
    d._advance(per_project)                    # project 1 failed its pull
    assert d.state["progress"]["percent"] == 50


def test_starting_a_deploy_resets_progress_from_the_previous_run(dep):
    d, _ = dep
    d._progress_begin(10)
    d._advance(10)
    assert d.state["progress"]["percent"] == 100
    d.state = {"status": "running", "steps": [], "results": [],
               "progress": d._blank_progress()}
    assert d.state["progress"]["percent"] == 0
    assert d.state["progress"]["eta_s"] is None


def test_the_clocks_keep_moving_during_a_long_silent_build(dep):
    """Nothing publishes while a build runs, so the timers must be recomputed on read —
    otherwise elapsed sticks at the value it had when the build STARTED and the UI looks
    frozen for the several minutes that matter most."""
    from automation.api.v1.routers import builds
    d, clock = dep
    builds._deploy = d
    d.state["status"] = "running"
    d._progress_begin(17)
    d._advance(d._W_PULL)
    d._phase("build", "Consumer App")
    assert d.state["progress"]["elapsed_s"] == 0

    clock["t"] = 240.0                       # four minutes into the build, no publish
    assert d.state["progress"]["elapsed_s"] == 0        # stale until someone reads it
    live = builds.deploy_status()
    assert live["progress"]["elapsed_s"] == 240
    assert live["progress"]["phase_elapsed_s"] == 240
    assert live["progress"]["eta_s"] == 240 * 16        # 1 unit in 240s, 16 to go


def test_a_finished_deploy_is_not_re_timed_on_read(dep):
    from automation.api.v1.routers import builds
    d, clock = dep
    builds._deploy = d
    d._progress_begin(10)
    d._advance(10)
    d.state["status"] = "completed"
    clock["t"] = 9999.0
    assert builds.deploy_status()["progress"]["elapsed_s"] == 0   # frozen at completion
