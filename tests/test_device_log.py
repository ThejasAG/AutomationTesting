"""Device log, filtered to what explains a failure.

Real shape from `xcrun simctl spawn <udid> log show --style compact`.
"""
from automation.evidence.device_log import interesting_lines, capture

REAL = """Timestamp               Ty Process[PID:TID]
2026-09-02 19:02:24.184 Df VyaBusinessiPad[8931:28be9a] [BackgroundTask] Will add backgroundTask
2026-09-02 19:02:24.185 Df VyaBusinessiPad[8931:28be9a] Snapshotting a view that has not rendered
2026-09-02 19:02:25.001 Er VyaBusinessiPad[8931:28be9a] Failed to load resource: 500
2026-09-02 19:02:25.100 Df VyaBusinessiPad[8931:28be9a] no valid aps-environment entitlement
2026-09-02 19:02:26.900 Fa VyaBusinessiPad[8931:28be9a] *** Terminating app due to uncaught exception
2026-09-02 19:02:27.000 Df VyaBusinessiPad[8931:28be9a] RCTFatal called
"""


def test_errors_and_faults_are_kept():
    out = interesting_lines(REAL)
    assert any("Failed to load resource: 500" in l for l in out)
    assert any("Terminating app due to uncaught exception" in l for l in out)


def test_a_crash_is_kept_even_at_debug_level():
    """RCTFatal is logged Df but is exactly what you want to see."""
    assert any("RCTFatal" in l for l in interesting_lines(REAL))


def test_launch_noise_is_dropped():
    out = "\n".join(interesting_lines(REAL))
    for noise in ("BackgroundTask", "aps-environment", "Snapshotting a view"):
        assert noise not in out


def test_the_header_row_is_not_evidence():
    assert not any("Timestamp" in l for l in interesting_lines(REAL))


def test_it_is_bounded_and_tail_biased():
    text = "\n".join(
        f"2026-09-02 19:02:{i%60:02d}.000 Er App[1:2] boom {i}" for i in range(200))
    out = interesting_lines(text, max_lines=10)
    assert len(out) == 10 and out[-1].endswith("boom 199")


def test_missing_device_returns_nothing_rather_than_raising():
    assert capture("", "App") == []
    assert capture("not-a-udid", "App", timeout=5) == []
