"""One run per simulator.

Appium allows one session per device; a second run opening a session on a busy
simulator killed the first run's session mid-step (InvalidSessionIdException).
Seen when Retry was pressed while a waiter demo was still running: every
overlapping run failed. A run now waits for its devices.
"""
import threading
import time

from automation.scenarios import cross_app_flows as caf


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
    assert caf._DEVICE_OWNERS == {}


def test_all_or_nothing():
    _clean()
    ev = threading.Event()
    assert caf.acquire_devices("A", ["IPAD"], ev)
    assert caf.acquire_devices("B", ["IPHONE", "IPAD"], ev, timeout=0.1) is False
    assert "IPHONE" not in caf._DEVICE_OWNERS, "must not hold a partial claim"
    _clean()


def test_flow_udids_cover_every_role():
    r = caf.FlowRunner.__new__(caf.FlowRunner)
    r.devices = {"consumer": "PHONE", "waiter": "IPAD", "kitchen": "IPAD"}
    r.flow = {"segments": [{"role": "consumer"}, {"role": "waiter"}, {"role": "kitchen"}]}
    assert r._flow_udids() == ["IPAD", "PHONE"]
