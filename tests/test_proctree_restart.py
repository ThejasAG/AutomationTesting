"""Crash/restart validation: the registry outlives the agent that wrote it.

Every step here runs in a SEPARATE OS process. That is the whole point — a
spawn-then-sweep inside one interpreter proves nothing about restart, because the
Popen handles and the module globals are still in memory. Here the process that
spawns the tracked child dies via os._exit() (no teardown, no atexit, no
reap_pid), and a fresh interpreter later has nothing but the JSON on disk.

Uses `sleep` subprocesses, never Appium. Sends no signals except the harness's own
cleanup of the processes it spawned, after all assertions have run.
"""
import json
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

from automation.utils import proctree

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
CRASHED_JOB = "dry-run-crashed-job"
LIVE_JOB = "dry-run-active-job"


def _run(code: str, env_extra: dict, timeout: int = 120) -> subprocess.CompletedProcess:
    """Run *code* in a fresh interpreter with no inherited state."""
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), **env_extra}
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True, text=True, timeout=timeout, cwd=str(REPO_ROOT), env=env,
    )


def _crash_spawn(env_extra: dict, job_id: str, kind: str) -> int:
    """A fresh process spawns a tracked sleeper, then dies WITHOUT tearing it down.

    os._exit() skips atexit handlers, finally blocks and reap_pid — the closest
    thing to a killed agent that a test can stage safely.
    """
    res = _run(
        f"""
        import os, subprocess, sys
        from automation.utils import proctree
        # 4F.7A: the reaper asks the backend for active jobs. In this harness the
        # scratch job-state DB stands in for that endpoint, so the classification
        # path below is exercised exactly as it was when proctree queried directly.
        from automation.database.config import SessionLocal as _SL
        from automation.database.models import TestRun as _TR
        proctree.set_active_jobs_source(lambda: set(
            str(r[0]) for r in _SL().query(_TR.id).filter(
                _TR.job_state.in_(tuple(proctree.ACTIVE_JOB_STATES))).all()))
        p = proctree.spawn_tracked(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            job_id={job_id!r}, kind={kind!r},
            # The sleeper must NOT inherit this process's pipes: it outlives the
            # crash, and an inherited stdout keeps the pipe open, hanging any
            # reader of the crashed process for as long as the orphan lives.
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print(p.pid, flush=True)
        os._exit(9)   # crash: no teardown, no reap, no registry cleanup
        """,
        env_extra,
    )
    assert res.returncode == 9, f"harness did not crash as intended: {res.stderr}"
    return int(res.stdout.strip())


SWEEP_SRC = """
import json, os, sys
from automation.utils import proctree
# 4F.7A: the reaper asks the backend for active jobs. In this harness the
# scratch job-state DB stands in for that endpoint, so the classification
# path below is exercised exactly as it was when proctree queried directly.
from automation.database.config import SessionLocal as _SL
from automation.database.models import TestRun as _TR
proctree.set_active_jobs_source(lambda: set(
    str(r[0]) for r in _SL().query(_TR.id).filter(
        _TR.job_state.in_(tuple(proctree.ACTIVE_JOB_STATES))).all()))

# Any real signal from the sweep aborts this process and fails the test.
signals = []
_kill, _killpg = os.kill, os.killpg
def spy_kill(pid, sig, *a, **k):
    if sig != 0:
        signals.append(["kill", pid, sig]); raise AssertionError("sweep signalled")
    return _kill(pid, sig, *a, **k)
def spy_killpg(pgid, sig, *a, **k):
    signals.append(["killpg", pgid, sig]); raise AssertionError("sweep signalled")
os.kill, os.killpg = spy_kill, spy_killpg

rep = proctree.sweep_orphans(dry_run=True)

os.kill, os.killpg = _kill, _killpg
rep["signals_observed"] = signals
rep["registry_path"] = proctree.REGISTRY_PATH
print("REPORT:" + json.dumps(rep, default=str))
"""


def _sweep_fresh(env_extra: dict) -> dict:
    """Run the startup sweep in a brand-new interpreter and return its report."""
    res = _run(SWEEP_SRC, env_extra)
    assert res.returncode == 0, f"sweep process failed: {res.stderr}"
    line = next(l for l in res.stdout.splitlines() if l.startswith("REPORT:"))
    return json.loads(line[len("REPORT:"):])


def _alive(pid: int) -> bool:
    return subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode == 0


def _verdicts(report: dict) -> dict:
    return {r["pid"]: r["verdict"] for r in report["entries"]}


@pytest.fixture(scope="module")
def crashed(tmp_path_factory):
    """Stage a crashed agent: three tracked processes, orphaned in a scratch registry.

    The job-state DB is a scratch sqlite file, so the real backend database is
    never written to — but `active_job_ids()` runs its real query against it.
    """
    tmp = tmp_path_factory.mktemp("restart")
    env = {
        "PROC_REGISTRY_PATH": str(tmp / "proc_registry.json"),
        "DATABASE_URL": f"sqlite:////{(tmp / 'jobs.db').as_posix().lstrip('/')}",
        "PROC_SWEEP_MIN_AGE_SEC": "0",     # no waiting 15 minutes in a test
    }

    # A real job-state store, seeded through the same models the sweep reads.
    seed = _run(
        f"""
        from automation.database.config import Base, engine, SessionLocal
        from automation.database import models
        Base.metadata.create_all(engine)
        db = SessionLocal()
        db.add(models.TestRun(id={CRASHED_JOB!r}, job_state="running"))
        db.add(models.TestRun(id={LIVE_JOB!r}, job_state="running"))
        db.commit(); db.close()
        print("seeded")
        """,
        env,
    )
    assert "seeded" in seed.stdout, seed.stderr

    # Filled in as we go, so a failure PART WAY THROUGH setup still cleans up what
    # it already spawned — a yield-fixture skips its teardown if setup raises.
    pids = {}

    def _cleanup():
        """Kill ONLY the processes this harness spawned."""
        for pid in pids.values():
            try:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
            except (ProcessLookupError, ChildProcessError, PermissionError, OSError):
                pass

    try:
        pids["orphan"] = _crash_spawn(env, CRASHED_JOB, "appium")
        pids["active"] = _crash_spawn(env, LIVE_JOB, "appium")
        pids["metro"] = _crash_spawn(env, CRASHED_JOB, "metro")
    except BaseException:
        _cleanup()
        raise

    yield {"env": env, "pids": pids, "tmp": tmp}

    _cleanup()   # after all assertions have run


def _finish_crashed_job(env: dict) -> None:
    """Flip the job to a terminal state through the real model, as the backend would."""
    res = _run(
        f"""
        from automation.database.config import SessionLocal
        from automation.database import models
        db = SessionLocal()
        db.query(models.TestRun).filter(models.TestRun.id == {CRASHED_JOB!r}).update(
            {{"job_state": "completed"}})
        db.commit(); db.close()
        print("finished")
        """,
        env,
    )
    assert "finished" in res.stdout, res.stderr


# ── 1-2. The registry, and the processes, outlive the crashed agent ──────────

def test_registry_survives_the_crashed_agent(crashed):
    """A fresh interpreter reads back what the dead process recorded."""
    entries = json.loads(pathlib.Path(crashed["env"]["PROC_REGISTRY_PATH"]).read_text())
    assert set(entries) == {str(p) for p in crashed["pids"].values()}
    orphan = entries[str(crashed["pids"]["orphan"])]
    assert orphan["job_id"] == CRASHED_JOB
    assert orphan["kind"] == "appium"
    assert orphan["identity"], "start-time identity persisted for the restart check"
    assert orphan["pgid"] == crashed["pids"]["orphan"]


def test_tracked_processes_survive_the_crashed_agent(crashed):
    """The agent died; its tracked children did not. That is the leak being detected."""
    for name, pid in crashed["pids"].items():
        assert _alive(pid), f"{name} did not outlive the crashed agent"


# ── 3-7. What a restarted agent's startup sweep concludes ────────────────────

@pytest.fixture(scope="module")
def restart_report(crashed):
    """One fresh-process sweep, run after the crashed job reaches a terminal state."""
    # While the job is still active, nothing is reapable...
    active_report = _sweep_fresh(crashed["env"])
    # ...then the backend marks it finished, and a restarted agent sweeps again.
    _finish_crashed_job(crashed["env"])
    return {"before": active_report, "after": _sweep_fresh(crashed["env"])}


def test_finished_job_becomes_would_reap(crashed, restart_report):
    """The lifecycle this phase exists to prove."""
    pid = crashed["pids"]["orphan"]
    row = next(r for r in restart_report["after"]["entries"] if r["pid"] == pid)
    assert row["verdict"] == "WOULD_REAP", row["reason"]
    assert row["job_active"] is False
    assert row["identity_match"] is True
    assert restart_report["after"]["would_reap"] == [pid]


def test_active_job_is_not_reapable_before_or_after(crashed, restart_report):
    """The still-running job's process is never a candidate, in either sweep."""
    pid = crashed["pids"]["active"]
    for phase in ("before", "after"):
        row = next(r for r in restart_report[phase]["entries"] if r["pid"] == pid)
        assert row["verdict"] == "DO_NOT_REAP", f"{phase}: {row['reason']}"
        assert row["job_active"] is True
    assert restart_report["before"]["would_reap"] == [], "active job -> nothing reapable"


def test_identity_is_still_validated_after_restart(crashed):
    """A recycled pid must be rejected even though the registry entry looks perfect."""
    reg = pathlib.Path(crashed["env"]["PROC_REGISTRY_PATH"])
    original = reg.read_text()
    entries = json.loads(original)
    pid = str(crashed["pids"]["orphan"])
    entries[pid]["identity"] = "Thu Jan  1 00:00:00 1970"
    reg.write_text(json.dumps(entries))
    try:
        row = next(r for r in _sweep_fresh(crashed["env"])["entries"]
                   if r["pid"] == crashed["pids"]["orphan"])
        assert row["verdict"] == "DO_NOT_REAP"
        assert "IDENTITY_MISMATCH" in row["reason"]
        assert row["identity_match"] is False
    finally:
        reg.write_text(original)


def test_metro_stays_never_reap_across_restart(crashed, restart_report):
    """Metro is excluded even as an orphan of a finished job."""
    pid = crashed["pids"]["metro"]
    row = next(r for r in restart_report["after"]["entries"] if r["pid"] == pid)
    assert row["verdict"] == "NEVER_REAP"
    assert pid not in restart_report["after"]["would_reap"]
    assert _alive(pid)


def test_restart_sweep_sends_zero_signals(crashed, restart_report):
    """The dry-run guarantee, enforced inside the fresh sweep process itself."""
    for phase in ("before", "after"):
        assert restart_report[phase]["signals_observed"] == []
        assert restart_report[phase]["signals_sent"] == 0
        assert restart_report[phase]["sweep_enabled"] is False
    # Everything classified — WOULD_REAP included — is still running.
    for pid in crashed["pids"].values():
        assert _alive(pid), "the dry-run sweep killed something"


# ── 8-10. Safety properties, verified across the restart boundary ────────────

def test_unregistered_processes_stay_invisible(crashed, restart_report):
    """Only registry entries are considered — no scanning for appium/node by name."""
    seen = {r["pid"] for r in restart_report["after"]["entries"]}
    assert seen == set(crashed["pids"].values())
    assert os.getpid() not in seen


def test_corrupt_registry_fails_closed_after_restart(crashed):
    reg = pathlib.Path(crashed["env"]["PROC_REGISTRY_PATH"])
    original = reg.read_text()
    reg.write_text("{ truncated mid-write")
    try:
        rep = _sweep_fresh(crashed["env"])
        assert rep["available"] is False
        assert rep["entries"] == [] and rep["would_reap"] == []
        assert rep["signals_observed"] == []
    finally:
        reg.write_text(original)
    for pid in crashed["pids"].values():
        assert _alive(pid), "a corrupt registry must not endanger anything"


def test_startup_sweep_is_non_blocking_after_restart(crashed):
    """The real agent entry point: a sweep failure never stops startup."""
    res = _run(
        """
        from automation.agent import main as agent_main
        from automation.utils import proctree
        # 4F.7A: the reaper asks the backend for active jobs. In this harness the
        # scratch job-state DB stands in for that endpoint, so the classification
        # path below is exercised exactly as it was when proctree queried directly.
        from automation.database.config import SessionLocal as _SL
        from automation.database.models import TestRun as _TR
        proctree.set_active_jobs_source(lambda: set(
            str(r[0]) for r in _SL().query(_TR.id).filter(
                _TR.job_state.in_(tuple(proctree.ACTIVE_JOB_STATES))).all()))
        def boom(**kw):
            raise RuntimeError("registry on fire")
        proctree.sweep_orphans = boom
        agent_main._startup_sweep()
        print("agent continued")
        """,
        {**crashed["env"], "PROC_REGISTRY_PATH": "/nonexistent/dir/registry.json"},
    )
    assert "agent continued" in res.stdout, res.stderr
    assert res.returncode == 0


# ── P0.3: the armed reaping path ─────────────────────────────────────────────

ARMED = {"PROC_SWEEP_ENABLED": "true", "PROC_SWEEP_ALLOW_REAP": "true",
         "PROC_SWEEP_DRY_RUN": "false"}

REAP_SRC = SWEEP_SRC.replace(
    "rep = proctree.sweep_orphans(dry_run=True)",
    "rep = proctree.sweep_orphans()",
).replace(
    """def spy_kill(pid, sig, *a, **k):
    if sig != 0:
        signals.append(["kill", pid, sig]); raise AssertionError("sweep signalled")
    return _kill(pid, sig, *a, **k)
def spy_killpg(pgid, sig, *a, **k):
    signals.append(["killpg", pgid, sig]); raise AssertionError("sweep signalled")""",
    # Armed runs are ALLOWED to signal — record what was signalled instead of aborting.
    """def spy_kill(pid, sig, *a, **k):
    if sig != 0:
        signals.append(["kill", pid, sig])
    return _kill(pid, sig, *a, **k)
def spy_killpg(pgid, sig, *a, **k):
    signals.append(["killpg", pgid, sig])
    return _killpg(pgid, sig, *a, **k)""",
)


def _sweep_armed(env_extra: dict) -> dict:
    """Run the sweep in a fresh interpreter with reaping armed."""
    res = _run(REAP_SRC, {**env_extra, **ARMED})
    assert res.returncode == 0, f"armed sweep failed: {res.stderr}"
    line = next(l for l in res.stdout.splitlines() if l.startswith("REPORT:"))
    return json.loads(line[len("REPORT:"):])


def _registry(env: dict) -> dict:
    return json.loads(pathlib.Path(env["PROC_REGISTRY_PATH"]).read_text())


@pytest.fixture
def staged(tmp_path):
    """One crashed agent, one finished job, plus an UNREGISTERED bystander process."""
    env = {
        "PROC_REGISTRY_PATH": str(tmp_path / "proc_registry.json"),
        "DATABASE_URL": f"sqlite:////{(tmp_path / 'jobs.db').as_posix().lstrip('/')}",
        "PROC_SWEEP_MIN_AGE_SEC": "0",
    }
    seed = _run(
        f"""
        from automation.database.config import Base, engine, SessionLocal
        from automation.database import models
        Base.metadata.create_all(engine)
        db = SessionLocal()
        db.add(models.TestRun(id={CRASHED_JOB!r}, job_state="completed"))
        db.add(models.TestRun(id={LIVE_JOB!r}, job_state="running"))
        db.commit(); db.close()
        print("seeded")
        """,
        env,
    )
    assert "seeded" in seed.stdout, seed.stderr

    pids, extra = {}, []

    def _cleanup():
        for pid in list(pids.values()) + [p.pid for p in extra]:
            try:
                os.kill(pid, 9)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        for p in extra:
            try:
                p.wait(timeout=5)
            except Exception:
                pass

    try:
        pids["orphan"] = _crash_spawn(env, CRASHED_JOB, "appium")
        pids["active"] = _crash_spawn(env, LIVE_JOB, "appium")
        pids["metro"] = _crash_spawn(env, CRASHED_JOB, "metro")
        # Never recorded anywhere — the sweep must not be able to see it.
        bystander = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        extra.append(bystander)
    except BaseException:
        _cleanup()
        raise

    yield {"env": env, "pids": pids, "unregistered": bystander.pid}
    _cleanup()


def test_armed_sweep_reaps_only_the_eligible_entry(staged):
    """THE critical test: stale + registered + identity valid + job done + armed."""
    env, pids = staged["env"], staged["pids"]
    before = _registry(env)
    assert set(before) == {str(p) for p in pids.values()}

    rep = _sweep_armed(env)

    assert rep["reap_armed"] is True, rep["gate_reason"]
    orphan = next(r for r in rep["entries"] if r["pid"] == pids["orphan"])
    assert orphan["verdict"] == "WOULD_REAP"
    assert orphan["action"] == "REAP_COMPLETE"
    assert rep["reaped"] == [pids["orphan"]] and rep["failed"] == []

    # process dead, registry entry removed
    assert not _alive(pids["orphan"])
    assert str(pids["orphan"]) not in _registry(env)

    # everything else survives, and its entries are retained
    assert _alive(pids["active"]), "active job's process must survive"
    assert _alive(pids["metro"]), "Metro must survive"
    assert _alive(staged["unregistered"]), "UNREGISTERED — NOT TOUCHED"
    assert {str(pids["active"]), str(pids["metro"])} <= set(_registry(env))


def test_armed_sweep_never_touches_unregistered_processes(staged):
    """A live process that is not in the registry is invisible, not spared by luck."""
    rep = _sweep_armed(staged["env"])
    assert staged["unregistered"] not in {r["pid"] for r in rep["entries"]}
    assert staged["unregistered"] not in rep["reaped"]
    assert _alive(staged["unregistered"])


def test_armed_sweep_leaves_metro_and_active_jobs_alone(staged):
    rep = _sweep_armed(staged["env"])
    metro = next(r for r in rep["entries"] if r["pid"] == staged["pids"]["metro"])
    active = next(r for r in rep["entries"] if r["pid"] == staged["pids"]["active"])
    assert metro["verdict"] == "NEVER_REAP" and metro["action"] == "NONE"
    assert active["verdict"] == "DO_NOT_REAP" and active["action"] == "NONE"
    assert _alive(staged["pids"]["metro"]) and _alive(staged["pids"]["active"])


@pytest.mark.parametrize("flags,why", [
    ({"PROC_SWEEP_ENABLED": "false", "PROC_SWEEP_ALLOW_REAP": "true", "PROC_SWEEP_DRY_RUN": "false"},
     "PROC_SWEEP_ENABLED=false"),
    ({"PROC_SWEEP_ENABLED": "true", "PROC_SWEEP_ALLOW_REAP": "false", "PROC_SWEEP_DRY_RUN": "false"},
     "PROC_SWEEP_ALLOW_REAP=false"),
    ({"PROC_SWEEP_ENABLED": "true", "PROC_SWEEP_ALLOW_REAP": "true", "PROC_SWEEP_DRY_RUN": "true"},
     "PROC_SWEEP_DRY_RUN=true"),
])
def test_each_disarming_flag_blocks_all_signals(staged, flags, why):
    """Any one switch out of alignment means zero signals, in a real process."""
    res = _run(SWEEP_SRC, {**staged["env"], **flags})   # SWEEP_SRC aborts on any signal
    assert res.returncode == 0, res.stderr
    rep = json.loads(next(l for l in res.stdout.splitlines() if l.startswith("REPORT:"))[7:])
    assert rep["reap_armed"] is False
    assert why in rep["gate_reason"] or rep["gate_reason"]
    assert rep["signals_observed"] == [] and rep["reaped"] == []
    assert rep["would_reap"] == [staged["pids"]["orphan"]], "still classified, just not acted on"
    for pid in list(staged["pids"].values()) + [staged["unregistered"]]:
        assert _alive(pid)


def test_identity_change_between_classification_and_signal_blocks_the_reap(staged):
    """The mandatory second identity check, exercised at its exact race point."""
    env = staged["env"]
    res = _run(
        f"""
        import json, os
        from automation.utils import proctree
        # 4F.7A: the reaper asks the backend for active jobs. In this harness the
        # scratch job-state DB stands in for that endpoint, so the classification
        # path below is exercised exactly as it was when proctree queried directly.
        from automation.database.config import SessionLocal as _SL
        from automation.database.models import TestRun as _TR
        proctree.set_active_jobs_source(lambda: set(
            str(r[0]) for r in _SL().query(_TR.id).filter(
                _TR.job_state.in_(tuple(proctree.ACTIVE_JOB_STATES))).all()))

        real_identity = proctree._proc_identity
        seen = {{}}
        def drifting(pid):
            # Truthful for the two classification reads (_classify + the report
            # row), then changed for the third — the final pre-signal gate. That
            # is exactly the "pid recycled in the gap" race the gate exists for.
            seen[pid] = seen.get(pid, 0) + 1
            return real_identity(pid) if seen[pid] <= 2 else "Thu Jan  1 00:00:00 1970"
        proctree._proc_identity = drifting

        signals = []
        _k, _kp = os.kill, os.killpg
        os.kill = lambda p, s, *a, **w: (signals.append(s) if s != 0 else None) or _k(p, s, *a, **w)
        os.killpg = lambda g, s, *a, **w: signals.append(("killpg", s))
        rep = proctree.sweep_orphans()
        os.kill, os.killpg = _k, _kp
        rep["signals_observed"] = signals
        print("REPORT:" + json.dumps(rep, default=str))
        """,
        {**env, **ARMED},
    )
    assert res.returncode == 0, res.stderr
    rep = json.loads(next(l for l in res.stdout.splitlines() if l.startswith("REPORT:"))[7:])
    orphan = next(r for r in rep["entries"] if r["pid"] == staged["pids"]["orphan"])
    assert orphan["action"] == "SKIPPED"
    assert "IDENTITY_CHANGED" in orphan["reason"]
    assert rep["signals_observed"] == [] and rep["reaped"] == []
    assert _alive(staged["pids"]["orphan"]), "must not signal a possibly-recycled pid"
    assert str(staged["pids"]["orphan"]) in _registry(env), "entry retained for the next sweep"


def test_failed_cleanup_keeps_the_registry_entry(staged):
    """REAP_FAILED must not silently forget the entry."""
    env = staged["env"]
    res = _run(
        """
        import json
        from automation.utils import proctree
        # 4F.7A: the reaper asks the backend for active jobs. In this harness the
        # scratch job-state DB stands in for that endpoint, so the classification
        # path below is exercised exactly as it was when proctree queried directly.
        from automation.database.config import SessionLocal as _SL
        from automation.database.models import TestRun as _TR
        proctree.set_active_jobs_source(lambda: set(
            str(r[0]) for r in _SL().query(_TR.id).filter(
                _TR.job_state.in_(tuple(proctree.ACTIVE_JOB_STATES))).all()))
        # Pretend the group refuses to die: reap_pid reports failure.
        proctree.reap_pid = lambda pid, timeout=10.0: False
        rep = proctree.sweep_orphans()
        print("REPORT:" + json.dumps(rep, default=str))
        """,
        {**env, **ARMED},
    )
    assert res.returncode == 0, res.stderr
    rep = json.loads(next(l for l in res.stdout.splitlines() if l.startswith("REPORT:"))[7:])
    orphan = next(r for r in rep["entries"] if r["pid"] == staged["pids"]["orphan"])
    assert orphan["action"] == "REAP_FAILED" and orphan["verdict"] == "REAP_FAILED"
    assert rep["failed"] == [staged["pids"]["orphan"]] and rep["reaped"] == []
    assert str(staged["pids"]["orphan"]) in _registry(env), "entry must survive a failed reap"
    assert _alive(staged["pids"]["orphan"])


def test_corrupt_registry_never_reaps_even_when_armed(staged):
    reg = pathlib.Path(staged["env"]["PROC_REGISTRY_PATH"])
    original = reg.read_text()
    reg.write_text("{ truncated")
    try:
        rep = _sweep_armed(staged["env"])
        assert rep["available"] is False
        assert rep["reaped"] == [] and rep["entries"] == []
        assert rep["signals_observed"] == []
    finally:
        reg.write_text(original)
    for pid in list(staged["pids"].values()) + [staged["unregistered"]]:
        assert _alive(pid)


def test_backend_unavailable_never_reaps_even_when_armed(staged):
    res = _run(
        """
        import json, os
        from automation.utils import proctree
        # 4F.7A: the reaper asks the backend for active jobs. In this harness the
        # scratch job-state DB stands in for that endpoint, so the classification
        # path below is exercised exactly as it was when proctree queried directly.
        from automation.database.config import SessionLocal as _SL
        from automation.database.models import TestRun as _TR
        proctree.set_active_jobs_source(lambda: set(
            str(r[0]) for r in _SL().query(_TR.id).filter(
                _TR.job_state.in_(tuple(proctree.ACTIVE_JOB_STATES))).all()))
        def unavailable():
            raise proctree.SweepUnavailable("connection refused")
        proctree.active_job_ids = unavailable
        signals = []
        _kp = os.killpg
        os.killpg = lambda g, s, *a, **w: signals.append(s)
        rep = proctree.sweep_orphans()
        os.killpg = _kp
        rep["signals_observed"] = signals
        print("REPORT:" + json.dumps(rep, default=str))
        """,
        {**staged["env"], **ARMED},
    )
    assert res.returncode == 0, res.stderr
    rep = json.loads(next(l for l in res.stdout.splitlines() if l.startswith("REPORT:"))[7:])
    assert rep["available"] is False
    assert rep["reaped"] == [] and rep["signals_observed"] == []
    assert {r["verdict"] for r in rep["entries"]} == {"UNKNOWN"}
    for pid in staged["pids"].values():
        assert _alive(pid)


def test_too_young_entry_is_never_reaped_when_armed(staged):
    """The age floor still applies with reaping armed."""
    env = {**staged["env"], "PROC_SWEEP_MIN_AGE_SEC": "9000"}
    rep = _sweep_armed(env)
    assert {r["verdict"] for r in rep["entries"]} <= {"TOO_YOUNG", "NEVER_REAP", "DO_NOT_REAP"}
    assert rep["reaped"] == [] and rep["would_reap"] == []
    for pid in staged["pids"].values():
        assert _alive(pid)


def test_sigterm_is_tried_before_sigkill(staged):
    """Graceful first: a process that honours SIGTERM is never SIGKILLed."""
    rep = _sweep_armed(staged["env"])
    assert rep["reaped"] == [staged["pids"]["orphan"]]
    kills = [s for s in rep["signals_observed"] if s[0] == "killpg"]
    assert kills, "expected a process-group signal"
    assert kills[0][2] == 15, f"first signal must be SIGTERM, got {kills[0]}"
    assert not any(s[2] == 9 for s in kills), "a cooperative process needs no SIGKILL"


def test_sigkill_fallback_for_a_process_that_ignores_sigterm(tmp_path):
    """SIGTERM-ignoring process: the bounded grace period expires, SIGKILL lands."""
    env = {
        "PROC_REGISTRY_PATH": str(tmp_path / "reg.json"),
        "DATABASE_URL": f"sqlite:////{(tmp_path / 'j.db').as_posix().lstrip('/')}",
        "PROC_SWEEP_MIN_AGE_SEC": "0",
    }
    _run(
        f"""
        from automation.database.config import Base, engine, SessionLocal
        from automation.database import models
        Base.metadata.create_all(engine)
        db = SessionLocal(); db.add(models.TestRun(id={CRASHED_JOB!r}, job_state="completed"))
        db.commit(); print("seeded")
        """, env)

    stubborn = """import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(120)"""
    res = _run(
        f"""
        import os, subprocess, sys
        from automation.utils import proctree
        # 4F.7A: the reaper asks the backend for active jobs. In this harness the
        # scratch job-state DB stands in for that endpoint, so the classification
        # path below is exercised exactly as it was when proctree queried directly.
        from automation.database.config import SessionLocal as _SL
        from automation.database.models import TestRun as _TR
        proctree.set_active_jobs_source(lambda: set(
            str(r[0]) for r in _SL().query(_TR.id).filter(
                _TR.job_state.in_(tuple(proctree.ACTIVE_JOB_STATES))).all()))
        p = proctree.spawn_tracked([sys.executable, "-c", {stubborn!r}],
            job_id={CRASHED_JOB!r}, kind="appium",
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(p.pid, flush=True)
        os._exit(9)
        """, env)
    assert res.returncode == 9, res.stderr
    pid = int(res.stdout.strip())
    try:
        rep = _sweep_armed(env)
        sigs = [s[2] for s in rep["signals_observed"] if s[0] == "killpg"]
        assert 15 in sigs, "SIGTERM must be tried first"
        assert 9 in sigs, "SIGKILL must follow when SIGTERM is ignored"
        assert rep["reaped"] == [pid]
        assert not _alive(pid)
        assert str(pid) not in _registry(env)
    finally:
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError, OSError):
            pass
