"""One run per simulator.

Appium allows one session per device; a second run opening a session on a busy
simulator killed the first run's session mid-step (InvalidSessionIdException).
Seen when Retry was pressed while a waiter demo was still running: every
overlapping run failed. A run now waits for its devices.
"""
import subprocess
import sys
import threading
import time

import pytest

from automation.scenarios import cross_app_flows as caf


@pytest.fixture(autouse=True)
def _lock_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(caf, "DEVICE_LOCK_DIR", str(tmp_path / "locks"))


def _clean():
    caf.release_devices("A")
    caf.release_devices("B")


def test_second_run_waits_until_first_releases():
    _clean()
    ev = threading.Event()
    assert caf.acquire_devices("A", ["IPAD"], ev)
    waited, got = [], []

    def second():
        got.append(caf.acquire_devices("B", ["IPAD"], ev,
                                       on_wait=lambda o, u: waited.append((o, u))))
    t = threading.Thread(target=second)
    t.start()
    time.sleep(0.3)
    assert not got and waited == [("A", "IPAD")], "must wait and say who holds it"
    caf.release_devices("A")
    t.join(5)
    assert got == [True]
    _clean()


def test_disjoint_devices_run_in_parallel():
    _clean()
    ev = threading.Event()
    assert caf.acquire_devices("A", ["IPHONE"], ev)
    assert caf.acquire_devices("B", ["IPAD"], ev, timeout=0.1)
    _clean()


def test_stop_while_waiting_gives_up_without_claiming():
    _clean()
    assert caf.acquire_devices("A", ["IPAD", "IPHONE"], threading.Event())
    cancel = threading.Event()
    cancel.set()
    assert caf.acquire_devices("B", ["IPHONE"], cancel) is False
    caf.release_devices("A")
    assert caf._HELD == {}


def test_all_or_nothing():
    _clean()
    ev = threading.Event()
    assert caf.acquire_devices("A", ["IPAD"], ev)
    assert caf.acquire_devices("B", ["IPHONE", "IPAD"], ev, timeout=0.1) is False
    # IPHONE must not be left claimed by B's failed attempt.
    assert caf.acquire_devices("C", ["IPHONE"], ev, timeout=0.1)
    caf.release_devices("C")
    _clean()


def test_a_run_in_another_process_is_waited_for(tmp_path):
    """scripts/dryrun.py and the backend are separate processes."""
    lockdir = caf.DEVICE_LOCK_DIR
    holder = subprocess.Popen([sys.executable, "-c", f"""
import fcntl, os, sys, time
os.makedirs({lockdir!r}, exist_ok=True)
fd = os.open(os.path.join({lockdir!r}, "IPAD.lock"), os.O_RDWR | os.O_CREAT)
fcntl.flock(fd, fcntl.LOCK_EX); os.pwrite(fd, b"other-proc-run", 0)
print("held", flush=True); time.sleep(1.5)
"""], stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"
    waited = []
    t0 = time.time()
    assert caf.acquire_devices("B", ["IPAD"], threading.Event(),
                               on_wait=lambda o, u: waited.append(o))
    assert waited == ["other-proc-run"] and time.time() - t0 > 0.5
    caf.release_devices("B")
    holder.wait(5)


def test_a_dead_process_never_leaves_a_device_locked():
    lockdir = caf.DEVICE_LOCK_DIR
    subprocess.run([sys.executable, "-c", f"""
import fcntl, os
os.makedirs({lockdir!r}, exist_ok=True)
fd = os.open(os.path.join({lockdir!r}, "IPAD.lock"), os.O_RDWR | os.O_CREAT)
fcntl.flock(fd, fcntl.LOCK_EX); os._exit(1)
"""], check=False)
    assert caf.acquire_devices("B", ["IPAD"], threading.Event(), timeout=0.5)
    caf.release_devices("B")


def test_flow_udids_cover_every_role():
    r = caf.FlowRunner.__new__(caf.FlowRunner)
    r.devices = {"consumer": "PHONE", "waiter": "IPAD", "kitchen": "IPAD"}
    r.flow = {"segments": [{"role": "consumer"}, {"role": "waiter"}, {"role": "kitchen"}]}
    assert r._flow_udids() == ["IPAD", "PHONE"]
