"""A run takes a screenshot ONLY when the app crashed.

The user, 2026-11-17: "the screenshot should take while running scenarios only when it
crashes, when the screen shows white screen with warning modal. otherwise no need of
taking screenshots while running". A crash is the red box, a warning modal on a white
screen, or the app gone from the screen (a native crash). A step that simply failed on
a normal screen gets its notes and evidence, but no screenshot.
"""
import threading
from pathlib import Path

from automation.reporting import scenario_report as sr
from automation.scenarios import cross_app_flows as caf
from automation.scenarios import idb_driver as dv
from automation.scenarios.cross_app_flows import FlowRunner

SHOT = "data:image/png;base64,iVBORw0KGgo="


def _el(type_, label="", ident="", x=0, y=0, w=10, h=10):
    return {"type": type_, "AXLabel": label, "AXIdentifier": ident,
            "frame": {"x": x, "y": y, "width": w, "height": h}}


APP = _el("Application", "Vya", w=390, h=844)
NORMAL = [APP, _el("StaticText", "NylaiKitchen2", y=100, w=200, h=20),
          _el("Button", "", "bookAppoitment", y=700, w=300, h=50)]
RED_BOX = [APP, _el("StaticText", "Render Error", y=60, w=300, h=30),
           _el("StaticText", "undefined is not an object", y=100, w=300, h=60)]
# A white app screen with a warning box in the middle and nothing else.
WHITE_WITH_WARNING = [APP,
                      _el("Alert", "", x=40, y=300, w=310, h=200),
                      _el("StaticText", "Warning: something went wrong", x=60, y=320, w=270, h=40),
                      _el("Button", "Dismiss", x=60, y=440, w=270, h=40)]
# The same box over a normal screen: a confirm dialog the app shows on purpose.
WARNING_OVER_A_SCREEN = NORMAL + WHITE_WITH_WARNING[1:]
SPRINGBOARD = [_el("Application", "SpringBoard", w=390, h=844),
               _el("Button", "Safari", y=700, w=60, h=60)]


# ── which screens count as a crash ──────────────────────────────────────────

def test_the_red_box_is_a_crash():
    assert dv.crash_screen(RED_BOX) == "Render Error"


def test_a_warning_modal_on_a_white_screen_is_a_crash():
    assert dv.crash_screen(WHITE_WITH_WARNING) == \
        "white screen with a warning: Warning: something went wrong"


def test_a_modal_over_a_normal_screen_is_not_a_crash():
    assert dv.crash_screen(WARNING_OVER_A_SCREEN) is None


def test_a_normal_screen_and_an_empty_read_are_not_a_crash():
    assert dv.crash_screen(NORMAL) is None
    assert dv.crash_screen([]) is None          # idb failing, not the app


def test_an_error_line_on_a_normal_screen_is_not_a_crash():
    toast = NORMAL + [_el("StaticText", "Payment error, try again", y=780, w=300, h=20)]
    assert dv.crash_screen(toast) is None


# ── the flow engine ────────────────────────────────────────────────────────

class _Runner:
    _app_label = "Vya"

    def app_crash(self):
        return None


def _segment(monkeypatch, screen, ok=False):
    monkeypatch.setenv("UI_WATCHDOG", "0")
    monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: list(screen))
    fr = FlowRunner.__new__(FlowRunner)
    fr._cancel = threading.Event()
    fr.devices = {"consumer": "PHONE"}
    fr._cur_udid = "PHONE"
    fr.saved, fr.shots = [], []
    fr._persist = lambda seg, status, notes, secs, screenshot=None: \
        fr.saved.append((status, screenshot))
    fr._metro_log_sizes = lambda: {}
    fr.on_event = lambda ev: None
    fr._capture_screenshot = lambda: fr.shots.append(1) or SHOT
    fr._collect_evidence = lambda role: ["    [evidence] none"]
    fr._visible_ids = lambda r: []
    fr._session_for = lambda role: _Runner()
    fr._handle_special = lambda r, s, notes: ok
    seg = {"role": "consumer", "name": "C", "steps": ["@one"]}
    return fr, fr._run_segment(seg)


def test_a_step_that_failed_on_a_normal_screen_takes_no_screenshot(monkeypatch):
    fr, passed = _segment(monkeypatch, NORMAL)
    assert not passed and fr.saved[-1] == ("FAIL", None) and fr.shots == []


def test_a_step_that_failed_on_a_crash_screen_takes_one(monkeypatch):
    for screen in (RED_BOX, WHITE_WITH_WARNING, SPRINGBOARD):
        fr, passed = _segment(monkeypatch, screen)
        assert not passed and fr.saved[-1] == ("FAIL", SHOT) and fr.shots == [1], screen


def test_a_passing_segment_takes_no_screenshot(monkeypatch):
    fr, passed = _segment(monkeypatch, NORMAL, ok=True)
    assert passed and fr.saved[-1] == ("PASS", None) and fr.shots == []


def test_a_crash_stops_the_segment_with_its_screenshot(monkeypatch):
    """A crash stops the run (2026-11-17); its screen is the segment's screenshot."""
    monkeypatch.setenv("UI_WATCHDOG", "0")
    crashes = ["RCTFatal: undefined is not an object"]

    class Crashing(_Runner):
        def app_crash(self):
            return crashes.pop(0) if crashes else None
    fr, _ = _segment(monkeypatch, NORMAL, ok=True)
    fr.saved, fr.shots = [], []
    fr._session_for = lambda role: Crashing()
    assert not fr._run_segment({"role": "consumer", "name": "C", "steps": ["@one", "@two"]})
    assert fr.saved[-1] == ("FAIL", SHOT) and fr.shots == [1]


def test_no_failure_path_screenshots_the_screen_directly():
    """Every failure path goes through _crash_shot. The only direct capture is the one
    after a crash app_crash already found (and the one inside _crash_shot itself)."""
    src = Path("automation/scenarios/cross_app_flows.py").read_text()
    assert src.count("self._capture_screenshot()") == 2
    assert src.count("self._crash_shot(") == 4


def test_saved_scenario_runs_follow_the_same_rule():
    src = Path("automation/intelligence/scenario_runner.py").read_text()
    assert "if not res.ok and self.shot_dir and (crash or self._crash_on_screen()):" in src


# ── the report ──────────────────────────────────────────────────────────────

def test_the_report_shows_the_crash_screen_even_on_a_segment_that_passed():
    html = sr._screenshot_html({"status": "PASS", "screenshot": SHOT})
    assert "Screen when the app crashed:" in html and SHOT in html
    assert sr._screenshot_html({"status": "FAIL", "screenshot": ""}) == ""


# ── the void's money check (exact to the cent, 2026-11-17) ──────────────────

def _void(price0, price1, before=(3, "Pizza Margherita x 3"), after=(2, "Pizza Margherita x 2")):
    fr = FlowRunner.__new__(FlowRunner)
    notes = []
    fr._void_check("@w_void:Pizza Margherita@Guest 4*1", "Guest 4", "Pizza Margherita",
                   before, after, price0, price1, notes)
    return notes


def test_a_void_that_leaves_the_right_price_passes():
    notes = _void(33.99, 22.66)                       # 3 x 11.33 -> 2 x 11.33
    t = caf.invoice_check.calc_of(notes[0])
    assert t["t"] == "Void (Guest 4)" and t["r"][0][-1] is True and len(notes) == 1


def test_a_void_a_cent_off_is_red_and_the_flow_goes_on():
    notes = _void(33.99, 22.67)
    assert caf.invoice_check.calc_of(notes[0])["r"][0][-1] is False
    assert notes[1].startswith(caf.invoice_check.MISMATCH_PREFIX)
    assert "expected €22.66 but the card shows €22.67 (diff +0.01)" in notes[1]


def test_a_void_whose_price_could_not_be_read_is_information():
    notes = _void(None, 22.66)
    assert caf.invoice_check.calc_of(notes[0])["r"][0][-1] is None and len(notes) == 1


# ── the order's grand Total around a split / void / comp (2026-11-17) ─────────

def _el2(t, label, x, y, w, h):
    return {"type": t, "AXLabel": label, "frame": {"x": x, "y": y, "width": w, "height": h}}


ORDER = [_el2("Application", "Vya Business", 0, 0, 1194, 834),
         _el2("StaticText", "Total", 900, 300, 60, 20), _el2("StaticText", "12.50 €", 1050, 300, 80, 20),
         _el2("StaticText", "Total", 860, 740, 90, 74), _el2("StaticText", "404.56 €", 1040, 745, 120, 64)]


def test_the_grand_total_is_the_panels_footer_not_a_cards_total():
    fr = FlowRunner.__new__(FlowRunner)
    assert fr._order_total("IPAD", ORDER) == 404.56


def _gt(kind, before, value, after):
    fr = FlowRunner.__new__(FlowRunner)
    notes = []
    fr._grand_total_check(kind, f"@w_{kind}:Penne Pollo@Guest 1*1", before, value, after, notes)
    return caf.invoice_check.calc_of(notes[0])["r"][0], notes


def test_a_split_leaves_the_grand_total_as_it_was():
    row, notes = _gt("split", 404.56, None, 404.56)
    assert row[-1] is True and len(notes) == 1
    row, notes = _gt("split", 404.56, None, 404.57)
    assert row[-1] is False and "expected 404.56 € after it but the order shows 404.57 €" in notes[1]


def test_a_void_or_comp_takes_off_exactly_its_value():
    assert _gt("void", 404.56, 17.51, 387.05)[0][-1] is True
    assert _gt("comp", 404.56, 0.18, 404.38)[0][-1] is True
    row, notes = _gt("comp", 404.56, 0.18, 404.39)          # a cent off
    assert row[-1] is False and notes[1].startswith(caf.invoice_check.MISMATCH_PREFIX)


def test_a_grand_total_not_read_is_information():
    assert _gt("void", None, 17.51, 387.05)[0][-1] is None
    assert _gt("void", 404.56, None, 387.05)[0][-1] is None


def test_the_check_reads_the_total_after_the_step_and_never_changes_its_result(monkeypatch):
    fr = FlowRunner.__new__(FlowRunner)
    fr._cur_udid = "IPAD"
    reads = []
    monkeypatch.setattr(caf._idb_fast, "recent_screen", lambda udid, age: None)
    monkeypatch.setattr(caf._idbd, "describe_all", lambda udid: reads.append("read") or ORDER)
    order = []
    def act():
        order.append("step"); fr._money_delta = 0.0
        return True
    notes = []
    assert fr._with_grand_total("split", "@w_split:x", act, notes) is True
    assert order == ["step"] and reads == ["read", "read"]   # before (no fresh read) + after
    def failing():
        return False
    assert fr._with_grand_total("void", "@w_void:x", failing, []) is False
