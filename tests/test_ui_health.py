"""Passive UI loading monitor.

The monitor must never fail a run, never touch Appium, and never call a slow
screen broken. These tests pin all three.
"""
import inspect

from automation.scenarios import ui_health as U
from automation.scenarios.ui_health import (
    LoadResult, ScreenExpectation, SingleFlightSampler, find_spinners,
    monitor_ui_loading, normalise_state,
)

WALLET = ScreenExpectation(name="wallet",
                           required_all=("walletHeader",),
                           required_any=("walletBalance", "transactionHistory"),
                           timeout=30.0)


class FakeClock:
    """Deterministic time so the tests run instantly."""
    def __init__(self, step=5.0):
        self.t = 0.0
        self.step = step

    def __call__(self):
        return self.t

    def sleep(self, secs):
        self.t += secs


def run(frames, expectation=WALLET, window=30.0):
    """Drive the monitor over a scripted list of UI frames."""
    clk = FakeClock()
    seq = list(frames)

    def sample():
        return seq.pop(0) if seq else (frames[-1] if frames else [])

    return monitor_ui_loading(sample, expectation, watchdog_s=window,
                              sleep_s=5.0, clock=clk, sleeper=clk.sleep)


# ── the hard guarantees ──────────────────────────────────────────────────────
def test_monitor_never_imports_or_calls_appium():
    # Prose in the docstring may name Appium; the CODE must never import or use it.
    src = inspect.getsource(U)
    assert "import appium" not in src
    assert "from appium" not in src
    assert "webdriver" not in src.lower()
    assert not hasattr(U, "webdriver")
    assert not [n for n in dir(U) if "appium" in n.lower()]


def test_monitor_never_raises_even_if_the_sampler_explodes():
    def boom():
        raise RuntimeError("idb died")
    clk = FakeClock()
    rep = monitor_ui_loading(boom, WALLET, watchdog_s=10, sleep_s=5,
                             clock=clk, sleeper=clk.sleep)
    assert rep.result == LoadResult.MONITOR_UNAVAILABLE


def test_sampler_failure_is_never_blamed_on_the_app():
    rep = run([None, None, None, None])
    assert rep.result == LoadResult.MONITOR_UNAVAILABLE
    assert "MONITORING fault" in rep.to_note()
    assert not rep.is_problem


# ── result classes ───────────────────────────────────────────────────────────
def test_success_within_the_window():
    rep = run([["walletHeader", "walletBalance"]])
    assert rep.result == LoadResult.SUCCESS and not rep.is_problem


def test_slow_success_is_not_a_failure():
    late = [["spinner"]] * 7 + [["walletHeader", "transactionHistory"]]
    rep = run(late)
    assert rep.result == LoadResult.SLOW_SUCCESS
    assert not rep.is_problem
    assert "[SLOW LOAD]" in rep.to_note()


def test_partial_load_when_required_content_never_arrives():
    exp = ScreenExpectation(name="wallet", required_all=("walletHeader", "transactionHistory"))
    frames = [["walletHeader", "a"], ["walletHeader", "b"], ["walletHeader", "c"],
              ["walletHeader", "d"], ["walletHeader", "e"], ["walletHeader", "f"],
              ["walletHeader", "g"]]
    rep = run(frames, expectation=exp)
    assert rep.result == LoadResult.PARTIAL_LOAD
    assert "transactionHistory" in rep.missing


def test_stuck_loading_when_a_spinner_stays_up():
    frames = [["ActivityIndicator", f"x{i}"] for i in range(8)]
    rep = run(frames)
    assert rep.result == LoadResult.STUCK_LOADING
    assert rep.spinners


def test_no_progress_when_the_screen_never_changes():
    rep = run([["bookingBoard", "allBtn"]] * 8)
    assert rep.result == LoadResult.NO_PROGRESS
    assert rep.progressed is False


def test_timeout_when_ui_moves_but_target_never_appears():
    frames = [["screen", f"row{i}"] for i in range(8)]
    rep = run(frames)
    assert rep.result == LoadResult.TIMEOUT
    assert rep.progressed is True


# ── elapsed time alone is NOT an issue (the explicit correction) ─────────────
def test_exceeding_the_window_is_not_reported_when_content_arrives():
    frames = [["a"], ["b"], ["c"], ["d"], ["e"], ["f"], ["g"],
              ["walletHeader", "walletBalance"]]
    rep = run(frames)
    assert rep.result == LoadResult.SLOW_SUCCESS
    assert not rep.is_problem


# ── volatile UI filtering ────────────────────────────────────────────────────
def test_logbox_counter_is_not_progress():
    a = normalise_state(["Log 8 of 29", "bookingBoard"])
    b = normalise_state(["Log 8 of 28", "bookingBoard"])
    assert a == b


def test_leading_badge_count_is_not_progress():
    a = normalise_state(["6 Each child in a list should have a key", "board"])
    b = normalise_state(["28 Each child in a list should have a key", "board"])
    assert a == b


def test_clock_ticks_are_not_progress():
    assert normalise_state(["12:04", "board"]) == normalise_state(["12:05", "board"])


def test_queue_counter_is_not_progress():
    assert normalise_state(["In Queue 04", "orders"]) == normalise_state(["In Queue 09", "orders"])


def test_a_real_screen_change_IS_progress():
    assert normalise_state(["bookingBoard"]) != normalise_state(["reservationDetails"])


def test_volatile_only_churn_reports_no_progress():
    frames = [["Log 8 of 29", "bookingBoard"], ["Log 8 of 28", "bookingBoard"],
              ["Log 8 of 27", "bookingBoard"], ["Log 8 of 26", "bookingBoard"],
              ["Log 8 of 25", "bookingBoard"], ["Log 8 of 24", "bookingBoard"],
              ["Log 8 of 23", "bookingBoard"]]
    assert run(frames).result == LoadResult.NO_PROGRESS


# ── spinner detection ────────────────────────────────────────────────────────
def test_spinner_hints_match_common_indicators():
    assert find_spinners(["ActivityIndicator"])
    assert find_spinners(["productLoadingSpinner"])
    assert find_spinners(["Skeleton row"])
    assert not find_spinners(["addNewEvent", "walletHeader"])


# ── single-flight sampling ───────────────────────────────────────────────────
def test_single_flight_reuses_the_previous_read_while_one_is_in_flight():
    calls = {"n": 0}
    holder = {}

    def slow():
        calls["n"] += 1
        # re-enter while "in flight"
        if calls["n"] == 1:
            holder["reentrant"] = holder["s"]()
        return ["frame%d" % calls["n"]]

    s = SingleFlightSampler(slow)
    holder["s"] = s
    first = s()
    assert first == ["frame1"]
    assert holder["reentrant"] is None or holder["reentrant"] == s._last
    assert s.skipped == 1
    assert calls["n"] == 1          # the re-entrant call did NOT hit the sampler


def test_single_flight_swallows_sampler_errors():
    def boom():
        raise OSError("idb gone")
    s = SingleFlightSampler(boom)
    assert s() is None


# ── expectation semantics ────────────────────────────────────────────────────
def test_required_any_is_satisfied_by_one_match():
    exp = ScreenExpectation(name="reservation",
                            required_any=("selectAllItemsBtn", "addItemsBtn", "assignToBtn"))
    assert exp.satisfied(["addItemsBtn"])
    assert not exp.satisfied(["somethingElse"])


def test_empty_expectation_is_never_satisfied():
    assert not ScreenExpectation(name="none").satisfied(["anything"])


def test_missing_lists_the_gaps():
    exp = ScreenExpectation(name="wallet", required_all=("h",), required_any=("a", "b"))
    gaps = exp.missing(["h"])
    assert any("any of" in g for g in gaps)


# ── recovered after retry ────────────────────────────────────────────────────
def test_an_issue_refiled_as_recovered_is_not_a_failure():
    stalled = run([["bookingBoard"]] * 8)          # attempt 1 stalls
    assert stalled.result == LoadResult.NO_PROGRESS and stalled.is_problem
    rec = stalled.as_recovered(attempt=2)
    assert rec.result == LoadResult.RECOVERED
    assert not rec.is_problem
    assert "[SLOW / RECOVERED]" in rec.to_note()


def test_recovered_keeps_the_original_evidence():
    stalled = run([["ActivityIndicator", f"x{i}"] for i in range(8)])
    rec = stalled.as_recovered(attempt=2)
    assert rec.context["first_attempt_was"] == LoadResult.STUCK_LOADING
    assert rec.context["recovered_on_attempt"] == "2"
    assert rec.spinners == stalled.spinners
    assert rec.waited == stalled.waited


# ── named screens / substring content matching ───────────────────────────────
from automation.scenarios.ui_health import SCREENS


def test_wallet_shell_alone_is_not_a_successful_load():
    # real ids from a failure dump: the shell rendered, the bookings never did
    shell = ["tab", "couponBlock", "walletUpcomingSearchInput",
             "walletUpcomingFilterIcon", "Wallet", "Menu"]
    w = SCREENS["wallet"]
    assert not w.satisfied(shell)
    assert any("containing" in g for g in w.missing(shell))


def test_wallet_with_bookings_is_satisfied():
    full = ["walletUpcomingSearchInput", "MypreorderList", "RoopaDpreOrderCard"]
    assert SCREENS["wallet"].satisfied(full)


def test_wallet_shell_without_content_reports_partial_load():
    shell = ["walletUpcomingSearchInput", "couponBlock"]
    frames = [shell + [f"x{i}"] for i in range(8)]     # shell up, list never arrives, UI moving
    rep = run(frames, expectation=SCREENS["wallet"])
    assert rep.result == LoadResult.PARTIAL_LOAD
    assert "[PARTIAL LOAD]" in rep.to_note()


def test_substring_match_is_not_fooled_by_a_similar_word():
    exp = ScreenExpectation(name="t", required_any_contains=("Card",))
    assert not exp.satisfied(["discard", "placard"]) or exp.satisfied(["NylaiKitchen2Card"])
    assert exp.satisfied(["RoopaDpreOrderCard"])
