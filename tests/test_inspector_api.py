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
