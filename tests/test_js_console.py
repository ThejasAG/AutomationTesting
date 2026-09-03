"""Only the log lines that explain a failure — from the real Metro output.

The line this exists for sat in /tmp/metro8084.log for hours while every run
reported "element not found":
    Invariant Violation: Module AppRegistry is not a registered callable module
"""
from automation.evidence.js_console import extract_js_errors, read_js_console

REAL = """
 LOG  Adding times: 15:30 0 1 hr
 ERROR  Warning: Each child in a list should have a unique "key" prop.
 ERROR  no valid “aps-environment” entitlement string found for application
 ERROR  Invariant Violation: Module AppRegistry is not a registered callable module
Error: Unable to resolve module react-native-compressor from App/Utils/videoUploadTracker.js
 LOG  Failed to fetch wishlisted products: [AxiosError: Request failed with status code 401]
 ERROR  Warning: Failed prop type: Invalid prop `name`
"""


def test_it_keeps_the_line_that_ends_the_investigation():
    out = extract_js_errors(REAL)
    assert any("AppRegistry is not a registered callable module" in l for l in out)
    assert any("Unable to resolve module react-native-compressor" in l for l in out)


def test_it_drops_the_noise_that_is_on_every_screen():
    """These match the signal patterns but have never explained a failure."""
    out = "\n".join(extract_js_errors(REAL))
    assert "aps-environment" not in out
    assert "Each child in a list" not in out
    assert "Failed prop type" not in out


def test_a_plain_log_line_is_not_evidence():
    assert not any("Adding times" in l for l in extract_js_errors(REAL))


def test_a_failed_network_call_is_kept():
    assert any("status code 401" in l for l in extract_js_errors(REAL))


def test_it_returns_the_TAIL_not_the_head():
    """A failure is explained by what happened just before it."""
    text = "\n".join(f" ERROR  line {i}" for i in range(100))
    out = extract_js_errors(text, max_lines=5)
    assert len(out) == 5 and out[-1].endswith("line 99")


def test_ansi_colour_is_stripped():
    assert extract_js_errors("\x1b[31m ERROR  Invariant Violation: x\x1b[0m") == [
        "ERROR  Invariant Violation: x"]


def test_a_missing_log_is_not_an_error(tmp_path):
    """Absent evidence must never be the thing that fails a run."""
    assert read_js_console(None) == []
    assert read_js_console(str(tmp_path / "nope.log")) == []
