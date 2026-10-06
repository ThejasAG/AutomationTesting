"""The per-step native-crash check must not call Appium when the screen read it
already has proves the app is alive. queryAppState measured 6-52s per step on the
busy iPad (WDA snapshots the whole app to serve it)."""
from automation.intelligence.scenario_runner import ScenarioRunner
from automation.scenarios import idb_driver as dv

APP = {"type": "Application", "AXLabel": "Vya Business", "frame": {}}
HOME = {"type": "Application", "AXLabel": "SpringBoard", "frame": {}}


class _Driver:
    def __init__(self, states):
        self.states, self.calls = list(states), 0

    def query_app_state(self, bid):
        self.calls += 1
        return self.states.pop(0) if self.states else 4


def _runner(monkeypatch, screens, states):
    r = ScenarioRunner.__new__(ScenarioRunner)
    r.d, r.bid = _Driver(states), "org.vyapy.biz"
    monkeypatch.setattr(r, "_device_udid", lambda: "IPAD", raising=False)
    seq = iter(screens)
    monkeypatch.setattr(dv, "describe_all", lambda u: next(seq))
    return r


def test_our_app_on_top_skips_appium_once_learned(monkeypatch):
    r = _runner(monkeypatch, [[APP], [APP], [APP]], [4])
    assert r.app_crash() is None and r.d.calls == 1      # learns the label from state 4
    assert r.app_crash() is None and r.app_crash() is None
    assert r.d.calls == 1                                 # no more Appium calls


def test_home_screen_on_top_still_asks_and_reports_the_crash(monkeypatch):
    r = _runner(monkeypatch, [[APP], [HOME]], [4, 1])
    assert r.app_crash() is None
    assert "native crash" in r.app_crash()
    assert r.d.calls == 2


def test_label_is_not_learned_unless_foreground_is_confirmed(monkeypatch):
    r = _runner(monkeypatch, [[APP], [APP]], [3, 4])
    r.app_crash()
    assert getattr(r, "_app_label", None) is None
    r.app_crash()
    assert r._app_label == "Vya Business" and r.d.calls == 2
