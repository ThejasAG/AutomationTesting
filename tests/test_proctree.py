"""Process-tree teardown: covers the leak where killing Appium's parent pid left
the xcodebuild/WDA descendants alive. Uses harmless `sleep` processes, never Appium.
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
def isolated_registry(tmp_path, monkeypatch):
    """Never let a test touch the real registry or the real audit trail."""
    monkeypatch.setattr(proctree, "REGISTRY_PATH", str(tmp_path / "proc_registry.json"))
    monkeypatch.setattr(proctree, "AUDIT_PATH", str(tmp_path / "audit.jsonl"))


def _spawn_sleeper(job_id="job-test", kind="test"):
    return proctree.spawn_tracked(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        job_id=job_id, kind=kind,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _wait_gone(pid, timeout=15):
    """True once *pid* is no longer a running process (zombies count as gone)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not proctree._alive(pid):
            return True
        time.sleep(0.1)
    return not proctree._alive(pid)


def test_spawn_tracked_records_the_process():
    proc = _spawn_sleeper(job_id="job-a")
    try:
        entry = proctree._load()[str(proc.pid)]
        assert entry["job_id"] == "job-a"
        assert entry["kind"] == "test"
        assert entry["pgid"] == proc.pid, "must lead its own group"
        assert entry["identity"], "start time recorded for PID-reuse checks"
        assert entry["cmd"][0] == sys.executable
    finally:
        proctree.reap_pid(proc.pid, timeout=5)


def test_reap_pid_terminates_the_group_and_clears_the_entry():
    proc = _spawn_sleeper()
    assert proctree.reap_pid(proc.pid, timeout=10) is True
    proc.wait(timeout=5)
    assert _wait_gone(proc.pid)
    assert str(proc.pid) not in proctree._load()


def test_reap_kills_child_and_grandchild():
    """The whole point: descendants die too, not just the process we spawned."""
    script = (
        "import subprocess,sys,time;"
        "g=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
        "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
        "print(c.pid,g.pid,flush=True);"
        "time.sleep(120)"
    )
    proc = proctree.spawn_tracked(
        [sys.executable, "-c", script],
        job_id="job-tree", kind="test",
        stdout=subprocess.PIPE, text=True,
    )
    child_pid, grandchild_pid = (int(x) for x in proc.stdout.readline().split())
    assert proctree._alive(child_pid) and proctree._alive(grandchild_pid)

    assert proctree.reap_pid(proc.pid, timeout=10) is True
    assert _wait_gone(child_pid), "child survived teardown"
    assert _wait_gone(grandchild_pid), "grandchild survived teardown"


def test_reap_refuses_on_identity_mismatch():
    """A recycled PID must never be killed."""
    proc = _spawn_sleeper()
    try:
        def corrupt_identity(entries):
            entries[str(proc.pid)]["identity"] = "Thu Jan  1 00:00:00 1970"
        proctree._update(corrupt_identity)

        assert proctree.reap_pid(proc.pid, timeout=5) is False
        assert proctree._alive(proc.pid), "must not kill a pid whose identity changed"
        assert str(proc.pid) not in proctree._load(), "stale entry dropped"
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_reap_refuses_untracked_pid():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
    )
    try:
        assert proctree.reap_pid(proc.pid, timeout=5) is False
        assert proctree._alive(proc.pid), "must not kill a process we did not spawn"
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_corrupt_registry_fails_closed():
    proc = _spawn_sleeper(job_id="job-corrupt")
    try:
        with open(proctree.REGISTRY_PATH, "w") as fh:
            fh.write("{ this is not json")

        with pytest.raises(proctree.RegistryError):
            proctree._load()
        assert proctree.reap_pid(proc.pid, timeout=5) is False
        assert proctree.reap_job("job-corrupt", timeout=5) == 0
        assert proctree._alive(proc.pid), "corrupt registry must kill nothing"
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_reap_job_only_touches_its_own_job():
    mine = _spawn_sleeper(job_id="job-mine")
    theirs = _spawn_sleeper(job_id="job-theirs")
    try:
        assert proctree.reap_job("job-mine", timeout=10) == 1
        assert mine.wait(timeout=10) is not None, "our job's process exited"
        assert proctree._alive(theirs.pid), "another job's process must be untouched"
    finally:
        proctree.reap_pid(theirs.pid, timeout=5)


def test_atomic_write_leaves_no_temp_files(tmp_path):
    proc = _spawn_sleeper()
    try:
        d = os.path.dirname(proctree.REGISTRY_PATH)
        assert not [f for f in os.listdir(d) if f.endswith(".tmp")]
        json.loads(open(proctree.REGISTRY_PATH).read())
    finally:
        proctree.reap_pid(proc.pid, timeout=5)


def test_reap_kills_detached_descendant_group():
    """The xcodebuild shape: a descendant that setsid()s into its own group.

    appium-webdriveragent spawns xcodebuild with `detached: true`, so it leads its
    own process group and killpg(appium_group) never reaches it. Teardown must
    follow parentage down and take those breakaway groups too.
    """
    script = (
        "import subprocess,sys,time;"
        "d=subprocess.Popen([sys.executable,'-c',"
        "'import subprocess,sys,time;"
        "k=subprocess.Popen([sys.executable,\\\"-c\\\",\\\"import time;time.sleep(120)\\\"]);"
        "print(k.pid,flush=True);time.sleep(120)'],"
        "start_new_session=True,stdout=subprocess.PIPE,text=True);"
        "print(d.pid,d.stdout.readline().strip(),flush=True);"
        "time.sleep(120)"
    )
    proc = proctree.spawn_tracked(
        [sys.executable, "-c", script],
        job_id="job-detached", kind="test",
        stdout=subprocess.PIPE, text=True,
    )
    detached_pid, its_child_pid = (int(x) for x in proc.stdout.readline().split())

    # Precondition: it really did break out of our group.
    assert os.getpgid(detached_pid) == detached_pid != os.getpgid(proc.pid)
    assert detached_pid not in proctree._group_members(proc.pid)

    assert proctree.reap_pid(proc.pid, timeout=10) is True
    assert _wait_gone(detached_pid), "detached descendant survived (the xcodebuild leak)"
    assert _wait_gone(its_child_pid), "detached descendant's child survived (the WDA leak)"


# ── Orphan sweep (dry-run, observational) ────────────────────────────────────

OLD = 1_000_000.0          # a "recorded_at" far in the past
NOW = OLD + 100_000.0


def _entry(pid, **over):
    """A registry entry for a live process, old enough to be sweepable."""
    try:
        pgid = os.getpgid(pid)      # the real group, so the association check passes
    except OSError:
        pgid = pid
    e = {
        "pid": pid, "pgid": pgid, "job_id": f"job-{pid}", "kind": "appium",
        "identity": proctree._proc_identity(pid),
        "cmd": ["npx", "appium"], "recorded_at": OLD,
    }
    e.update(over)
    return e


def _write_registry(entries):
    proctree._update(lambda cur: cur.update({str(e["pid"]): e for e in entries}))


@pytest.fixture
def live_pid():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], stdout=subprocess.DEVNULL,
    )
    yield proc.pid
    proc.kill()
    proc.wait(timeout=5)


@contextlib.contextmanager
def _no_signals():
    """Assert the wrapped code signals nothing.

    os.kill(pid, 0) is a liveness probe, not a signal, and the classifier needs it —
    so it is allowed through to the real call and checked to be signal 0. Any real
    signal, and any killpg at all, fails the test.
    """
    calls = []
    real_kill, real_killpg = os.kill, os.killpg

    def spy_kill(pid, sig, *a, **k):
        calls.append(("kill", pid, sig))
        return real_kill(pid, sig, *a, **k)

    def spy_killpg(pgid, sig, *a, **k):
        calls.append(("killpg", pgid, sig))
        raise AssertionError(f"sweep called killpg({pgid}, {sig})")

    os.kill, os.killpg = spy_kill, spy_killpg
    try:
        yield calls
    finally:
        os.kill, os.killpg = real_kill, real_killpg
    signalled = [c for c in calls if c[0] == "killpg" or c[2] != 0]
    assert not signalled, f"sweep sent signals: {signalled}"


def _sweep(**kw):
    """sweep_orphans() under the zero-signal guarantee."""
    with _no_signals():
        return proctree.sweep_orphans(**kw)


def _verdicts(report):
    return {r["pid"]: r["verdict"] for r in report["entries"]}


def test_sweep_stale_entry_inactive_job_would_reap(live_pid):
    _write_registry([_entry(live_pid, job_id="job-done")])
    rep = _sweep(dry_run=True, active=set(), now=NOW)
    assert _verdicts(rep) == {live_pid: "WOULD_REAP"}
    assert rep["would_reap"] == [live_pid]
    assert rep["signals_sent"] == 0


def test_sweep_active_job_is_not_reapable(live_pid):
    _write_registry([_entry(live_pid, job_id="job-live")])
    rep = _sweep(dry_run=True, active={"job-live"}, now=NOW)
    row = rep["entries"][0]
    assert row["verdict"] == "DO_NOT_REAP" and row["job_active"] is True
    assert "still active" in row["reason"]
    assert rep["would_reap"] == []


def test_sweep_metro_is_never_reaped(live_pid):
    """Metro is excluded even when every other condition says orphan."""
    _write_registry([_entry(live_pid, kind="metro", job_id="job-done")])
    rep = _sweep(dry_run=True, active=set(), now=NOW)
    assert _verdicts(rep) == {live_pid: "NEVER_REAP"}
    assert rep["would_reap"] == []


def test_sweep_identity_mismatch_is_not_reapable(live_pid):
    _write_registry([_entry(live_pid, identity="Thu Jan  1 00:00:00 1970")])
    rep = _sweep(dry_run=True, active=set(), now=NOW)
    row = rep["entries"][0]
    assert row["verdict"] == "DO_NOT_REAP"
    assert "IDENTITY_MISMATCH" in row["reason"]
    assert row["identity_match"] is False


def test_sweep_group_mismatch_is_not_reapable(live_pid):
    """Recorded pgid must still match: no positive association, no reap."""
    _write_registry([_entry(live_pid, pgid=os.getpgid(live_pid) + 999_999)])
    row = _sweep(dry_run=True, active=set(), now=NOW)["entries"][0]
    assert row["verdict"] == "DO_NOT_REAP" and "GROUP_MISMATCH" in row["reason"]


def test_sweep_young_entry_is_too_young(live_pid):
    _write_registry([_entry(live_pid, recorded_at=NOW - 5)])
    row = _sweep(dry_run=True, active=set(), now=NOW)["entries"][0]
    assert row["verdict"] == "TOO_YOUNG"
    assert "minimum" in row["reason"]


def test_sweep_dead_process_is_already_gone():
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=10)
    _write_registry([_entry(dead.pid, identity="whatever")])
    row = _sweep(dry_run=True, active=set(), now=NOW)["entries"][0]
    assert row["verdict"] == "ALREADY_GONE"


def test_sweep_corrupt_registry_fails_closed():
    with open(proctree.REGISTRY_PATH, "w") as fh:
        fh.write("}{ not json")
    rep = _sweep(dry_run=True, active=set(), now=NOW)
    assert rep["available"] is False
    assert "registry unreadable" in rep["unavailable_reason"]
    assert rep["entries"] == [] and rep["would_reap"] == []


def test_sweep_backend_unavailable_fails_closed(live_pid, monkeypatch):
    """An unreachable backend means 'unknown', never 'nothing is running'."""
    def unavailable():
        raise proctree.SweepUnavailable("backend job state unreadable: connection refused")
    monkeypatch.setattr(proctree, "active_job_ids", unavailable)

    _write_registry([_entry(live_pid, job_id="job-done")])
    rep = _sweep(dry_run=True, now=NOW)   # no `active` -> looks it up
    assert rep["available"] is False
    assert rep["would_reap"] == []
    row = rep["entries"][0]
    assert row["verdict"] == "UNKNOWN" and row["job_active"] is None
    assert "failing closed" in row["reason"]


def test_sweep_ignores_unregistered_processes(live_pid):
    """A process we never recorded is invisible to the sweep — no name scanning."""
    _write_registry([_entry(live_pid, job_id="job-done")])
    rep = _sweep(dry_run=True, active=set(), now=NOW)
    assert [r["pid"] for r in rep["entries"]] == [live_pid], "only registry entries considered"
    assert os.getpid() not in rep["would_reap"]


def test_sweep_multiple_jobs_classifies_each_independently(live_pid):
    procs = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                              stdout=subprocess.DEVNULL) for _ in range(3)]
    try:
        a, b, c = (p.pid for p in procs)
        _write_registry([
            _entry(a, job_id="job-running"),                       # active
            _entry(b, job_id="job-done"),                          # orphan
            _entry(c, job_id="job-done", kind="metro"),            # excluded
            _entry(live_pid, job_id="job-done", recorded_at=NOW),  # too young
        ])
        rep = _sweep(dry_run=True, active={"job-running"}, now=NOW)
        assert _verdicts(rep) == {
            a: "DO_NOT_REAP", b: "WOULD_REAP", c: "NEVER_REAP", live_pid: "TOO_YOUNG",
        }
        assert rep["would_reap"] == [b]
    finally:
        for p in procs:
            p.kill()
            p.wait(timeout=5)


def test_sweep_sends_zero_signals(live_pid):
    """The dry-run guarantee. os.kill/os.killpg are stubbed to fail the test."""
    _write_registry([
        _entry(live_pid, job_id="job-done"),
        _entry(live_pid + 1, kind="metro", job_id="job-done"),
    ])
    rep = _sweep(dry_run=True, active=set(), now=NOW)
    assert rep["signals_sent"] == 0
    assert proctree._alive.__module__  # sanity: module intact
    # And the process is still alive, having been classified WOULD_REAP.
    assert subprocess.run(["ps", "-p", str(live_pid)], capture_output=True).returncode == 0


def test_reaping_ships_disabled_by_default():
    """All three switches default to the safe value."""
    assert proctree.PROC_SWEEP_ENABLED is False
    assert proctree.PROC_SWEEP_ALLOW_REAP is False
    assert proctree.PROC_SWEEP_DRY_RUN is True
    allowed, reason = proctree.reaping_allowed()
    assert allowed is False and "DRY_RUN" in reason


@pytest.mark.parametrize("enabled,allow,dry,expected", [
    (False, False, True,  False),
    (True,  True,  True,  False),   # dry-run overrides both enable flags
    (False, True,  False, False),   # not enabled
    (True,  False, False, False),   # not allowed
    (True,  True,  False, True),    # the only armed combination
])
def test_reaping_gate_truth_table(monkeypatch, enabled, allow, dry, expected):
    monkeypatch.setattr(proctree, "PROC_SWEEP_ENABLED", enabled)
    monkeypatch.setattr(proctree, "PROC_SWEEP_ALLOW_REAP", allow)
    monkeypatch.setattr(proctree, "PROC_SWEEP_DRY_RUN", dry)
    assert proctree.reaping_allowed()[0] is expected


def test_explicit_dry_run_argument_cannot_arm_the_sweep(live_pid, monkeypatch):
    """dry_run=False must not bypass the configuration — only disarm, never arm."""
    monkeypatch.setattr(proctree, "PROC_SWEEP_ENABLED", False)
    _write_registry([_entry(live_pid, job_id="job-done")])
    with _no_signals():
        rep = proctree.sweep_orphans(dry_run=False, active=set(), now=NOW)
    assert rep["reap_armed"] is False
    assert rep["reaped"] == [] and rep["would_reap"] == [live_pid]
    assert subprocess.run(["ps", "-p", str(live_pid)], capture_output=True).returncode == 0


def test_enabling_the_flag_does_not_enable_killing(live_pid, monkeypatch):
    """Even if someone sets PROC_SWEEP_ENABLED=true, nothing is signalled."""
    monkeypatch.setattr(proctree, "PROC_SWEEP_ENABLED", True)
    _write_registry([_entry(live_pid, job_id="job-done")])
    rep = _sweep(dry_run=True, active=set(), now=NOW)
    assert rep["sweep_enabled"] is True
    assert rep["signals_sent"] == 0 and rep["would_reap"] == [live_pid]
    assert subprocess.run(["ps", "-p", str(live_pid)], capture_output=True).returncode == 0


def test_startup_sweep_never_blocks_the_agent(monkeypatch):
    """A sweep failure must not stop the agent from starting."""
    from automation.agent import main as agent_main

    def explode(**kw):
        raise RuntimeError("backend on fire")
    monkeypatch.setattr(agent_main.proctree, "sweep_orphans", explode)

    agent_main._startup_sweep()   # must not raise


def test_startup_sweep_reports_unavailable_backend(monkeypatch, caplog):
    """Backend unreachable at startup: sweep runs, classifies nothing, agent lives on."""
    from automation.agent import main as agent_main

    def unavailable():
        raise proctree.SweepUnavailable("connection refused")
    monkeypatch.setattr(proctree, "active_job_ids", unavailable)

    with _no_signals():
        agent_main._startup_sweep()
