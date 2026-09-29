"""The inspector endpoint's contract: tree + screen + what cannot be tapped."""
from pathlib import Path

SRC = Path("automation/api/v1/routers/inspector.py").read_text()
PAGE = Path("automation/dashboard/src/pages/InspectorPage.tsx").read_text()


def test_every_route_requires_auth():
    """A UI tree names every control in the app — not public; and /tap drives the
    device. All three routes (tree, inspect, tap) need a signed-in user."""
    assert SRC.count("dependencies=[Depends(get_current_user)]") == 3


def test_tap_uses_the_measured_rotation():
    """A fixed rotation formula was right for one iPad orientation only."""
    assert "to_device(udid, body.x, body.y)" in SRC


def test_orientation_is_reported():
    """Landscape means idb's coordinates are rotated from where a tap lands — the
    bug that made every iPad coordinate tap miss by ~240pt."""
    assert '"landscape" if w > h else "portrait"' in SRC


def test_a_dead_device_is_a_502_not_a_500():
    assert "Could not read the UI tree" in SRC
    assert "is the device booted and an app in the" in SRC


def test_the_page_scales_frames_from_points_to_pixels():
    """The screenshot is in pixels, frames in points; drawing them 1:1 is wrong."""
    assert "VIEW_W / tree.screen.width" in PAGE


def test_the_page_marks_unreachable_elements():
    assert "problemLabels" in PAGE and "Cannot be tapped as drawn" in PAGE


def test_the_screenshot_is_turned_by_the_measured_direction():
    """A fixed -90deg on the page drew the iPad upside down whenever the simulator
    was turned the other way (measured 2026-09-29: 'ccw', page showed it inverted)."""
    assert "idb_coords.forget(udid)" in SRC and "idb_coords.upright(img, m)" in SRC


def test_upright_puts_each_app_point_where_a_tap_lands():
    """The rotated screenshot and to_device must agree: the pixel a tap at app
    point (x, y) lands on must sit at (x, y) in the upright image."""
    from PIL import Image
    from automation.scenarios.idb_coords import _MODES, upright
    w, h = 12, 8                                   # landscape app, points
    for mode in ("ccw", "cw"):
        x, y = 9, 2
        px, py = _MODES[mode](x, y, w, h)          # device (portrait) point
        raw = Image.new("RGB", (h, w), "white")    # portrait framebuffer
        raw.putpixel((int(px), int(py)), (255, 0, 0))
        up = upright(raw, mode)
        assert up.size == (w, h)
        red = [(i, j) for i in range(w) for j in range(h) if up.getpixel((i, j)) == (255, 0, 0)]
        assert len(red) == 1 and abs(red[0][0] - x) <= 1 and abs(red[0][1] - y) <= 1, (mode, red)
