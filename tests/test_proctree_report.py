"""Observation reporting: aggregates the audit trail, touches no process.

Reads only the registry and the audit records the sweep already writes. Nothing
here scans the machine, and nothing here can signal.
"""
import contextlib
import json
import os
import subprocess
import sys
import time

import pytest

from automation.utils import proctree


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    """Never read or write the real registry / audit trail."""
    monkeypatch.setattr(proctree, "REGISTRY_PATH", str(tmp_path / "proc_registry.json"))
    monkeypatch.setattr(proctree, "AUDIT_PATH", str(tmp_path / "audit.jsonl"))


@contextlib.contextmanager
def _no_signals():
    """Any signal at all fails the test — reporting must be inert."""
    real_kill, real_killpg = os.kill, os.killpg
    calls = []

    def spy_kill(pid, sig, *a, **k):
        if sig != 0:
            calls.append(("kill", pid, sig))
            raise AssertionError(f"report sent kill({pid}, {sig})")
        return real_kill(pid, sig, *a, **k)

    def spy_killpg(pgid, sig, *a, **k):
        calls.append(("killpg", pgid, sig))
        raise AssertionError(f"report sent killpg({pgid}, {sig})")

    os.kill, os.killpg = spy_kill, spy_killpg
    try:
        yield calls
    finally:
        os.kill, os.killpg = real_kill, real_killpg
    assert calls == []


def _rec(pid, verdict, **over):
    rec = {
        "ts": 1_000_000.0, "pid": pid, "pgid": pid, "job_id": f"job-{pid}",
        "kind": "appium", "verdict": verdict, "action": "DRY_RUN",
        "identity_match": True, "job_active": False,
        "age_sec": 3600.0, "recorded_at": 990_000.0, "reason": "",
    }
    rec.update(over)
    return rec


def _write_audit(records, path=None):
    path = path or proctree.AUDIT_PATH
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _counts(records):
    with _no_signals():
        return proctree.summarize(proctree.read_audit())["counts"]


# ── 1. Nothing observed ──────────────────────────────────────────────────────

def test_empty_audit_trail_reports_nothing():
    with _no_signals():
        summary = proctree.summarize(proctree.read_audit())
    assert summary == {
        "observations": 0, "entries_examined": 0,
        "period_start": None, "period_end": None,
        "counts": {}, "would_reap_by_kind": {}, "would_reap_by_job": {},
        "would_reap_by_age": {},
    }
    assert "Observations       : 0" in proctree.observation_report()


def test_missing_audit_file_is_not_an_error():
    with _no_signals():
        assert proctree.read_audit(path="/nonexistent/audit.jsonl") == []


# ── 2-6. Each classification lands in its own bucket ─────────────────────────

def test_mixed_classifications_are_counted_separately():
    _write_audit([
        _rec(101, "WOULD_REAP"),
        _rec(102, "DO_NOT_REAP", job_active=True),
        _rec(103, "DO_NOT_REAP", identity_match=False),
        _rec(104, "TOO_YOUNG", age_sec=60.0),
        _rec(105, "NEVER_REAP", kind="metro"),
        _rec(106, "UNKNOWN", job_active=None),
        _rec(107, "REAP_FAILED", action="REAP_FAILED"),
    ])
    assert _counts(None) == {
        "WOULD_REAP": 1,
        "DO_NOT_REAP - active job": 1,
        "DO_NOT_REAP - identity mismatch": 1,
        "TOO_YOUNG": 1,
        "NEVER_REAP - Metro": 1,
        "UNKNOWN": 1,
        "REAP_FAILED": 1,
    }


def test_metro_is_reported_as_its_own_exclusion():
    """A Metro entry must never be aggregated into WOULD_REAP."""
    _write_audit([_rec(201, "NEVER_REAP", kind="metro"), _rec(202, "WOULD_REAP")])
    with _no_signals():
        summary = proctree.summarize(proctree.read_audit())
    assert summary["counts"]["NEVER_REAP - Metro"] == 1
    assert "metro" not in summary["would_reap_by_kind"]
    assert summary["would_reap_by_job"] == {"job-202": 1}


def test_active_job_and_identity_mismatch_are_distinguished():
    """Both are DO_NOT_REAP, and the operator needs to tell them apart."""
    _write_audit([
        _rec(301, "DO_NOT_REAP", job_active=True, identity_match=True),
        _rec(302, "DO_NOT_REAP", job_active=False, identity_match=False),
    ])
    counts = _counts(None)
    assert counts["DO_NOT_REAP - active job"] == 1
    assert counts["DO_NOT_REAP - identity mismatch"] == 1


def test_categories_come_from_fields_not_reason_text():
    """Rewording a reason string must not move an entry between categories."""
    _write_audit([_rec(401, "DO_NOT_REAP", job_active=True, reason="totally different wording")])
    assert _counts(None) == {"DO_NOT_REAP - active job": 1}


def test_too_young_entries_are_never_would_reap():
    _write_audit([_rec(501, "TOO_YOUNG", age_sec=30.0) for _ in range(3)])
    with _no_signals():
        summary = proctree.summarize(proctree.read_audit())
    assert summary["counts"] == {"TOO_YOUNG": 1}, "same entry observed 3x = 1 entry"
    assert summary["would_reap_by_age"] == {}


# ── 7-8. Aggregation ─────────────────────────────────────────────────────────

def test_would_reap_is_grouped_by_kind_job_and_age():
    _write_audit([
        _rec(601, "WOULD_REAP", job_id="job-a", age_sec=1800.0),      # 15m-1h
        _rec(602, "WOULD_REAP", job_id="job-a", age_sec=7200.0),      # 1h-6h
        _rec(603, "WOULD_REAP", job_id="job-b", age_sec=90000.0),     # 6h+
        _rec(604, "WOULD_REAP", job_id="job-b", kind="node", age_sec=None),
    ])
    with _no_signals():
        summary = proctree.summarize(proctree.read_audit())
    assert summary["would_reap_by_kind"] == {
        "appium": {"job-a": 2, "job-b": 1},
        "node": {"job-b": 1},
    }
    assert summary["would_reap_by_job"] == {"job-a": 2, "job-b": 2}
    assert summary["would_reap_by_age"] == {
        "15m-1h": 1, "1h-6h": 1, "6h+": 1, "unknown": 1,
    }
    text = proctree.format_summary(summary)
    assert "WOULD_REAP" in text and "job-a: 2" in text and "6h+: 1" in text


def test_repeated_observations_of_one_entry_count_once():
    """Every sweep re-observes the same orphan; the report must not inflate."""
    _write_audit([_rec(701, "WOULD_REAP", ts=1_000_000.0 + i * 900) for i in range(40)])
    with _no_signals():
        summary = proctree.summarize(proctree.read_audit())
    assert summary["observations"] == 40
    assert summary["entries_examined"] == 1
    assert summary["counts"] == {"WOULD_REAP": 1}
    assert summary["period_end"] - summary["period_start"] == 39 * 900


def test_recycled_pid_is_not_merged_with_the_old_entry():
    """Same pid, different recorded_at = a different tracked process."""
    _write_audit([
        _rec(801, "WOULD_REAP", recorded_at=1.0, job_id="job-old"),
        _rec(801, "WOULD_REAP", recorded_at=2.0, job_id="job-new"),
    ])
    with _no_signals():
        summary = proctree.summarize(proctree.read_audit())
    assert summary["entries_examined"] == 2
    assert summary["would_reap_by_job"] == {"job-old": 1, "job-new": 1}


def test_multiple_jobs_stay_isolated_in_the_report():
    _write_audit([
        _rec(901, "WOULD_REAP", job_id="job-1"),
        _rec(902, "DO_NOT_REAP", job_id="job-2", job_active=True),
        _rec(903, "NEVER_REAP", job_id="job-3", kind="metro"),
    ])
    with _no_signals():
        summary = proctree.summarize(proctree.read_audit())
    assert summary["would_reap_by_job"] == {"job-1": 1}
    assert summary["counts"]["DO_NOT_REAP - active job"] == 1
    assert summary["counts"]["NEVER_REAP - Metro"] == 1


def test_since_filter_bounds_the_observation_period():
    _write_audit([
        _rec(1001, "WOULD_REAP", ts=1_000.0, job_id="old"),
        _rec(1002, "WOULD_REAP", ts=9_000.0, job_id="recent"),
    ])
    with _no_signals():
        rows = proctree.read_audit(since=5_000.0)
    assert [r["job_id"] for r in rows] == ["recent"]


# ── 9-10. Robustness and inertness ───────────────────────────────────────────

def test_malformed_audit_records_are_skipped_not_fatal():
    with open(proctree.AUDIT_PATH, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(_rec(1101, "WOULD_REAP")) + "\n")
        fh.write("{ truncated mid-write\n")
        fh.write("\n")
        fh.write("[1, 2, 3]\n")                      # valid JSON, wrong shape
        fh.write(json.dumps(_rec(1102, "TOO_YOUNG")) + "\n")
    with _no_signals():
        summary = proctree.summarize(proctree.read_audit())
    assert summary["observations"] == 2
    assert summary["counts"] == {"WOULD_REAP": 1, "TOO_YOUNG": 1}


def test_records_missing_fields_do_not_crash_the_report():
    _write_audit([{"ts": 1.0, "verdict": "WOULD_REAP"}, {}, {"pid": 5}])
    with _no_signals():
        text = proctree.observation_report()
    assert "Observations" in text


def test_report_generation_sends_no_signals_and_kills_nothing():
    """The whole point: this is an inert read of two files."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _write_audit([_rec(proc.pid, "WOULD_REAP"), _rec(proc.pid + 1, "REAP_FAILED")])
        with _no_signals():
            summary = proctree.summarize(proctree.read_audit())
            proctree.format_summary(summary)
            proctree.observation_report()
        assert summary["counts"]["WOULD_REAP"] == 1
        assert proc.poll() is None, "reporting killed a process"
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_report_never_reads_the_machine_process_table():
    """No unregistered process can enter the report: it only sees audit records."""
    _write_audit([_rec(1201, "WOULD_REAP")])
    with _no_signals():
        summary = proctree.summarize(proctree.read_audit())
    assert summary["entries_examined"] == 1
    assert os.getpid() not in summary["would_reap_by_job"]
    assert list(summary["would_reap_by_job"]) == ["job-1201"]


def test_sweep_writes_audit_records_that_the_report_can_read():
    """End to end: a real dry-run sweep produces records the summary aggregates."""
    proc = proctree.spawn_tracked(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        job_id="job-observed", kind="appium",
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with _no_signals():
            rep = proctree.sweep_orphans(dry_run=True, active=set(),
                                         now=time.time() + 10_000)
            summary = proctree.summarize(proctree.read_audit())
        assert rep["reaped"] == [] and rep["would_reap"] == [proc.pid]
        assert summary["counts"] == {"WOULD_REAP": 1}
        assert summary["would_reap_by_kind"] == {"appium": {"job-observed": 1}}
        assert proc.poll() is None, "the dry-run sweep killed the process"
    finally:
        proctree.reap_pid(proc.pid, timeout=5)
