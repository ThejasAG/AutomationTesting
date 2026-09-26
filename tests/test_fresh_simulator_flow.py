"""A flow must work on a simulator the app has never run on.

Every one of these was hit running the Quick demo on a new Mac's own (iOS 26.5)
simulator instead of the original developer's pre-configured one.
"""
import inspect
from unittest.mock import patch

from automation.projects import deployment
from automation.scenarios import cross_app_flows as F
from automation.scenarios import cross_app_orchestrator as O


def test_consumer_prewarm_sets_metro_before_attaching():
    # The prewarm launches the app; without Metro set first the session held a
    # "No bundle URL present" instance for the whole run.
    src = inspect.getsource(F.FlowRunner._prewarm_sessions)
    assert "(self.consumer_bundle, 8100, True)" in src
    assert "ensure_app_metro" in src


def test_changed_metro_location_relaunches_the_app():
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        class R:
            stdout = "" if "read" in cmd else ""
            returncode = 0
        return R()
    with patch.object(O.subprocess, "run", run), \
         patch.object(O.httpx, "get", lambda *a, **k: type("r", (), {"status_code": 200})()), \
         patch.object(O, "_business_metro_target", lambda b: (8084, "p")):
        O.ensure_app_metro("UDID", "org.vyapy.sarls.vyaconsumerstaging")
    assert any("write" in c and "localhost:8084" in c for c in calls)
    assert any(c[:3] == ["xcrun", "simctl", "terminate"] for c in calls), \
        "the running instance must be ended so the new location is read"


def test_unchanged_metro_location_does_not_kill_the_app():
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return type("R", (), {"stdout": "localhost:8084\n", "returncode": 0})()
    with patch.object(O.subprocess, "run", run), \
         patch.object(O.httpx, "get", lambda *a, **k: type("r", (), {"status_code": 200})()), \
         patch.object(O, "_business_metro_target", lambda b: (8084, "p")):
        O.ensure_app_metro("UDID", "org.vyapy.sarls.vyaconsumerstaging")
    assert not any("terminate" in c for c in calls)


def test_deployment_pre_grants_all_permissions():
    calls = []
    req = deployment.AppRequirement.__new__(deployment.AppRequirement)
    req.platform, req.device_id, req.bundle_id, req.role = "ios", "U", "b", "consumer"
    with patch.object(deployment.app_builder, "_configure_ios_location", lambda *a: None), \
         patch.object(deployment.subprocess, "run", lambda cmd, **k: calls.append(cmd)):
        deployment._grant_device_permissions(req)
    assert ["xcrun", "simctl", "privacy", "U", "grant", "all", "b"] in calls
    # contacts explicitly, and LAST, so it ends at full access
    assert calls[-1] == ["xcrun", "simctl", "privacy", "U", "grant", "contacts", "b"]


def test_consumer_sign_in_types_through_verified_fill():
    src = inspect.getsource(F.FlowRunner._sign_in_consumer)
    assert "_fill_field(" in src, "raw send_keys dropped characters on a busy sim"
    assert "still on the sign-in screen" in src, "must not claim success blindly"
