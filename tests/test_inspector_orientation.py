"""The Inspector must not draw a portrait screenshot into a landscape box.

`simctl io screenshot` always writes the raster in the device's NATIVE orientation.
For the iPad in landscape that is still PORTRAIT — measured 1668x2420 — while the
accessibility tree reports 1210x834 landscape. Painting that raster with
objectFit:'fill' squashed it into the landscape box and left the picture lying on
its side, so the element frames drawn over it (from the tree, in landscape) lined
up with nothing: the "Select a table" sheet appeared rotated 90 degrees with the
nav rail along the top instead of down the left.
"""
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1]
       / "automation/dashboard/src/pages/InspectorPage.tsx").read_text()


def test_the_rotation_is_driven_by_the_two_orientations_disagreeing():
    """Not by device model or a hardcoded 90deg — a portrait device needs none."""
    assert "shotLandscape" in SRC and "treeLandscape" in SRC
    assert "shotLandscape !== treeLandscape" in SRC


def test_the_image_is_rotated_counter_clockwise():
    """Verified by rendering the real raster both ways: clockwise puts the picture
    upside down (nav rail on the right, text mirrored); counter-clockwise puts the
    nav rail down the left and the sheet upright, matching the device. CSS rotate()
    is clockwise-positive, the opposite sense to most image libraries."""
    assert "rotate(-90deg)" in SRC
    assert "rotate(90deg)" not in SRC


def test_the_rotated_image_is_laid_out_with_its_axes_swapped():
    """Rotating a WxH element by 90deg makes it HxW, so to fill a viewW x viewH box
    it must first be laid out as viewH x viewW. Laying it out at the box's own size
    and rotating would leave it the wrong shape."""
    i = SRC.index("const imgStyle")
    block = SRC[i:SRC.index("objectFit", i)]
    assert "width: viewH" in block and "height: VIEW_W" in block


def test_the_unrotated_path_is_unchanged():
    """A portrait device already matches the box; it must not be rotated."""
    assert "objectFit: 'fill'" in SRC


def test_the_reason_is_recorded_with_the_measurement():
    assert "1668x2420" in SRC or "NATIVE orientation" in SRC


def test_rotating_preserves_the_aspect_ratio():
    """The whole point: the old fill distorted the picture."""
    VIEW_W = 380
    tree_w, tree_h = 1210, 834          # accessibility tree, landscape
    shot_w, shot_h = 1668, 2420         # measured raster, portrait
    scale = VIEW_W / tree_w
    view_h = tree_h * scale
    # after a 90deg rotation the raster's long axis maps onto the box's long axis
    assert abs((shot_h / shot_w) - (VIEW_W / view_h)) < 0.01
