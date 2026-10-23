"""Crash reports reduced to what a human reads first.

TestRun.crash_detected existed for months with nothing filling it. Shape below is a
real .ips from this machine: a JSON header line, then a JSON body.
"""
import json, os, time
from automation.evidence.crash_report import summarise, recent_for_app

HEAD = {"app_name": "VyaBusinessiPad", "app_version": "1.0.1",
        "timestamp": "2026-09-02 19:02:26.00 +0530"}
BODY = {
    "procName": "VyaBusinessiPad",
    "termination": {"indicator": "Terminating app due to uncaught exception 'NSInvalidArgument'"},
    "exception": {"type": "EXC_CRASH", "signal": "SIGABRT"},
    "usedImages": [{"name": "VyaBusinessiPad"}, {"name": "CoreFoundation"}],
    "threads": [
        {"frames": [{"imageIndex": 1, "symbol": "not_the_crash"}]},
        {"triggered": True, "frames": [
            {"imageIndex": 0, "symbol": "-[RCTFatal]"},
            {"imageIndex": 1, "imageOffset": 4660},
        ]},
    ],
}
IPS = json.dumps(HEAD) + "\n" + json.dumps(BODY)


def test_it_reports_why_the_app_died():
    s = summarise(IPS)
    assert "uncaught exception" in s["reason"]
    assert s["signal"] == "SIGABRT" and s["app"] == "VyaBusinessiPad"


def test_it_reads_the_FAULTING_thread_not_the_first():
    s = summarise(IPS)
    assert s["frames"][0] == "VyaBusinessiPad -[RCTFatal]"
    assert not any("not_the_crash" in f for f in s["frames"])


def test_a_frame_without_a_symbol_falls_back_to_its_offset():
    assert summarise(IPS)["frames"][1] == "CoreFoundation 0x1234"


def test_a_non_ips_file_is_ignored_not_crashed_on():
    assert summarise("this is not json") is None
    assert summarise("") is None


def test_only_recent_crashes_count(tmp_path):
    """A run must not be blamed for a crash from last week."""
    old = tmp_path / "VyaBusinessiPad-old.ips"
    old.write_text(IPS)
    os.utime(old, (time.time() - 86400, time.time() - 86400))
    assert recent_for_app("VyaBusinessiPad", within_seconds=600, directory=str(tmp_path)) == []

    new = tmp_path / "VyaBusinessiPad-new.ips"
    new.write_text(IPS)
    got = recent_for_app("VyaBusinessiPad", within_seconds=600, directory=str(tmp_path))
    assert len(got) == 1 and got[0]["file"] == "VyaBusinessiPad-new.ips"


def test_another_apps_crash_is_not_ours(tmp_path):
    (tmp_path / "SomeOtherApp.ips").write_text(IPS)
    assert recent_for_app("VyaBusinessiPad", directory=str(tmp_path)) == []


def test_a_missing_directory_is_not_an_error():
    assert recent_for_app("App", directory="/nope/nothing/here") == []
