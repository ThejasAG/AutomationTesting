"""Pure screen loading times (the user, 2026-11-17): from the tap that opens a screen to
the first screen read that shows it -- none of the run's own waits -- noted per step and
shown in the report when over 1 s."""
from automation.reporting import scenario_report as sr
from automation.scenarios import cross_app_flows as caf
from automation.scenarios.cross_app_flows import FlowRunner


def _fr():
    return FlowRunner.__new__(FlowRunner)


def test_the_load_time_runs_from_the_tap_to_the_read_that_showed_it(monkeypatch):
    fr = _fr()
    clock = iter([100.0])
    monkeypatch.setattr(caf.time, "time", lambda: next(clock))
    fr._nav_start("c_home")                      # the tap at t=100
    fr._nav_done("c_home", at=103.2)             # Home showed in the read that began at 103.2
    notes = []
    fr._flush_loads(notes)
    assert notes == ["[load] Home screen (C-App) — 3.2s"]


def test_only_the_started_screen_is_timed_and_only_once():
    fr = _fr()
    fr._nav_start("c_cart")
    fr._nav_done("c_payment", at=0.0)            # another screen: ignored
    fr._nav_done("c_cart")
    fr._nav_done("c_cart")                        # a second end: ignored
    notes = []
    fr._flush_loads(notes)
    assert len(notes) == 1 and notes[0].startswith("[load] Cart screen (C-App) — ")


def test_wait_els_remembers_when_the_screen_first_showed(monkeypatch):
    fr = _fr()
    reads = iter([[{"AXLabel": "x"}], [{"AXLabel": "cartCheckout"}]])
    monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: next(reads))
    monkeypatch.setattr(caf.time, "sleep", lambda s: None)
    els, hit = fr._wait_els("U", lambda es: any(e["AXLabel"] == "cartCheckout" for e in es), 5.0)
    assert hit and fr._hit_at is not None


def test_a_dialogs_last_tap_starts_the_clock_and_its_close_ends_it(monkeypatch):
    fr = _fr()
    monkeypatch.setattr(fr, "_box", lambda els, kind: (0, 0, 10, 10), raising=False)
    taps = []
    monkeypatch.setattr(fr, "_ocr_tap", lambda udid, box, text: (taps.append(text) or True, text),
                        raising=False)
    monkeypatch.setattr(fr, "_wait_els", lambda udid, pick, secs: ([], True), raising=False)
    monkeypatch.setattr(caf.time, "sleep", lambda s: None)
    fr._overlay_nav = ("b_void", True)
    assert fr._overlay_taps("U", [], "void", ["Entry error", "Apply"], "m", "@w_void:x", []) == []
    assert taps == ["Entry error", "Apply"]
    notes = []
    fr._flush_loads(notes)
    assert len(notes) == 1 and notes[0].startswith("[load] Void (B-App) — ")
    assert fr._overlay_nav is None               # used once


def test_every_screen_the_user_named_has_a_name():
    names = set(FlowRunner.SCREEN_LOADS.values())
    for want in ("Home screen (C-App)", "Reservation screen (C-App)", "Menu screen (C-App)",
                 "Add to cart (C-App)", "Cart screen (C-App)", "Payment screen (C-App)",
                 "Payment confirm (C-App)", "Accept invitation (C-App)", "Logout (C-App)",
                 "Sign in → Home (B-App)", "Assign table (B-App)", "Add items (B-App)",
                 "Assign (B-App)", "Split (B-App)", "Comp (B-App)", "Void (B-App)",
                 "Payment confirm (B-App)", "Close table (B-App)", "Logout (B-App)",
                 "Kitchen ready (B-App)", "Kitchen close (B-App)", "Send items (B-App)",
                 "Notify payment (B-App)", "Serve (B-App)", "Event invoice (B-App)",
                 "Individual invoice (C-App)"):
        assert want in names, want


def _rec(num, name, *loads):
    return {"num": num, "name": name, "status": "PASS", "reasons": list(loads)}


def test_the_report_lists_only_the_screens_over_a_second():
    recs = [_rec("1", "Host (C-App): book", "[load] Home screen (C-App) — 0.6s",
                 "[load] Reservation screen (C-App) — 3.2s"),
            _rec("3", "Waiter: assign", "[load] Assign table (B-App) — 1.4s")]
    html = sr.render_run_report(recs, title="T")
    assert "Screen load times" in html and "(2 of 3 timed)" in html
    assert "Reservation screen (C-App)" in html and "3.2 s" in html
    assert "Assign table (B-App)" in html and "Home screen (C-App)" not in html


def test_a_run_with_every_screen_quick_says_so_and_an_old_run_draws_nothing():
    quick = sr.render_run_report([_rec("1", "H", "[load] Home screen (C-App) — 0.4s")], title="T")
    assert "All 1 timed screens loaded in 1 s or less." in quick
    old = sr.render_run_report([_rec("1", "H", "[ok] open app")], title="T")
    assert "Screen load times" not in old


def test_over_twenty_seconds_is_reported_as_loading_indefinitely():
    fr = _fr()
    fr._nav_start("c_reservation")
    fr._nav_done("c_reservation", at=fr._nav[1] + 23.4)
    notes = []
    fr._flush_loads(notes)
    assert notes == ["[load] Reservation screen (C-App) — loads indefinitely (over 20s)"]


def test_a_screen_that_never_came_is_reported_as_loading_indefinitely(monkeypatch):
    fr = _fr()
    fr._nav_start("b_event_invoice")
    fr._nav = ("b_event_invoice", fr._nav[1] - 45.0)      # started 45 s ago, never came
    notes = []
    fr._flush_loads(notes)
    assert notes == ["[load] Event invoice (B-App) — loads indefinitely (over 20s)"]
    fr._nav_start("c_cart")                               # a quick one still pending: dropped
    fr._flush_loads(notes)
    assert len(notes) == 1


def test_the_report_shows_an_indefinite_load_in_red():
    recs = [_rec("19", "Waiter: the event invoice",
                 "[load] Event invoice (B-App) — loads indefinitely (over 20s)")]
    html = sr.render_run_report(recs, title="T")
    assert "loads indefinitely" in html and "Event invoice (B-App)" in html and "(1 of 1 timed)" in html
