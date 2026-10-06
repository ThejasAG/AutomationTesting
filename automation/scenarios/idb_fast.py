"""Drop-in replacement for `subprocess.run([idb, *args], ...)` without the start-up.

Every idb call used to spawn a fresh `idb` process, and each spawn paid ~0.55-0.9s
to start Python and import idb before talking to the companion. A full cross-app
run makes 150-300 such calls, so minutes went to start-up alone. `run()` sends the
same argv to a small pool of long-lived idb_worker.py processes instead; small
commands (tap, describe-point, text, key) drop from ~0.55s to ~0.04s.

Behaves like subprocess.run for every caller in this repo: returns a
CompletedProcess with .returncode/.stdout/.stderr, and raises
subprocess.TimeoutExpired on timeout (the worker is killed, as the CLI would be).

Never makes things worse: if a worker cannot be started, or dies mid-command, the
call falls back to spawning the CLI exactly as before. IDB_FAST=0 disables it.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import select
import subprocess
import threading
import time
from typing import List, Optional, Sequence

from automation.scenarios.idb_path import idb_binary

logger = logging.getLogger(__name__)

_REAL_RUN = subprocess.run
_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "idb_worker.py")
# The step, the UI watchdog and the Inspector can all read a screen at once.
_MAX_WORKERS = 4

_idle: "queue.LifoQueue[_Worker]" = queue.LifoQueue()
_lock = threading.Lock()
_count = 0
_broken_until = 0.0     # after a failed start, use the CLI for a while instead of retrying every call


def _python_for(idb: str) -> Optional[str]:
    """The interpreter fb-idb is installed in: the idb script's own shebang."""
    try:
        with open(os.path.realpath(idb), "rb") as f:
            first = f.readline().decode(errors="replace").strip()
    except OSError:
        return None
    if not first.startswith("#!"):
        return None
    py = first[2:].strip().split()[0]
    return py if os.path.isfile(py) else None


class _Worker:
    def __init__(self, python: str):
        self.p = subprocess.Popen([python, _WORKER], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, bufsize=1)
        hello = self._readline(30.0)
        if not hello or not json.loads(hello).get("ready"):
            self.kill()
            raise RuntimeError("idb worker did not start")

    def _readline(self, timeout: float) -> Optional[str]:
        r, _, _ = select.select([self.p.stdout], [], [], max(0.0, timeout))
        return self.p.stdout.readline() if r else None

    def alive(self) -> bool:
        return self.p.poll() is None

    def call(self, argv: Sequence[str], timeout: float) -> dict:
        self.p.stdin.write(json.dumps({"argv": list(argv)}) + "\n")
        self.p.stdin.flush()
        line = self._readline(timeout)
        if line is None:
            raise subprocess.TimeoutExpired(["idb", *argv], timeout)
        if not line:
            raise EOFError("idb worker exited")
        return json.loads(line)

    def kill(self) -> None:
        try:
            self.p.kill()
            self.p.wait(timeout=2)
        except Exception:
            pass


def _acquire() -> Optional[_Worker]:
    global _count, _broken_until
    while True:
        try:
            w = _idle.get_nowait()
        except queue.Empty:
            break
        if w.alive():
            return w
        with _lock:
            _count -= 1
    with _lock:
        if time.time() < _broken_until:
            return None
        if _count >= _MAX_WORKERS:
            spawn = False
        else:
            _count += 1
            spawn = True
    if not spawn:
        try:
            return _idle.get(timeout=5.0)    # all busy: wait briefly for one
        except queue.Empty:
            return None
    python = _python_for(idb_binary())
    try:
        if not python:
            raise RuntimeError("no interpreter for idb")
        return _Worker(python)
    except Exception as e:
        logger.info("idb_fast: worker unavailable (%s) -- using the idb CLI", e)
        with _lock:
            _count -= 1
            _broken_until = time.time() + 60
        return None


def _release(w: _Worker, ok: bool) -> None:
    global _count
    if ok and w.alive():
        _idle.put(w)
    else:
        w.kill()
        with _lock:
            _count -= 1


def _cli(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run([idb_binary(), *argv], capture_output=True, text=True,
                          timeout=timeout)


def _trace(argv: Sequence[str], t0: float, how: str) -> None:
    """IDB_TRACE=<file>: one line per idb call (when, seconds, path, command), so a
    slow step can be broken down into the calls that made it slow."""
    path = os.getenv("IDB_TRACE")
    if not path:
        return
    try:
        with open(path, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {time.time() - t0:6.2f}s {how:6} "
                    f"{' '.join(a for a in argv if not a.startswith(('--udid',)))[:120]}\n")
    except OSError:
        pass


def run(argv: Sequence[str], timeout: float = 20.0) -> subprocess.CompletedProcess:
    """`idb <argv>` -- same result and exceptions as subprocess.run(text=True)."""
    t0 = time.time()
    try:
        res = _run(argv, timeout)
    except subprocess.TimeoutExpired:
        _trace([str(a) for a in argv], t0, "TIMEOUT")
        raise
    _trace([str(a) for a in argv], t0, "idb")
    return res


def _run(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    argv = [str(a) for a in argv]
    if os.getenv("IDB_FAST", "1") == "0":
        return _cli(argv, timeout)
    w = _acquire()
    if w is None:
        return _cli(argv, timeout)
    try:
        res = w.call(argv, timeout)
    except subprocess.TimeoutExpired:
        _release(w, ok=False)          # a wedged command: kill it, like the CLI timeout
        raise
    except Exception as e:
        _release(w, ok=False)
        logger.info("idb_fast: worker failed (%s) -- retrying via the idb CLI", e)
        return _cli(argv, timeout)
    _release(w, ok=True)
    return subprocess.CompletedProcess(["idb", *argv], res.get("rc", 1),
                                       res.get("out", ""), res.get("err", ""))


def subprocess_run(cmd: Sequence[str], capture_output: bool = True, text: bool = True,
                   timeout: Optional[float] = None, check: bool = False,
                   **_ignored) -> subprocess.CompletedProcess:
    """Signature-compatible with `subprocess.run([idb, ...], ...)` for TEXT commands,
    so a call site switches by renaming one function. cmd[0] (the idb path) is
    dropped -- the worker is idb. Not for binary output (screenshots)."""
    if subprocess.run is not _REAL_RUN:
        # Tests replace subprocess.run to capture idb taps/reads; honour that so they
        # keep testing the caller's logic instead of driving a real simulator.
        return subprocess.run(list(cmd), capture_output=capture_output, text=text,
                              timeout=timeout, check=check)
    res = run(list(cmd)[1:], timeout=timeout if timeout is not None else 60.0)
    if check and res.returncode != 0:
        raise subprocess.CalledProcessError(res.returncode, list(cmd), res.stdout, res.stderr)
    return res


def shutdown() -> None:
    """Stop every idle worker (tests; the backend just lets them die with it)."""
    global _count
    while True:
        try:
            w = _idle.get_nowait()
        except queue.Empty:
            return
        w.kill()
        with _lock:
            _count -= 1
