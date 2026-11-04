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


# ── only what this segment logged (2026-10-27) ───────────────────────────────

def test_lines_older_than_the_segment_are_not_evidence(tmp_path):
    """MEASURED 2026-10-27: an old 'JavaScript heap out of memory' line in the Metro
    log was attached to three unrelated failures as their cause."""
    log = tmp_path / "metro_8084.log"
    log.write_text("FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory\n"
                   " ERROR  Invariant Violation: old\n")
    mark = log.stat().st_size
    with open(log, "a") as f:
        f.write(" LOG  ok\n ERROR  TypeError: undefined is not an object (evaluating 'x.y')\n")
    got = read_js_console(str(log), since=mark)
    assert got == ["ERROR  TypeError: undefined is not an object (evaluating 'x.y')"]
    assert any("heap out of memory" in g for g in read_js_console(str(log)))   # no mark: old behaviour


def test_a_rolled_log_is_read_from_its_start(tmp_path):
    log = tmp_path / "metro_8084.log"
    log.write_text(" ERROR  TypeError: after the roll\n")
    assert read_js_console(str(log), since=10_000_000) == ["ERROR  TypeError: after the roll"]


def test_evidence_comes_from_the_failing_apps_own_metro_log(tmp_path, monkeypatch):
    from automation.projects import builder
    from automation.scenarios import cross_app_flows as caf
    monkeypatch.setattr(builder, "METRO_LOG_DIR", str(tmp_path))
    consumer, business = tmp_path / "metro_8084.log", tmp_path / "metro_8083.log"
    consumer.write_text(" ERROR  old consumer line\n")
    business.write_text(" ERROR  business line, newest file\n")
    runner = caf.FlowRunner.__new__(caf.FlowRunner)
    runner.consumer_bundle = "org.vyapy.sarls.vyaconsumerstaging"
    runner.business_bundle = "org.vyapy.sarls.vyabusinessipadstaging"
    runner.devices = {}
    runner._cur_udid = ""
    runner._metro_marks = runner._metro_log_sizes()
    with open(consumer, "a") as f:
        f.write(" ERROR  TypeError: consumer, during the segment\n")
    monkeypatch.setattr("automation.evidence.device_log.capture", lambda *a, **k: [])
    monkeypatch.setattr("automation.evidence.crash_report.recent_for_app", lambda *a, **k: [])
    js = [n for n in runner._collect_evidence("consumer") if "[evidence] js:" in n]
    assert js == ["    [evidence] js: ERROR  TypeError: consumer, during the segment"]
