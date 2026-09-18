"""The preflight must tell an environment problem from an application one.

The failure that prompted this: a build died with a Swift module error for
AssetsLibrary, and the platform's advisory blamed react-native-blob-util —
which has no Swift files at all. The real cause was react-native-compressor.
Grepping for a framework name cannot tell those apart, so the distinction
between a Swift import (fatal) and an Objective-C one (compiles with warnings)
is the thing these tests pin down.
"""

import json
import os

from automation.projects import macos_environment as env
from automation.projects.macos_environment import (
    APPLICATION_DEPENDENCY, COCOAPODS, FAIL, INFO, PASS, PATCH, WARN,
    check_assetslibrary, _check_patches, _check_pods, _check_deployment_target,
)


def _pkg(root, name, version="1.0.0", files=None):
    """A node_modules package with iOS sources."""
    d = os.path.join(root, "node_modules", name)
    os.makedirs(os.path.join(d, "ios"), exist_ok=True)
    with open(os.path.join(d, "package.json"), "w") as f:
        json.dump({"name": name, "version": version}, f)
    for rel, text in (files or {}).items():
        full = os.path.join(d, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)
    return d


# ── AssetsLibrary: the distinction that matters ──────────────────────────────

def test_a_swift_import_is_fatal_on_ios_26(tmp_path):
    root = str(tmp_path)
    _pkg(root, "react-native-compressor", "1.10.3",
         {"ios/Video/VideoMain.swift": "import Foundation\nimport AssetsLibrary\n"})

    checks = check_assetslibrary(root, sdk_version="26.0")

    assert len(checks) == 1
    assert checks[0].status == FAIL
    assert checks[0].category == APPLICATION_DEPENDENCY
    assert "react-native-compressor" in checks[0].name
    assert not checks[0].auto_fixable, "nothing can patch in a framework the SDK dropped"


def test_an_objc_import_is_advisory_not_fatal(tmp_path):
    """react-native-blob-util's case: the headers still compile with warnings.

    Reporting this as the blocker is what sent the last investigation at the
    wrong dependency.
    """
    root = str(tmp_path)
    _pkg(root, "react-native-blob-util", "0.19.11",
         {"ios/ReactNativeBlobUtilFS.mm": "#import <AssetsLibrary/AssetsLibrary.h>\n"})

    checks = check_assetslibrary(root, sdk_version="26.0")

    assert len(checks) == 1
    assert checks[0].status == INFO, "an Obj-C reference must not be reported as the blocker"


def test_swift_and_objc_users_are_reported_separately(tmp_path):
    root = str(tmp_path)
    _pkg(root, "react-native-compressor", "1.10.3",
         {"ios/Video/VideoMain.swift": "import AssetsLibrary\n"})
    _pkg(root, "react-native-camera", "4.2.1",
         {"ios/RCT/RCTCameraManager.m": "#import <AssetsLibrary/AssetsLibrary.h>\n"})

    by_status = {c.status for c in check_assetslibrary(root, sdk_version="26.0")}

    assert by_status == {FAIL, INFO}, "the fatal one must not be flattened in with the rest"


def test_an_older_sdk_still_ships_the_framework(tmp_path):
    """Apple removed AssetsLibrary in iOS 26; on 18 the same import builds."""
    root = str(tmp_path)
    _pkg(root, "react-native-compressor", "1.10.3",
         {"ios/Video/VideoMain.swift": "import AssetsLibrary\n"})

    checks = check_assetslibrary(root, sdk_version="18.2")

    assert checks[0].status == WARN, "not fatal on an SDK that still has the framework"


def test_a_podspec_mention_alone_is_not_a_compile_problem(tmp_path):
    root = str(tmp_path)
    d = _pkg(root, "some-pod", "1.0.0")
    with open(os.path.join(d, "some-pod.podspec"), "w") as f:
        f.write('s.frameworks = "AssetsLibrary"\n')

    checks = check_assetslibrary(root, sdk_version="26.0")

    assert [c.status for c in checks] == [INFO]


# ── Pods: the states that produce a sandbox error ────────────────────────────

def _ios(tmp_path, podfile=True, lock=None, manifest=None, pods_proj=True):
    ios = tmp_path / "ios"
    ios.mkdir(exist_ok=True)
    if podfile:
        (ios / "Podfile").write_text("platform :ios, '13.0'\n")
    if lock is not None:
        (ios / "Podfile.lock").write_text(lock)
    pods = ios / "Pods"
    pods.mkdir(exist_ok=True)
    if pods_proj:
        (pods / "Pods.xcodeproj").mkdir(exist_ok=True)
    if manifest is not None:
        (pods / "Manifest.lock").write_text(manifest)
    return str(tmp_path)


def test_locks_that_differ_are_the_sandbox_error(tmp_path):
    root = _ios(tmp_path, lock="PODS: A\n", manifest="PODS: B\n")

    checks = _check_pods(root)

    bad = [c for c in checks if c.status == FAIL]
    assert bad and bad[0].category == COCOAPODS
    assert bad[0].auto_fixable, "pod install fixes this"


def test_matching_locks_pass(tmp_path):
    root = _ios(tmp_path, lock="PODS: A\n", manifest="PODS: A\n")
    assert any(c.status == PASS for c in _check_pods(root))


def test_a_missing_manifest_is_caught_before_xcode_fails_on_it(tmp_path):
    root = _ios(tmp_path, lock="PODS: A\n", manifest=None)

    checks = _check_pods(root)

    assert checks[0].status == FAIL
    assert "Manifest.lock" in checks[0].name


def test_pods_without_an_xcodeproj_is_an_unfinished_install(tmp_path):
    """The directory existing is not proof the install finished."""
    root = _ios(tmp_path, lock="PODS: A\n", manifest="PODS: A\n", pods_proj=False)

    checks = _check_pods(root)

    assert checks[0].status == FAIL
    assert "did not finish" in checks[0].detail


# ── Patches ──────────────────────────────────────────────────────────────────

def _project(tmp_path, pkg_json, patches=None):
    (tmp_path / "package.json").write_text(json.dumps(pkg_json))
    if patches:
        d = tmp_path / "patches"
        d.mkdir(exist_ok=True)
        for name in patches:
            (d / name).write_text("--- a/x\n+++ b/x\n")
    return str(tmp_path)


def test_patches_present_but_patch_package_missing_is_a_failure(tmp_path):
    root = _project(tmp_path, {"dependencies": {}}, ["left-pad+1.0.0.patch"])

    checks = _check_patches(root)

    assert any(c.status == FAIL and c.category == PATCH for c in checks)


def test_patch_package_not_wired_to_postinstall_is_a_failure(tmp_path):
    """Declared but never run is the quiet case: the build uses unpatched source."""
    root = _project(tmp_path,
                    {"devDependencies": {"patch-package": "^8.0.0"},
                     "scripts": {"postinstall": "something-else"}},
                    ["left-pad+1.0.0.patch"])

    checks = _check_patches(root)

    assert any(c.status == FAIL and "postinstall" in c.name for c in checks)


def test_a_patch_for_an_uninstalled_package_is_not_a_failure(tmp_path):
    """react-native-compressor's patch when the package is absent: inert, not broken."""
    root = _project(tmp_path,
                    {"devDependencies": {"patch-package": "^8.0.0"},
                     "scripts": {"postinstall": "patch-package"}},
                    ["react-native-compressor+1.10.3.patch"])

    checks = _check_patches(root)

    statuses = {c.status for c in checks if c.name.startswith("patch ")}
    assert statuses == {INFO}, "an inapplicable patch must not fail the build"


def test_a_version_mismatch_warns_rather_than_rewriting_the_patch(tmp_path):
    root = _project(tmp_path,
                    {"devDependencies": {"patch-package": "^8.0.0"},
                     "scripts": {"postinstall": "patch-package"}},
                    ["left-pad+1.0.0.patch"])
    _pkg(root, "left-pad", "2.0.0")

    checks = _check_patches(root)

    mismatch = [c for c in checks if c.name == "patch left-pad"]
    assert mismatch and mismatch[0].status == WARN
    assert "1.0.0" in mismatch[0].detail and "2.0.0" in mismatch[0].detail


def test_no_patches_directory_is_silent(tmp_path):
    root = _project(tmp_path, {"dependencies": {}})
    assert _check_patches(root) == []


# ── Deployment target: reported, never rewritten ─────────────────────────────

def test_a_low_deployment_target_is_reported_for_the_app_repo(tmp_path):
    ios = tmp_path / "ios" / "app.xcodeproj"
    ios.mkdir(parents=True)
    (ios / "project.pbxproj").write_text(
        "IPHONEOS_DEPLOYMENT_TARGET = 11.0;\nIPHONEOS_DEPLOYMENT_TARGET = 9.0;\n")

    checks = _check_deployment_target(str(tmp_path))

    assert checks[0].status == WARN
    assert not checks[0].auto_fixable, "a tracked app file is not the platform's to edit"
    assert "APPLICATION repo" in checks[0].fix


def test_the_check_does_not_modify_the_project(tmp_path):
    """Detection must be read-only, or it cannot be used to compare two Macs."""
    ios = tmp_path / "ios" / "app.xcodeproj"
    ios.mkdir(parents=True)
    pbx = ios / "project.pbxproj"
    pbx.write_text("IPHONEOS_DEPLOYMENT_TARGET = 11.0;\n")
    before = pbx.read_text()

    _check_deployment_target(str(tmp_path))

    assert pbx.read_text() == before


# ── Comparing two machines ───────────────────────────────────────────────────

def test_compare_surfaces_only_what_differs():
    a = {"host": {"Xcode": "26.0", "Node": "20.19.0"}, "project": {}, "checks": []}
    b = {"host": {"Xcode": "26.0.1", "Node": "20.19.0"}, "project": {}, "checks": []}

    out = env.compare(a, b, "GOOD", "FAILING")

    assert "Xcode" in out and "26.0.1" in out
    assert "Node" not in out, "identical values are noise when diffing two Macs"


def test_compare_surfaces_differing_check_results():
    a = {"host": {}, "project": {}, "checks": [{"name": "Pod state", "status": "PASS"}]}
    b = {"host": {}, "project": {}, "checks": [{"name": "Pod state", "status": "FAIL"}]}

    assert "Pod state" in env.compare(a, b)


def test_doctor_runs_without_a_repository():
    """The host half must work on a machine with no project checked out yet."""
    report = env.doctor()

    assert report.host.get("Architecture")
    assert report.checks


# ── The builder's own advisory must name the Swift importer ──────────────────

def test_builder_blames_the_swift_importer_not_the_objc_one(tmp_path):
    """The regression this whole preflight came from.

    The advisory blamed react-native-blob-util (Objective-C only, no Swift files
    at all) for a Swift module failure that react-native-compressor caused.
    """
    from automation.projects.builder import app_builder

    root = str(tmp_path)
    _pkg(root, "react-native-blob-util", "0.19.11",
         {"ios/ReactNativeBlobUtilFS.mm": "#import <AssetsLibrary/AssetsLibrary.h>\n"})
    _pkg(root, "react-native-compressor", "1.10.3",
         {"ios/Video/VideoMain.swift": "import Foundation\nimport AssetsLibrary\n"})

    found = app_builder._swift_importers_of_removed_frameworks(
        root, {"AssetsLibrary": "26.0"})

    assert len(found) == 1, "only the Swift importer is a blocker"
    assert "react-native-compressor" in found[0]
    assert "VideoMain.swift" in found[0], "name the file, so it can be verified"
    assert "BUILD BLOCKED" in found[0]


def test_builder_finds_no_swift_importer_when_there_is_none(tmp_path):
    from automation.projects.builder import app_builder

    root = str(tmp_path)
    _pkg(root, "react-native-blob-util", "0.19.11",
         {"ios/ReactNativeBlobUtilFS.mm": "#import <AssetsLibrary/AssetsLibrary.h>\n"})

    assert app_builder._swift_importers_of_removed_frameworks(
        root, {"AssetsLibrary": "26.0"}) == []
