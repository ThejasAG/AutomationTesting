"""Fresh iOS 26 simulators run mediaanalysisd/photoanalysisd at ~400% CPU each.

On an Intel Mac that starved the build and made `simctl install` on the iPad
time out after 300s during Latest build. The platform turns them off on every
simulator it boots.
"""
from automation.projects import simulators as sims


def _capture(monkeypatch):
    calls = []

    class R:
        returncode, stdout, stderr = 0, "", ""
    monkeypatch.setattr(sims.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or R())
    monkeypatch.setattr(sims, "_quieted", set())
    return calls


def test_indexers_are_disabled_and_stopped(monkeypatch):
    calls = _capture(monkeypatch)
    assert sims.quiet_background_daemons("UDID-1")
    for label in ("com.apple.mediaanalysisd", "com.apple.photoanalysisd"):
        for verb in ("disable", "bootout"):
            assert ["xcrun", "simctl", "spawn", "UDID-1", "launchctl", verb,
                    f"system/{label}"] in calls


def test_done_once_per_simulator(monkeypatch):
    calls = _capture(monkeypatch)
    sims.quiet_background_daemons("UDID-1")
    n = len(calls)
    sims.quiet_background_daemons("UDID-1")
    assert len(calls) == n


def test_a_failing_spawn_never_raises(monkeypatch):
    monkeypatch.setattr(sims, "_quieted", set())

    def boom(cmd, **kw):
        raise sims.subprocess.TimeoutExpired(cmd, 60)
    monkeypatch.setattr(sims.subprocess, "run", boom)
    assert sims.quiet_background_daemons("UDID-2") is False
    assert "UDID-2" not in sims._quieted, "must retry next time"


def test_only_booted_simulators_are_touched(monkeypatch):
    _capture(monkeypatch)
    monkeypatch.setattr(sims, "list_sims", lambda: [
        {"udid": "A", "state": "Booted"}, {"udid": "B", "state": "Shutdown"}])
    assert sims.quiet_booted() == ["A"]


def test_booting_for_a_deploy_quiets_the_simulator(monkeypatch):
    from automation.projects import builder
    seen = []
    monkeypatch.setattr(builder, "_run", lambda cmd, **kw: (True, "Booted"))
    monkeypatch.setattr(builder._simulators, "quiet_background_daemons", seen.append)
    b = builder.AppBuilder.__new__(builder.AppBuilder)
    monkeypatch.setattr(b, "_open_simulator_ui", lambda: None, raising=False)
    ok, _ = b.ensure_ios_booted("UDID-3")
    assert ok and seen == ["UDID-3"]
