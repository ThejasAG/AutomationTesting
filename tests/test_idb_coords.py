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
def _fresh():
    ic.forget()
    yield
    ic.forget()


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
