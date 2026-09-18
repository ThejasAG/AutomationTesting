"""Preparation must UNINSTALL before it installs.

`simctl install` does not replace a container built from a different derived-data
path; it adds a second one under the same bundle id. `simctl launch` then resolves
that id to whichever container iOS picks. A debug React Native build carries no
main.jsbundle, so driving the stale container against the current Metro renders an
empty root view — a blank screen with a ~3KB page source of empty XCUIElementTypeOther.

That is a silent failure: the build succeeds, the install succeeds, and the run
drives the wrong app. Only ordering catches it, so that is what is asserted here.
"""

from unittest.mock import MagicMock, patch

from automation.projects.preparation import preparation_service


def _build_result(bundle_id="com.example.app"):
    result = MagicMock()
    result.skipped = False
    result.ok = True
    result.artifact_path = "/tmp/Example.app"
    result.bundle_id = bundle_id
    return result


def _run_build_and_install(builder, bundle_id="com.example.app"):
    """Drive _build_and_install with everything but the builder stubbed out."""
    builder.build.return_value = _build_result(bundle_id)
    builder.install.return_value = (True, "Installed Example.app")
    builder.uninstall.return_value = (True, f"Uninstalled {bundle_id}")
    builder.ensure_metro.return_value = (True, "already running")
    builder.launch.return_value = (True, "launched")

    with patch.object(preparation_service, "_update_project"), \
         patch.object(preparation_service, "_set_yaml_app_path"):
        return preparation_service._build_and_install(
            project_id="p1",
            repo_path="/tmp/repo",
            platform="ios",
            device_id="SIM-UDID",
            project_name="Example",
            step=lambda _msg: None,
        )


def test_uninstall_runs_before_install():
    """The stale container must be gone BEFORE the new one lands."""
    with patch("automation.projects.preparation.app_builder") as builder:
        calls = []
        builder.uninstall.side_effect = lambda *a, **k: (calls.append("uninstall"), (True, "Uninstalled"))[1]
        builder.install.side_effect = lambda *a, **k: (calls.append("install"), (True, "Installed"))[1]

        ok, err = _run_build_and_install(builder)

    assert ok, f"preparation failed: {err}"
    assert calls == ["uninstall", "install"], (
        f"expected uninstall then install, got {calls} — installing over a stale "
        "container leaves two apps sharing one bundle id"
    )


def test_uninstall_targets_the_bundle_being_installed():
    with patch("automation.projects.preparation.app_builder") as builder:
        _run_build_and_install(builder, bundle_id="com.vya.consumer")

    builder.uninstall.assert_called_once_with("SIM-UDID", "com.vya.consumer", "ios")


def test_a_failed_uninstall_does_not_block_the_install():
    """Nothing to uninstall is the normal first-install case, not an error."""
    with patch("automation.projects.preparation.app_builder") as builder:
        builder.uninstall.return_value = (False, "No such bundle")
        ok, err = _run_build_and_install(builder)

    assert ok, f"a failed uninstall must not fail preparation: {err}"
    builder.install.assert_called_once()


def test_no_bundle_id_skips_uninstall_and_still_installs():
    """Android builds and native apps may not resolve a bundle id."""
    with patch("automation.projects.preparation.app_builder") as builder:
        ok, err = _run_build_and_install(builder, bundle_id=None)

    assert ok, f"preparation failed: {err}"
    builder.uninstall.assert_not_called()
    builder.install.assert_called_once()
