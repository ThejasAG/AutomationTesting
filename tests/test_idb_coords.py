"""idb taps on the landscape iPad must land on the element describe-all names.

describe-all reports landscape app coordinates; tap/describe-point take portrait
device coordinates, turned one way or the other depending on how the simulator
was last rotated. A fixed formula (the old _rotate_for_device) was right for one
direction only: with the iPad turned the other way every idb tap missed --
Save 'did nothing', and a tap on Table opened History.
"""
import pytest

from automation.scenarios import idb_coords as ic

W, H = 1210.0, 834.0
APP = {"type": "Application", "frame": {"x": 0, "y": 0, "width": W, "height": H}}
HOME = {"type": "Button", "AXLabel": "homeBtn",
        "frame": {"x": 30, "y": 40, "width": 36, "height": 50}}


def _device(mode, udid="U"):
    """A fake idb whose describe-point answers in *mode*'s device space."""
    cx, cy = 48, 65                                    # homeBtn centre (app space)
    real = ic._MODES[mode](cx, cy, W, H)

    def run_json(args, timeout=15):
        if args[:2] == ["ui", "describe-all"]:
            return [APP, HOME]
        if args[:2] == ["ui", "describe-point"]:
            px, py = int(args[-2]), int(args[-1])
            return HOME if (px, py) == tuple(int(v) for v in real) else {}
        raise AssertionError(args)
    return run_json


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    # Never write the real ~/.vya-platform/idb_rotation.json from a test.
    monkeypatch.setattr(ic, "_GOOD_FILE", str(tmp_path / "idb_rotation.json"))
    ic.forget()
    ic._GOOD.clear()
    yield
    ic.forget()
    ic._GOOD.clear()


def test_forget_keeps_the_last_confirmed_direction(monkeypatch):
    """The Inspector forgets on every Inspect; a failed probe right after must still
    fall back to the direction last seen working, not the bare 'cw' guess."""
    monkeypatch.setattr(ic, "_run_json", _device("ccw"))
    assert ic.mode("U") == "ccw"
    ic.forget("U")
    monkeypatch.setattr(ic, "_run_json", lambda a, timeout=15:
                        [APP, HOME] if a[:2] == ["ui", "describe-all"] else {})
    assert ic.mode("U") == "ccw"


@pytest.mark.parametrize("mode", ["ccw", "cw"])
def test_either_rotation_is_measured_and_hit(monkeypatch, mode):
    monkeypatch.setattr(ic, "_run_json", _device(mode))
    assert ic.to_device("U", 48, 65) == tuple(int(v) for v in ic._MODES[mode](48, 65, W, H))


def test_the_orientation_seen_today(monkeypatch):
    # Measured: homeBtn at describe-all (48, 65) was tapped at device (769, 48).
    monkeypatch.setattr(ic, "_run_json", _device("ccw"))
    assert ic.to_device("U", 48, 65) == (769, 48)


def test_portrait_device_is_unchanged(monkeypatch):
    phone = {"type": "Application", "frame": {"x": 0, "y": 0, "width": 402, "height": 874}}
    monkeypatch.setattr(ic, "_run_json", lambda a, timeout=15: [phone])
    assert ic.to_device("P", 100, 200) == (100, 200)


def test_direction_is_cached(monkeypatch):
    calls = []
    fake = _device("ccw")
    monkeypatch.setattr(ic, "_run_json", lambda a, timeout=15: calls.append(a) or fake(a))
    ic.to_device("U", 1, 1)
    n = len(calls)
    ic.to_device("U", 2, 2)
    assert len(calls) == n


def test_idb_failure_never_raises(monkeypatch):
    def boom(a, timeout=15):
        raise OSError("idb gone")
    monkeypatch.setattr(ic, "_run_json", boom)
    assert ic.to_device("U", 10, 20) == (10, 20)


def _with_form_open(mode):
    """The New Appointment form is open: the background elements listed FIRST are
    under its backdrop (describe-point hits the backdrop), and the form's own
    controls are listed LAST. Measured 2026-10-06 on the iPad."""
    backdrop = {"type": "Other", "AXLabel": "",
                "frame": {"x": 0, "y": 0, "width": W, "height": H}}
    behind = [{"type": "Button", "AXLabel": f"bg{i}",
               "frame": {"x": 20 + 40 * i, "y": 40, "width": 36, "height": 50}}
              for i in range(8)]
    slot = {"type": "Button", "AXLabel": "12:05Btn",
            "frame": {"x": 850, "y": 463, "width": 111, "height": 32}}
    real = tuple(int(v) for v in ic._MODES[mode](905.5, 479.0, W, H))

    def run_json(args, timeout=15):
        if args[:2] == ["ui", "describe-all"]:
            return [APP, *behind, backdrop, slot]
        if args[:2] == ["ui", "describe-point"]:
            px, py = int(args[-2]), int(args[-1])
            return slot if (px, py) == real else backdrop
        raise AssertionError(args)
    return run_json


@pytest.mark.parametrize("mode", ["ccw", "cw"])
def test_a_form_covering_the_first_probes_is_still_measured(monkeypatch, mode):
    monkeypatch.setattr(ic, "_run_json", _with_form_open(mode))
    assert ic.mode("U") == mode


def test_a_failed_measurement_keeps_the_last_confirmed_direction(monkeypatch):
    monkeypatch.setattr(ic, "_run_json", _device("ccw"))
    assert ic.mode("U") == "ccw"
    # Later every probe misses (e.g. an overlay we cannot see past) and the cache expires.
    ic._CACHE["U"] = (0.0, "ccw", W, H)
    monkeypatch.setattr(ic, "_run_json", lambda a, timeout=15:
                        [APP, HOME] if a[:2] == ["ui", "describe-all"] else {})
    assert ic.mode("U") == "ccw"          # not the blind 'cw' guess


def test_a_failed_measurement_is_retried_soon(monkeypatch):
    calls = []
    monkeypatch.setattr(ic, "_run_json", lambda a, timeout=15: calls.append(a) or
                        ([APP, HOME] if a[:2] == ["ui", "describe-all"] else {}))
    ic.to_device("U", 1, 1)
    t, _mode, _w, _h = ic._CACHE["U"]
    assert ic.time.time() - t > ic.TTL - ic.RETRY_AFTER - 1   # expires in seconds, not minutes


def test_the_confirmed_direction_survives_a_restart(tmp_path):
    """A covered screen (a dialog: idb sees two elements) cannot be measured; after a
    backend restart the in-memory direction was gone and it GUESSED -- the Inspector
    showed the iPad upside down (2026-10-06). It is kept on disk now."""
    ic._save_good("IPAD", "ccw")
    ic._GOOD.clear()                       # a new process
    ic._load_good()
    assert ic._GOOD["IPAD"] == "ccw"


# -- a dialog covers the screen: the screenshot's text decides the turn --------------

def _fake_ocr(monkeypatch, outputs):
    """outputs: what the reader returns for the 'ccw' turn, then the 'cw' turn."""
    import types
    from PIL import Image
    from automation.scenarios import screen_text
    monkeypatch.setattr(screen_text, "ocr_binary", lambda: "/fake/ocr")
    seq = iter(outputs)

    def run(args, **kw):
        if args[0] == "xcrun":
            Image.new("RGB", (834, 1210), "white").save(args[-1])
            return types.SimpleNamespace(stdout="")
        return types.SimpleNamespace(stdout=next(seq))
    monkeypatch.setattr(ic.subprocess, "run", run)


def _lines(*texts):
    return "".join(f"0.1\t0.1\t0.2\t0.05\t{t}\n" for t in texts)


def test_the_turn_whose_text_reads_wins(monkeypatch):
    # Measured 2026-10-06, split dialog: upside down reads as gibberish.
    _fake_ocr(monkeypatch, [_lines("> SSL8", "ieioi", "aav", "s isang"),
                            _lines("Roopa D", "Home", "ORDER SUMMARY", "Select All", "History")])
    assert ic._measure_by_text("IPAD") and ic._measure_by_text.last == "cw"


def test_no_clear_winner_is_not_a_measurement(monkeypatch):
    _fake_ocr(monkeypatch, [_lines("Home", "Menu"), _lines("Home", "Menu")])
    assert not ic._measure_by_text("IPAD") and ic._measure_by_text.last == ""
