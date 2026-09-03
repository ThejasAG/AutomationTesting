"""idb REPORTS app-space coordinates but TAPS in device space. On a landscape iPad
those differ by a 90-degree rotation, so every coordinate tap landed ~240pt off.

Measured on iPad Pro 11: app frame 1210x834 (landscape), screenshot 834x1210
(portrait). 'addNewEvent' reported at app (725, 723) is really at device (723, 485)
-- confirmed live: tapping the rotated point opened the New Appointment form, the
raw point did nothing.
"""
from automation.scenarios.cross_app_flows import FlowRunner


def _runner(w, h):
    r = object.__new__(FlowRunner)
    r._describe_app_frame = lambda udid: {"width": w, "height": h}
    return r


def _rotate(r, x, y, w, h, monkey):
    monkey.setattr(r, "_rotate_for_device", FlowRunner._rotate_for_device.__get__(r))
    return r._rotate_for_device(x, y, "udid")


def test_landscape_app_is_rotated(monkeypatch):
    r = object.__new__(FlowRunner)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: type("P", (), {"stdout": '[{"type":"Application","frame":'
                                                 '{"width":1210,"height":834}}]'})())
    assert r._rotate_for_device(725, 723, "u") == (723, 1210 - 725)   # -> (723, 485)


def test_portrait_app_is_left_alone(monkeypatch):
    r = object.__new__(FlowRunner)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: type("P", (), {"stdout": '[{"type":"Application","frame":'
                                                 '{"width":393,"height":852}}]'})())
    assert r._rotate_for_device(200, 560, "u") == (200, 560)


def test_an_unreadable_tree_taps_unchanged(monkeypatch):
    """Never guess a rotation we cannot see — an off-by-90 tap is worse than a raw one."""
    r = object.__new__(FlowRunner)
    monkeypatch.setattr("subprocess.run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert r._rotate_for_device(10, 20, "u") == (10, 20)
