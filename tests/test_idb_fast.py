"""idb_fast must never be worse than spawning the idb CLI: same result shape, same
exceptions, and a silent fall back to the CLI whenever a worker is unavailable."""
import subprocess
from unittest.mock import patch

import pytest

from automation.scenarios import idb_fast


def test_a_patched_subprocess_run_is_honoured():
    """Tests capture idb calls by patching subprocess.run; that must keep working."""
    seen = []
    with patch("subprocess.run", lambda cmd, **k: seen.append(cmd) or "faked"):
        out = idb_fast.subprocess_run(["/x/idb", "ui", "tap", "--udid", "U", "1", "2"],
                                      timeout=5)
    assert out == "faked"
    assert seen == [["/x/idb", "ui", "tap", "--udid", "U", "1", "2"]]


def test_no_worker_falls_back_to_the_cli(monkeypatch):
    calls = []
    monkeypatch.setattr(idb_fast, "_acquire", lambda: None)
    monkeypatch.setattr(idb_fast, "_cli", lambda argv, timeout: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0, "[]", ""))
    res = idb_fast.run(["ui", "describe-all", "--udid", "U"])
    assert res.returncode == 0 and res.stdout == "[]"
    assert calls == [["ui", "describe-all", "--udid", "U"]]


def test_disabled_by_env_uses_the_cli(monkeypatch):
    monkeypatch.setenv("IDB_FAST", "0")
    monkeypatch.setattr(idb_fast, "_acquire", lambda: pytest.fail("worker used"))
    monkeypatch.setattr(idb_fast, "_cli", lambda argv, timeout:
                        subprocess.CompletedProcess(argv, 0, "ok", ""))
    assert idb_fast.run(["ui", "tap"]).stdout == "ok"


class _DeadWorker:
    killed = False

    def call(self, argv, timeout):
        raise EOFError("idb worker exited")

    def alive(self):
        return False

    def kill(self):
        _DeadWorker.killed = True


def test_a_worker_dying_mid_command_retries_on_the_cli(monkeypatch):
    monkeypatch.setattr(idb_fast, "_acquire", lambda: _DeadWorker())
    monkeypatch.setattr(idb_fast, "_cli", lambda argv, timeout:
                        subprocess.CompletedProcess(argv, 0, "from-cli", ""))
    assert idb_fast.run(["ui", "describe-all"]).stdout == "from-cli"
    assert _DeadWorker.killed


class _SlowWorker(_DeadWorker):
    def call(self, argv, timeout):
        raise subprocess.TimeoutExpired(["idb", *argv], timeout)


def test_a_timeout_still_raises_like_the_cli(monkeypatch):
    monkeypatch.setattr(idb_fast, "_acquire", lambda: _SlowWorker())
    with pytest.raises(subprocess.TimeoutExpired):
        idb_fast.run(["ui", "describe-all"], timeout=0.1)


def test_check_raises_on_failure(monkeypatch):
    monkeypatch.setattr(idb_fast, "run", lambda argv, timeout=20.0:
                        subprocess.CompletedProcess(argv, 1, "", "boom"))
    with pytest.raises(subprocess.CalledProcessError):
        idb_fast.subprocess_run(["idb", "ui", "tap"], check=True)


def test_the_interpreter_comes_from_the_idb_shebang(tmp_path):
    py = tmp_path / "python3"
    py.write_text("")
    idb = tmp_path / "idb"
    idb.write_text(f"#!{py}\nimport sys\n")
    assert idb_fast._python_for(str(idb)) == str(py)
    assert idb_fast._python_for(str(tmp_path / "missing")) is None
