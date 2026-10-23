"""Prepare must start Metro on the environment's port and point the app at it.

The staging app (Metro :8084) was launched with no RCT_jsLocation, so it asked
the default :8081 -- where RN's own "Start Packager" build phase had left a
Metro serving a stale bundle -- and died with "Could not get BatchedBridge".
"""
from automation.projects.builder import _build_env


def test_build_env_stops_xcode_launching_its_own_metro():
    assert _build_env().get("RCT_NO_LAUNCH_PACKAGER")


def test_prepare_passes_port_device_and_bundle_to_metro():
    import inspect
    from automation.projects import preparation
    src = inspect.getsource(preparation.ProjectPreparationService._build_and_install)
    call = src[src.index("app_builder.ensure_metro("):]
    call = call[:call.index("bundle_id=") + 40]
    for arg in ("port=", "udid=", "bundle_id="):
        assert arg in call, f"ensure_metro called without {arg}"
