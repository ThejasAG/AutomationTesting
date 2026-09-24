"""Environment-aware builds: config resolution, artifact identity, variant safety.

The failure these guard against: the staging bundle id lived only in local, unpushed
commits, so wiping repos/ made a *staging* build silently produce a *production*
artifact. It built green, installed fine, and only surfaced as "…staging is not
installed" at scenario preflight.

These are unit tests on purpose — they exercise resolution, matching and rebuild
decisions without running xcodebuild (a real build is ~20-30 min, so a branch x
environment matrix built for real would be hours). The one real end-to-end build is
run separately.
"""
import json
import os
import plistlib

import pytest

from automation.projects import environments as E
from automation.projects.builder import app_builder


# ── config resolution ────────────────────────────────────────────────────────

def test_staging_and_production_resolve_to_different_bundles():
    prod = E.resolve("production", bundle_id="org.vyapy.sarls.vyaconsumer")
    stg = E.resolve("staging", bundle_id="org.vyapy.sarls.vyaconsumer")
    assert prod.bundle_id != stg.bundle_id
    assert stg.bundle_id.endswith("staging")


def test_prod_is_an_alias_for_production():
    """The scenario path says "prod"; most prose says "production". Aliases are
    config, not a special case in code."""
    a = E.resolve("prod", bundle_id="org.vyapy.sarls.vyaconsumer")
    b = E.resolve("production", bundle_id="org.vyapy.sarls.vyaconsumer")
    assert a.environment == b.environment == "production"
    assert a.bundle_id == b.bundle_id


def test_a_project_resolves_from_either_of_its_bundle_ids():
    """Asking with the prod bundle must still find the staging config, because a
    project row carries only ONE id and it may be either."""
    from_prod = E.resolve("staging", bundle_id="org.vyapy.sarls.vyaconsumer")
    from_stg = E.resolve("staging", bundle_id="org.vyapy.sarls.vyaconsumerstaging")
    assert from_prod.bundle_id == from_stg.bundle_id


def test_an_unconfigured_environment_is_refused_not_guessed():
    with pytest.raises(E.EnvironmentNotConfigured) as e:
        E.resolve("qa", bundle_id="org.vyapy.sarls.vyaconsumer")
    assert "ENVIRONMENT NOT CONFIGURED" in str(e.value)
    assert "production, staging" in str(e.value)     # says what IS available


def test_an_unknown_project_is_refused_not_defaulted_to_production():
    with pytest.raises(E.EnvironmentNotConfigured):
        E.resolve("staging", bundle_id="com.example.nothing-we-know")


def test_environment_is_looked_up_not_derived_from_the_name():
    """`bundle.endswith("staging")` is what the old orchestrator did. It breaks for
    any app whose name merely ends that way, so the mapping is config."""
    assert E.environment_for_bundle("org.vyapy.sarls.vyaconsumerstaging") == "staging"
    assert E.environment_for_bundle("org.vyapy.sarls.vyaconsumer") == "production"
    assert E.environment_for_bundle("com.example.unknown") is None


def test_business_project_is_configured_independently():
    """Multiple projects — the mechanism is not Consumer-specific."""
    c = E.resolve("staging", bundle_id="org.vyapy.sarls.vyaconsumer")
    b = E.resolve("staging", bundle_id="org.vyapy.sarls.vyabusinessipad")
    assert c.project_key != b.project_key
    assert c.bundle_id != b.bundle_id


def test_build_settings_are_emitted_for_xcodebuild():
    """The bundle id is deliberately NOT among them.

    It used to be, and a command-line override reaches every target in the build --
    it renamed the CocoaPods resource bundles too, which crashed PaymentSheet on
    launch (see tests/test_bundle_id_scoping.py). The variant is applied by a
    .pbxproj source_replacement scoped to the app target instead.
    """
    cfg = E.resolve("staging", bundle_id="org.vyapy.sarls.vyaconsumer")
    settings = cfg.xcodebuild_settings()
    assert not any(x.startswith("PRODUCT_BUNDLE_IDENTIFIER=") for x in settings)


def test_explicit_build_settings_win_over_the_shorthand():
    cfg = E.EnvironmentConfig(project_key="x", environment="staging",
                              bundle_id="a.b.c",
                              build_settings={"PRODUCT_BUNDLE_IDENTIFIER": "z.z.z"})
    assert "PRODUCT_BUNDLE_IDENTIFIER=z.z.z" in cfg.xcodebuild_settings()


# ── no hardcoding ────────────────────────────────────────────────────────────

_SRC = ("automation/projects/environments.py", "automation/projects/deployment.py")


def _read(rel):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, rel)) as f:
        return f.read()


def test_no_hardcoded_bundle_ids_or_branches_in_the_generic_layer():
    """Project specifics belong in project-environments.json, not in code."""
    for rel in _SRC:
        src = _read(rel)
        assert "org.vyapy" not in src, f"{rel} hardcodes a bundle id"
        assert "preprod-2-May18" not in src, f"{rel} hardcodes a branch"


def test_no_hardcoded_device_udids_or_developer_paths():
    for rel in _SRC:
        src = _read(rel)
        assert "/Users/" not in src, f"{rel} hardcodes a developer path"
        # Simulator UDIDs are 8-4-4-4-12 hex; the giveaway is a long hex run.
        assert "DA24A392" not in src and "D19D3EC7" not in src


# ── artifact identity + validation ───────────────────────────────────────────

def _fake_app(tmp_path, bundle_id, *, name="vyaconsumer.app", macho=True,
              executable="vyaconsumer"):
    app = tmp_path / name
    app.mkdir(parents=True, exist_ok=True)
    with open(app / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleIdentifier": bundle_id,
                       "CFBundleExecutable": executable,
                       "CFBundleShortVersionString": "1.0.2",
                       "CFBundleVersion": "31.0"}, f)
    with open(app / executable, "wb") as f:
        f.write(b"\xcf\xfa\xed\xfe" if macho else b"#!/bin/sh\n")
    return str(app)


def test_a_production_artifact_cannot_satisfy_a_staging_request(tmp_path):
    """The whole point. Artifact A (production) must never answer a staging ask."""
    app = _fake_app(tmp_path, "org.vyapy.sarls.vyaconsumer")
    ok, why = app_builder.validate_artifact(
        app, expected_bundle_id="org.vyapy.sarls.vyaconsumerstaging",
        environment="staging")
    assert not ok
    assert "BUILD VALIDATION FAILED" in why
    assert "Deployment blocked" in why


def test_a_staging_artifact_cannot_satisfy_a_production_request(tmp_path):
    app = _fake_app(tmp_path, "org.vyapy.sarls.vyaconsumerstaging")
    ok, _ = app_builder.validate_artifact(
        app, expected_bundle_id="org.vyapy.sarls.vyaconsumer",
        environment="production")
    assert not ok


def test_a_matching_artifact_validates(tmp_path):
    app = _fake_app(tmp_path, "org.vyapy.sarls.vyaconsumerstaging")
    ok, _ = app_builder.validate_artifact(
        app, expected_bundle_id="org.vyapy.sarls.vyaconsumerstaging",
        environment="staging")
    assert ok


def test_a_non_macho_executable_is_rejected(tmp_path):
    """Installs fine, then dies at launch with nothing useful in the log."""
    app = _fake_app(tmp_path, "a.b.c", macho=False)
    ok, why = app_builder.validate_artifact(app, expected_bundle_id="a.b.c")
    assert not ok and "Mach-O" in why


def test_an_incomplete_app_is_rejected(tmp_path):
    """An interrupted build leaves an .app whose executable never got written."""
    app = _fake_app(tmp_path, "a.b.c")
    os.remove(os.path.join(app, "vyaconsumer"))
    ok, why = app_builder.validate_artifact(app, expected_bundle_id="a.b.c")
    assert not ok and "incomplete" in why.lower()


def test_artifact_metadata_records_branch_commit_and_environment(tmp_path):
    app = _fake_app(tmp_path, "org.vyapy.sarls.vyaconsumerstaging")
    app_builder._write_artifact_metadata(
        app, repo_path=str(tmp_path), commit="abc123", environment="staging",
        bundle_id="org.vyapy.sarls.vyaconsumerstaging", configuration="Debug")
    meta = app_builder.artifact_metadata(app)
    assert meta["environment"] == "staging"
    assert meta["commit"] == "abc123"
    assert meta["bundle_id"] == "org.vyapy.sarls.vyaconsumerstaging"
    assert meta["built_at"]


def test_metadata_is_absent_not_fatal_for_older_artifacts(tmp_path):
    app = _fake_app(tmp_path, "a.b.c")
    assert app_builder.artifact_metadata(app) == {}


# ── artifact selection (the production/staging mix-up) ───────────────────────

def _products(tmp_path, configuration="Debug"):
    d = tmp_path / "Build" / "Products" / f"{configuration}-iphonesimulator"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_selection_picks_the_requested_variant_not_the_first_alphabetically(tmp_path):
    """Two .apps in one Products dir: sorted()[0] returned whichever sorted first,
    so a staging request could be answered with the production build."""
    d = _products(tmp_path)
    _fake_app(d, "org.vyapy.sarls.vyaconsumer", name="AAAprod.app",
              executable="AAAprod")
    want = _fake_app(d, "org.vyapy.sarls.vyaconsumerstaging", name="ZZZstaging.app",
                     executable="ZZZstaging")
    got = app_builder._find_built_app(
        str(tmp_path), bundle_id="org.vyapy.sarls.vyaconsumerstaging")
    assert got == want


def test_selection_returns_nothing_rather_than_the_wrong_variant(tmp_path):
    """No staging build present must mean "none", never "here's the prod one"."""
    d = _products(tmp_path)
    _fake_app(d, "org.vyapy.sarls.vyaconsumer", name="prod.app", executable="prod")
    assert app_builder._find_built_app(
        str(tmp_path), bundle_id="org.vyapy.sarls.vyaconsumerstaging") is None


def test_selection_honours_the_configuration_directory(tmp_path):
    d = _products(tmp_path, configuration="Release")
    want = _fake_app(d, "a.b.c", name="rel.app", executable="rel")
    assert app_builder._find_built_app(str(tmp_path), configuration="Release") == want
    assert app_builder._find_built_app(str(tmp_path), configuration="Debug") is None
