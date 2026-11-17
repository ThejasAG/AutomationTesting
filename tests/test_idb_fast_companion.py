"""A dead idb companion is revived instead of leaving every read of its device empty
(automation/scenarios/idb_fast.py). MEASURED 2026-11-05, run 3d1d2d6b: the participant
iPhone's companion died at 11:11 and segment 2 'never saw' the Wallet at 11:44."""
import json
import subprocess

from automation.scenarios import idb_fast

DEAD = "Failed to connect to companion at address DomainSocketAddress(path='/tmp/idb/X_companion.sock'): [Errno 61] Connection refused"


def _state(tmp_path, entries):
    p = tmp_path / "state"
    p.write_text(json.dumps(entries))
    return p


def test_a_dead_companion_is_taken_out_of_the_state_with_its_socket(tmp_path):
    sock = tmp_path / "PHONE_companion.sock"
    sock.write_text("")
    p = _state(tmp_path, [{"udid": "PHONE", "pid": 999999, "path": str(sock)},
                          {"udid": "IPAD", "pid": 1, "path": "/tmp/idb/IPAD_companion.sock"}])
    assert idb_fast.revive_companion("PHONE", str(p))
    assert [t["udid"] for t in json.loads(p.read_text())] == ["IPAD"]
    assert not sock.exists()


def test_a_live_companion_is_left_alone(tmp_path):
    import os
    p = _state(tmp_path, [{"udid": "PHONE", "pid": os.getpid(), "path": "/x_companion.sock"}])
    assert not idb_fast.revive_companion("PHONE", str(p))
    assert json.loads(p.read_text())[0]["udid"] == "PHONE"


def test_a_read_that_hits_a_dead_companion_is_retried_once(monkeypatch):
    calls = []
    monkeypatch.setattr(idb_fast, "_LAST_SCREEN", {})
    monkeypatch.setattr(idb_fast, "_run", lambda argv, t: subprocess.CompletedProcess(
        argv, 1, "", DEAD))
    monkeypatch.setattr(idb_fast, "revive_companion", lambda udid, state_path="": True)
    monkeypatch.setattr(idb_fast, "_cli", lambda argv, t: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0, "[]", ""))
    idb_fast._revived.clear()
    res = idb_fast.run(["ui", "describe-all", "--udid", "PHONE"])
    assert res.returncode == 0 and calls == [["ui", "describe-all", "--udid", "PHONE"]]
    # Not again within 30 s: a companion that keeps dying is not restarted in a loop.
    calls.clear()
    assert idb_fast.run(["ui", "describe-all", "--udid", "PHONE"]).returncode == 1
    assert calls == []


def test_other_failures_are_not_retried(monkeypatch):
    monkeypatch.setattr(idb_fast, "_run", lambda argv, t: subprocess.CompletedProcess(
        argv, 1, "", "some other error"))
    monkeypatch.setattr(idb_fast, "revive_companion",
                        lambda udid, state_path="": (_ for _ in ()).throw(AssertionError))
    idb_fast._revived.clear()
    assert idb_fast.run(["ui", "tap", "--udid", "PHONE", "1", "2"]).returncode == 1
