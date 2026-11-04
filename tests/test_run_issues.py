"""What a run saw that did not fail it is said in YELLOW (automation/scenarios/run_issues.py):
a crash after a step that did its job is relaunched past, a spinner left up, the app's
error toasts and slow screens are noted after their step, and the report lists them all
under Failed At without turning a passed segment red."""
import json
import threading

from automation.reporting import scenario_report as sr
from automation.scenarios import cross_app_flows as caf
from automation.scenarios import idb_driver as dv
from automation.scenarios import run_issues as iss
from automation.scenarios.cross_app_flows import FlowRunner

SPINNER = {"type": "GenericElement", "AXLabel": "In progress", "frame": {}}
METRO = {"type": "StaticText", "AXLabel": "Loading from Metro…", "frame": {}}
STATUS = {"type": "StaticText", "AXLabel": "In Progress", "frame": {}}
TOAST = {"type": "StaticText", "AXLabel": "Payment not done, Please try again !!", "frame": {}}
BUTTON = {"type": "Button", "AXLabel": "SEND", "frame": {}}


# ── which notes are yellow ─────────────────────────────────────────────────
def test_the_yellow_notes_are_found_each_once_and_read_plainly():
    reasons = [
        "[ok] @open_event — opened (3.1s)",
        "    [SLOW LOAD] @open_event — completed after 157.0s (over the 30s watchdog window)",
        "    [APP CRASH] the app crashed after '@pay:epay': RCTFatal — relaunched it and went on",
        "    [APP ERROR] @pay:epay — the app showed: 'Payment not done, Please try again !!'",
        "    [APP ERROR] @pay:epay — the app showed: 'Payment not done, Please try again !!'",
        "[warn] booking board still empty — no cards rendered (108.1s)",
        "    ⚡ flaky: 'click x' failed once, passed on retry",
        "    [MONITOR UNAVAILABLE] @x — could not sample the UI",
        "[FAIL] @y — nope",
        "[mismatch] Card: printed 280.47",
    ]
    # Slowness is not listed (the user, 2026-11-04: "dont mention slow steps").
    assert iss.issues_of(reasons) == [
        ("App crash", "the app crashed after '@pay:epay': RCTFatal — relaunched it and went on"),
        ("App error", "@pay:epay — the app showed: 'Payment not done, Please try again !!'"),
        ("Warning", "booking board still empty — no cards rendered (108.1s)"),
        ("Flaky", "'click x' failed once, passed on retry"),
    ]


def test_a_spinner_is_the_indicator_not_a_status_text():
    assert iss.loading_label([SPINNER]) == "In progress"
    assert iss.loading_label([METRO]) == "Loading from Metro…"
    assert iss.loading_label([{"type": "ActivityIndicator", "AXLabel": "", "frame": {}}])
    assert iss.loading_label([STATUS, BUTTON]) is None      # a booking's status


def test_error_texts_are_the_apps_failure_messages_not_its_refusals_or_ids():
    els = [TOAST, BUTTON,
           {"type": "StaticText", "AXLabel": "Something went wrong!", "frame": {}},
           {"type": "StaticText", "AXLabel": "You can't apply this coupon", "frame": {}},
           {"type": "Button", "AXLabel": "paymentFailedBtn", "frame": {}}]
    assert iss.error_texts(els) == ["Payment not done, Please try again !!",
                                    "Something went wrong!"]


# ── the step watch ─────────────────────────────────────────────────────────
class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_the_longest_spinner_stretch_is_measured_and_a_break_restarts_it():
    clock = _Clock()
    w = iss.StepWatch(["PHONE"], now=clock)
    for t, els in ((100, [SPINNER]), (104, [SPINNER]), (105, [BUTTON]),
                   (106, [SPINNER]), (115, [SPINNER]), (116, [BUTTON])):
        clock.t = t
        w("PHONE", els)
    assert w.longest == (9.0, "In progress")
    assert w.notes("@open_order") == [
        "[LOADING] @open_order — 'In progress' stayed on screen for about 9s"]


def test_a_short_spinner_and_another_device_say_nothing():
    clock = _Clock()
    w = iss.StepWatch(["PHONE"], now=clock)
    w("PHONE", [SPINNER]); clock.t += 4; w("PHONE", [SPINNER])
    w("IPAD", [TOAST])                                   # not this run's device
    assert w.notes("@x") == []


def test_an_error_toast_is_said_once_per_segment():
    seen: set = set()
    first = iss.StepWatch(["PHONE"], seen_errors=seen)
    first("PHONE", [TOAST]); first("PHONE", [TOAST])
    assert first.notes("@pay:epay") == [
        "[APP ERROR] @pay:epay — the app showed: 'Payment not done, Please try again !!'"]
    second = iss.StepWatch(["PHONE"], seen_errors=seen)
    second("PHONE", [TOAST])                             # the toast outlived the step
    assert second.notes("@next") == []


def test_a_slow_screen_is_said_unless_a_spinner_explains_it():
    assert iss.StepWatch().notes("@pay:epay", slow_wait=14.6) == [
        "[SLOW SCREEN] @pay:epay — the app took more than 15s to show the screen the "
        "step was waiting for"]
    assert iss.StepWatch().notes("@pay:epay", slow_wait=6.0) == []
    clock = _Clock()
    w = iss.StepWatch(now=clock)
    w("PHONE", [SPINNER]); clock.t += 12; w("PHONE", [SPINNER])
    assert [n.split(" ")[0] for n in w.notes("@x", slow_wait=14.6)] == ["[LOADING]"]


# ── the reads it watches ───────────────────────────────────────────────────
def test_every_screen_read_reaches_the_hooks_and_a_broken_hook_breaks_nothing(monkeypatch):
    screens = iter([json.dumps([BUTTON]), ""])
    monkeypatch.setattr(dv, "_idb", lambda args, timeout=None: next(screens))
    seen = []
    monkeypatch.setattr(dv, "READ_HOOKS", [lambda u, els: 1 / 0,
                                           lambda u, els: seen.append((u, len(els)))])
    assert dv.describe_all("PHONE") == [BUTTON]
    assert dv.describe_all("PHONE") == []              # nothing read: no hook call
    assert seen == [("PHONE", 1)]


def test_a_wait_that_got_its_screen_late_says_how_long_the_app_took(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(caf.time, "time", clock)
    monkeypatch.setattr(caf.time, "sleep", lambda s: setattr(clock, "t", clock.t + s))
    reads = iter([[BUTTON], [BUTTON], [BUTTON, SPINNER]])

    def read(udid):
        clock.t += 5.0                                  # each read takes 5s
        return next(reads)
    monkeypatch.setattr(caf._idbd, "describe_all", read)
    fr = FlowRunner.__new__(FlowRunner)
    fr._slow_wait = 0.0
    els, hit = fr._wait_els("PHONE", lambda es: SPINNER in es, 30.0)
    assert hit
    # The second read STARTED 5.6s in and did not show it: the app took more than that.
    assert round(fr._slow_wait, 1) == 5.6


def test_a_wait_that_never_got_its_screen_is_not_a_slow_screen(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(caf.time, "time", clock)
    monkeypatch.setattr(caf.time, "sleep", lambda s: setattr(clock, "t", clock.t + s))
    monkeypatch.setattr(caf._idbd, "describe_all",
                        lambda u: setattr(clock, "t", clock.t + 5.0) or [BUTTON])
    fr = FlowRunner.__new__(FlowRunner)
    fr._slow_wait = 0.0
    fr._wait_els("PHONE", lambda es: SPINNER in es, 10.0)
    assert fr._slow_wait == 0.0


# ── the segment loop ───────────────────────────────────────────────────────
class _Runner:
    bid = "org.app"
    _healthy_at = None

    def __init__(self, crashes):
        self.crashes = list(crashes)

    def app_crash(self):
        return self.crashes.pop(0) if self.crashes else None


def _segment(monkeypatch, crashes, relaunch_ok=True, step=None):
    monkeypatch.setenv("UI_WATCHDOG", "0")
    fr = FlowRunner.__new__(FlowRunner)
    fr._cancel = threading.Event()
    fr.devices = {"consumer": "PHONE"}
    fr.saved = []
    fr._persist = lambda seg, status, notes, secs, screenshot=None: \
        fr.saved.append((status, list(notes)))
    fr._metro_log_sizes = lambda: {}
    fr.on_event = lambda ev: None
    fr._capture_screenshot = lambda: None
    fr._collect_evidence = lambda role: ["    [evidence] CRASH Vya Consumer — abort()"]
    fr._visible_ids = lambda r: []
    r = _Runner(crashes)
    fr._session_for = lambda role: r
    fr.ran, fr.relaunched = [], []

    def handle(r_, s, notes):
        fr.ran.append(s)
        if step:
            step(s)
        notes.append(f"[ok] {s} — done")
        return True
    fr._handle_special = handle
    fr._relaunch_after_crash = lambda r_, role, notes: fr.relaunched.append(role) or relaunch_ok
    seg = {"role": "consumer", "name": "C", "steps": ["@one", "@two"]}
    return fr, fr._run_segment(seg)


def test_a_crash_after_a_step_that_worked_is_yellow_and_the_run_goes_on(monkeypatch):
    fr, passed = _segment(monkeypatch, ["RCTFatal: undefined is not an object"])
    assert passed and fr.ran == ["@one", "@two"] and fr.relaunched == ["consumer"]
    status, notes = fr.saved[-1]
    assert status == "PASS"
    assert ("    [APP CRASH] the app crashed after '@one': RCTFatal: undefined is not an "
            "object — relaunched it and went on") in notes
    assert "    [evidence] CRASH Vya Consumer — abort()" in notes
    assert not any(n.startswith("[FAIL]") for n in notes)


def test_a_crash_the_app_cannot_be_relaunched_from_fails(monkeypatch):
    fr, passed = _segment(monkeypatch, ["app terminated"], relaunch_ok=False)
    assert not passed and fr.ran == ["@one"]
    status, notes = fr.saved[-1]
    assert status == "FAIL"
    assert any(n.startswith("[FAIL] app crashed after '@one' and could not be relaunched")
               for n in notes)


def test_what_a_step_read_is_noted_after_it_in_yellow(monkeypatch):
    monkeypatch.setenv("ISSUE_LOADING_SECONDS", "0")
    screens = {"@one": [TOAST], "@two": [SPINNER]}
    monkeypatch.setattr(dv, "_idb", lambda args, timeout=None: json.dumps(screens[cur[0]]))
    cur = [""]

    def step(s):
        cur[0] = s
        dv.describe_all("PHONE")
    fr, passed = _segment(monkeypatch, [], step=step)
    assert passed
    notes = fr.saved[-1][1]
    at = {n.split(" (")[0]: i for i, n in enumerate(notes)}     # without '(0.0s)'
    assert notes[at["[ok] @one — done"] + 1].startswith(
        "    [APP ERROR] @one — the app showed: 'Payment not done")
    assert notes[at["[ok] @two — done"] + 1].startswith(
        "    [LOADING] @two — 'In progress' stayed on screen")
    assert dv.READ_HOOKS == [], "every step's watch is taken off when it ends"


# ── the report ─────────────────────────────────────────────────────────────
def _record(status, reasons, error=""):
    return {"num": "1", "name": "Pay", "status": status, "reasons": reasons, "error": error,
            "consumer_status": "PASS", "business_status": "N/A"}


def test_a_passed_segment_lists_its_warnings_yellow_and_stays_passed():
    rec = _record("PASS", [
        "[ok] @pay:epay — paid (3.0s)",
        "    [SLOW LOAD] @pay:epay — completed after 43.4s (over the 30s watchdog window)",
        "    [SLOW SCREEN] @pay:epay — the app took more than 10s to show the next screen",
        "    [LOADING] @pay:epay — 'In progress' stayed on screen for 12s",
        "    [MONITOR UNAVAILABLE] @x — could not sample the UI"])
    html = sr.render_run_report([rec], title="T")
    assert "PASSED" in html and "FAILED</span>" not in html
    assert ('<div style="color:#b7791f;font-weight:500;margin-top:8px;line-height:1.5">'
            "1. Loading: @pay:epay — 'In progress' stayed on screen for 12s</div>") in html
    assert "Slow step" not in html and "took 43.4s" not in html and "Slow screen" not in html
    assert "MONITOR UNAVAILABLE" not in html and "could not sample" not in html
    assert 'color:#b7791f">1</div><div class="lbl">Warnings' in html


def test_a_failed_segment_shows_its_failure_red_and_its_warnings_after_it():
    rec = _record("FAIL", ["    [APP GLITCH] @w_invoice — the History list was blank"],
                  error="@w_invoice — no card")
    cell = sr._build_failed_at_cell(rec)
    assert cell.index("no card") < cell.index("1. App glitch: @w_invoice — the History "
                                              "list was blank")
