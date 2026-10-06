"""The Inspector must not draw a portrait screenshot into a landscape box.

`simctl io screenshot` always writes the raster in the device's NATIVE orientation.
For the iPad in landscape that is still PORTRAIT — measured 1668x2420 — while the
accessibility tree reports 1210x834 landscape. Painting that raster with
objectFit:'fill' squashed it into the landscape box and left the picture lying on
its side, so the element frames drawn over it (from the tree, in landscape) lined
up with nothing: the "Select a table" sheet appeared rotated 90 degrees with the
nav rail along the top instead of down the left.
"""
import re
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


# ── the verdict must agree with itself ──────────────────────────────────────

def test_the_heading_is_not_an_alarm_on_a_clean_screen():
    """It used to be a fixed red "Cannot be tapped as drawn" rendered even when the
    count was zero, directly above "Nothing is covered or out of reach on this
    screen." — a healthy screen announced itself as broken, and a real finding
    looked exactly like a clean one."""
    assert "Every element can be tapped" in SRC
    # Strip JSX {/* ... */} comments, which quote the old heading verbatim.
    code = re.sub(r"\{/\*.*?\*/\}", "", SRC, flags=re.S)
    i = code.index("Cannot be tapped as drawn")
    head = code[max(0, i - 400):i]
    assert "problem_count > 0" in head, \
        "the alarm heading must be conditional on there being problems"


def test_the_problem_count_is_shown_when_there_are_problems():
    assert "({tree.problem_count})" in SRC


# ── an element nothing can name is also untappable ──────────────────────────

from automation.inspector.overlap import report

_APP = {"type": "Application", "AXLabel": "App",
        "frame": {"x": 0, "y": 0, "width": 1210, "height": 834}}


def _probs(extra):
    return report([_APP] + extra, 1210, 834)


def test_an_unnamed_tap_target_is_reported():
    """Covered and off-screen are about geometry; this is about whether the
    automation can NAME the thing at all. A chip with no accessibility id can only
    be reached by coordinate, which breaks the moment the screen re-lays out."""
    p = _probs([{"type": "GenericElement", "AXLabel": "",
                 "frame": {"x": 100, "y": 200, "width": 80, "height": 40}}])
    assert [i["kind"] for x in p for i in x["issues"]] == ["unnamed"]


def test_a_named_element_is_not_reported():
    assert _probs([{"type": "GenericElement", "AXLabel": "I1",
                    "frame": {"x": 300, "y": 200, "width": 80, "height": 40}}]) == []


def test_a_full_screen_backdrop_is_not_a_tap_target():
    """The page behind a modal is unnamed by nature and reporting it is noise."""
    assert _probs([{"type": "Other", "AXLabel": "",
                    "frame": {"x": 0, "y": 0, "width": 1210, "height": 834}}]) == []


def test_a_speck_is_not_a_tap_target():
    """Below finger size it is decoration, not a control."""
    assert _probs([{"type": "GenericElement", "AXLabel": "",
                    "frame": {"x": 5, "y": 5, "width": 6, "height": 6}}]) == []


def test_the_check_is_not_hidden_by_only_labelled():
    """only_labelled=True is the endpoint's default and filters the COVERAGE
    subjects. Gating the unnamed check on it would hide exactly the elements it
    exists to find."""
    import inspect as _i
    from automation.inspector import overlap
    src = _i.getsource(overlap.report)
    i = src.index('"unnamed"')
    assert "if not only_labelled:" not in src[:i]
