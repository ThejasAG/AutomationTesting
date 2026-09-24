"""PRODUCT_BUNDLE_IDENTIFIER must never be a global xcodebuild override.

A `SETTING=VALUE` argument to xcodebuild applies to EVERY target in the build,
CocoaPods resource bundles included. Overriding the bundle id that way rewrote
StripePaymentSheetBundle / StripeUICoreBundle / StripeCoreBundle to the app's own
staging id. Stripe's BundleLocator could then no longer find its resource bundle,
`UIImage(named:in:)` returned nil, and the Debug-only assertion in
StripeUICore/ImageMaker.swift:51 --

    assert(image.size != .zero, "Failed to find an image named \\(imageName)")

-- trapped with EXC_BAD_INSTRUCTION (SIGILL) the instant PaymentSheet was presented.
Verified on the installed staging app: every Pod resource bundle carried
CFBundleIdentifier=org.vyapy.sarls.vyaconsumerstaging, while prebuilt GoogleMaps
(not compiled by CocoaPods) correctly kept its own.

The variant is applied with a source_replacement on the app's .pbxproj instead,
which touches only the application target and is reverted after the build.
"""
import json
import pathlib

from automation.projects.environments import EnvironmentConfig

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_bundle_id_is_not_a_global_xcodebuild_override():
    env = EnvironmentConfig(
        project_key="x", environment="staging",
        bundle_id="org.example.app.staging",
    )
    settings = env.xcodebuild_settings()
    assert not any(s.startswith("PRODUCT_BUNDLE_IDENTIFIER=") for s in settings), (
        "PRODUCT_BUNDLE_IDENTIFIER passed globally to xcodebuild — it would also "
        f"rename every CocoaPods resource bundle: {settings}"
    )


def test_other_build_settings_still_pass_through():
    """The guard above must not silently disable the rest of the mechanism."""
    env = EnvironmentConfig(
        project_key="x", environment="staging",
        bundle_id="org.example.app.staging",
        product_name="Example",
        build_settings={"SOME_SETTING": "1"},
    )
    settings = env.xcodebuild_settings()
    assert "PRODUCT_NAME=Example" in settings
    assert "SOME_SETTING=1" in settings


def test_staging_bundle_id_is_applied_by_a_pbxproj_replacement():
    """Dropping the global override is only safe if something else still sets it."""
    cfg = json.loads((ROOT / "project-environments.json").read_text())
    for proj in cfg["projects"]:
        for name, env in proj["environments"].items():
            bid = env.get("bundle_id")
            if not bid or "staging" not in bid:
                continue
            reps = env.get("source_replacements", [])
            pbx = [r for r in reps if r.get("file", "").endswith("project.pbxproj")]
            assert pbx, (
                f"{proj['id']}/{name} declares staging bundle id {bid} but has no "
                f"project.pbxproj source_replacement — with the global override "
                f"removed, nothing would apply it and the build would produce a "
                f"PRODUCTION artifact."
            )
            assert any(bid in r["replace"] for r in pbx), (
                f"{proj['id']}/{name}: pbxproj replacement does not set {bid}"
            )


# ── build inputs must always be reverted ─────────────────────────────────────

def test_a_build_input_edit_is_reverted_even_for_a_debug_metro_build(tmp_path):
    """A .pbxproj edit must not survive the build, unlike a JS edit.

    _env_source_edits deliberately KEEPS edits for a Debug RN build, because the app
    fetches its JS from Metro after xcodebuild has finished. A .pbxproj is not read
    at run time, so keeping it leaves the checkout dirty -- and the next PRODUCTION
    build then finds no production line to replace and silently builds a staging
    artifact, the exact wrong-variant failure this module exists to prevent.
    """
    from automation.projects.builder import app_builder

    repo = tmp_path
    (repo / "package.json").write_text("{}")            # makes it look like RN
    proj = repo / "app.xcodeproj"
    proj.mkdir()
    (proj / "project.pbxproj").write_text("PRODUCT_BUNDLE_IDENTIFIER = a.b.c;\n")
    (repo / "cfg.js").write_text("const URL = 'https://prod.example';\n")

    cfg = EnvironmentConfig(
        project_key="x", environment="staging", configuration="Debug",
        bundle_id="a.b.c.staging",
        source_replacements=[
            {"file": "app.xcodeproj/project.pbxproj",
             "find": "PRODUCT_BUNDLE_IDENTIFIER = a.b.c;",
             "replace": "PRODUCT_BUNDLE_IDENTIFIER = a.b.c.staging;"},
            {"file": "cfg.js",
             "find": "https://prod.example",
             "replace": "https://staging.example"},
        ],
    )

    with app_builder._env_source_edits(str(repo), cfg):
        assert "a.b.c.staging" in (proj / "project.pbxproj").read_text()
        assert "staging.example" in (repo / "cfg.js").read_text()

    # Build input: reverted. Runtime input: kept for Metro.
    assert (proj / "project.pbxproj").read_text() == "PRODUCT_BUNDLE_IDENTIFIER = a.b.c;\n", (
        "the .pbxproj edit survived the build — the next production build would "
        "find no production line to replace"
    )
    assert "staging.example" in (repo / "cfg.js").read_text(), (
        "the JS edit was reverted — a Debug build serves this file from Metro at "
        "run time, so it must stay applied"
    )
