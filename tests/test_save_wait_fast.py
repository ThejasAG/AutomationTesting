"""@save_appointment's wait for the form to close: cheap point probes, one full
read to confirm. It took 127s re-reading the whole iPad screen every round."""
import itertools

from automation.scenarios import cross_app_flows as caf

SAVE = {"id": "", "label": "saveBtn", "type": "GenericElement",
        "x": 832, "y": 685, "w": 347, "h": 50, "cx": 1005, "cy": 710}


def _runner(monkeypatch, point_answers, full_reads):
    fr = caf.FlowRunner.__new__(caf.FlowRunner)
    fr._cur_udid = "IPAD"
    fr.devices = {}
    monkeypatch.setattr(fr, "_idb_els", lambda udid="": [SAVE], raising=False)
    monkeypatch.setattr(caf._idbd, "to_device", lambda u, x, y: (int(x), int(y)))
    pts = iter(point_answers)
    monkeypatch.setattr(caf._idbd, "describe_point",
                        lambda u, x, y: {"AXLabel": next(pts, "")})
    monkeypatch.setattr(caf.time, "sleep", lambda s: None)
    calls = {"full": 0}
    reads = iter(full_reads)

    def form_open():
        calls["full"] += 1
        return next(reads, True)
    return fr, form_open, calls


def test_closes_as_soon_as_the_button_is_gone(monkeypatch):
    fr, form_open, calls = _runner(monkeypatch, ["saveBtn", "saveBtn", ""], [False])
    assert fr._wait_form_closed(form_open) is True
    assert calls["full"] == 1            # only the confirming read, not one per round


def test_a_toast_over_the_button_is_not_taken_for_closed(monkeypatch):
    # The point stops answering saveBtn (a toast), but the full read says still open,
    # then the button is back, and finally it really closes.
    fr, form_open, calls = _runner(monkeypatch, ["toast", "saveBtn", ""], [True, False])
    assert fr._wait_form_closed(form_open) is True
    assert calls["full"] == 2


def test_gives_up_after_the_window(monkeypatch):
    fr, form_open, _ = _runner(monkeypatch, itertools.repeat("saveBtn"), [])
    clock = itertools.count(0, 5)
    monkeypatch.setattr(caf.time, "time", lambda: next(clock))
    assert fr._wait_form_closed(form_open, window=30) is False
