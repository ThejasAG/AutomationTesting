"""The idb label matcher must be token-strict: it has to find the restaurant card
whose label merely CONTAINS the phrase, without ever confusing NylaiKitchen2 with
the separate NylaiKitchen2Sub / NylaiKitchen2Fav controls."""
from unittest.mock import patch

from automation.intelligence.scenario_runner import ScenarioRunner


def _el(label, x=10, y=20, w=100, h=40):
    return {"AXLabel": label, "frame": {"x": x, "y": y, "width": w, "height": h}}


def _find(name, labels):
    r = ScenarioRunner.__new__(ScenarioRunner)          # no driver needed
    with patch.object(ScenarioRunner, "_idb_all", lambda self: [_el(l) for l in labels]):
        return r._idb_element(name)


def test_exact_label_wins():
    assert _find("Home", ["Wallet", "Home"]) == (60, 40)


def test_contained_token_matches():
    assert _find("NylaiKitchen2", ["card-container-outer-layer NylaiKitchen2 4.5 ★"]) == (60, 40)


def test_sibling_ids_are_not_the_same_token():
    assert _find("NylaiKitchen2", ["NylaiKitchen2Sub", "NylaiKitchen2Fav"]) is None


def test_ambiguous_is_rejected():
    assert _find("NylaiKitchen2", ["outer NylaiKitchen2 a", "inner NylaiKitchen2 b"]) is None


def test_regex_metacharacters_in_a_phrase_are_literal():
    assert _find("pasta-category", ["row pasta-category"]) == (60, 40)


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("ok", n)


# ── LogBox toast: the thing that was eating taps ─────────────────────────────
# A collapsed toast is ONE GenericElement with no Dismiss control, sitting over
# BOOK NOW. Tapping its centre expands LogBox; only the ✕ at the right end of its
# own frame closes it.

def _toast(label="8 Possible Unhandled Promise Rejection (id: 0):"):
    return {"type": "GenericElement", "AXLabel": label,
            "frame": {"x": 10, "y": 801.6, "width": 382, "height": 48}}


def test_collapsed_toast_is_detected_without_the_word_logbox():
    r = ScenarioRunner.__new__(ScenarioRunner)
    assert r._logbox_toast([_toast()]) is not None


def test_real_app_elements_are_not_mistaken_for_a_toast():
    r = ScenarioRunner.__new__(ScenarioRunner)
    assert r._logbox_toast([_el("bookAppoitment"), _el("Wallet")]) is None


def test_dismiss_aims_at_the_x_not_the_centre():
    r = ScenarioRunner.__new__(ScenarioRunner)
    taps = []
    with patch.object(ScenarioRunner, "_device_udid", lambda self: "UDID"), \
         patch("subprocess.run", lambda cmd, **k: taps.append((int(cmd[-2]), int(cmd[-1])))), \
         patch("time.sleep", lambda *_: None), \
         patch.object(ScenarioRunner, "_invalidate_source", lambda self: None):
        assert r._dismiss_toast(_toast()) is True
    (x, y), = taps
    assert x == 372 and 801 < y < 850          # right end of the frame, not (201, 825)
