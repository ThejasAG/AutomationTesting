"""Name the pod that uses a framework Apple has removed.

The build failed with:

    error: 'ALAssetsLibrary' is unavailable in iOS: Use PHPhotoLibrary from the
           Photos framework instead

pointing at Apple's own SDK header and naming no dependency at all. Four
packages in the project referenced AssetsLibrary and only ONE of them actually
broke, so finding the culprit took most of a day.

The distinction that matters: Apple's Objective-C headers still declare these
symbols, and code using them compiles with a deprecation WARNING. It is the
Swift interface that marks them

    @available(iOS, introduced: 9.0, deprecated: 9.0, obsoleted: 26.0)

and turns them into a hard error. So a pod carrying even one Swift file fails,
while pure Objective-C pods using the same framework build fine — which is
exactly what happened: FBSDKCoreKit (4 Swift files) broke; react-native-camera,
react-native-contacts and react-native-blob-util did not.

Nothing here is repairable — the symbol is gone from the SDK. This only turns a
confusing compiler error into a sentence naming the dependency.
"""
import os

import pytest

from automation.projects.builder import app_builder


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "ios" / "Pods").mkdir(parents=True)
    return str(tmp_path)


def _podspec(repo, name, *, links=None, mentions=None):
    """A podspec that LINKS a framework, and/or source that merely mentions it.

    The distinction is the whole point: only `frameworks` puts -framework on the
    link line. A source file naming the framework — or looking a class up
    dynamically, as FBSDKCoreKit does — is not linkage.
    """
    d = os.path.join(repo, "ios", "Pods", "Local Podspecs")
    os.makedirs(d, exist_ok=True)
    spec = {"name": name}
    if links:
        spec["frameworks"] = links
    import json as _json
    open(os.path.join(d, f"{name}.podspec.json"), "w").write(_json.dumps(spec))

    if mentions:
        s = os.path.join(repo, "ios", "Pods", name)
        os.makedirs(s, exist_ok=True)
        open(os.path.join(s, f"{name}.m"), "w").write(
            f"Class {name}_lookup(void) {{ return NSClassFromString(@\"AL{mentions}\"); }}\n")
        open(os.path.join(s, f"{name}.swift"), "w").write("import Foundation\n")


# ── reading the SDK, not a hardcoded list ───────────────────────────────────

def test_01_obsoleted_frameworks_come_from_the_sdk():
    """A hardcoded list would be wrong the day the next SDK ships."""
    found = app_builder._obsoleted_frameworks()
    assert isinstance(found, dict)
    # On an iOS 26 SDK this is AssetsLibrary; on older ones it may be empty.
    for fw, ver in found.items():
        assert fw and ver[0].isdigit()


def test_02_an_unreadable_sdk_is_silent_not_fatal(monkeypatch):
    """A missing warning must never become a failed build."""
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no xcrun")))
    assert app_builder._obsoleted_frameworks() == {}


# ── linkage, not mention ────────────────────────────────────────────────────

def test_03_a_pod_that_links_the_framework_is_named(repo, monkeypatch):
    monkeypatch.setattr(app_builder, "_obsoleted_frameworks",
                        lambda: {"AssetsLibrary": "26.0"})
    _podspec(repo, "react-native-blob-util", links="AssetsLibrary")
    warnings = app_builder.removed_framework_users(repo)
    assert len(warnings) == 1
    assert "react-native-blob-util" in warnings[0]


def test_04_a_dynamic_lookup_is_not_linkage(repo, monkeypatch):
    """The false positive that nearly cost an app-repo change.

    FBSDKCoreKit names ALAssetsLibrary only in a runtime lookup
    (fbsdkdfl_ALAssetsLibraryClass) and its Swift files never import the
    framework. The linker never sees it, and the build succeeds.
    """
    monkeypatch.setattr(app_builder, "_obsoleted_frameworks",
                        lambda: {"AssetsLibrary": "26.0"})
    _podspec(repo, "FBSDKCoreKit", mentions="AssetsLibrary")
    assert app_builder.removed_framework_users(repo) == [], \
        "blamed a pod that only looks the class up by name"


def test_05_only_the_linking_pod_is_named_among_many(repo, monkeypatch):
    monkeypatch.setattr(app_builder, "_obsoleted_frameworks",
                        lambda: {"AssetsLibrary": "26.0"})
    _podspec(repo, "FBSDKCoreKit", mentions="AssetsLibrary")
    _podspec(repo, "react-native-camera", mentions="AssetsLibrary")
    _podspec(repo, "react-native-blob-util", links="AssetsLibrary")
    warnings = app_builder.removed_framework_users(repo)
    assert len(warnings) == 1 and "blob-util" in warnings[0]


def test_06_a_pod_linking_something_else_is_not_flagged(repo, monkeypatch):
    monkeypatch.setattr(app_builder, "_obsoleted_frameworks",
                        lambda: {"AssetsLibrary": "26.0"})
    _podspec(repo, "SomePod", links="Photos")
    assert app_builder.removed_framework_users(repo) == []


def test_07_no_removed_frameworks_means_no_warnings(repo, monkeypatch):
    monkeypatch.setattr(app_builder, "_obsoleted_frameworks", lambda: {})
    _podspec(repo, "react-native-blob-util", links="AssetsLibrary")
    assert app_builder.removed_framework_users(repo) == []


def test_08_a_project_without_pods_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(app_builder, "_obsoleted_frameworks",
                        lambda: {"AssetsLibrary": "26.0"})
    assert app_builder.removed_framework_users(str(tmp_path)) == []


# ── the message must not overstate ──────────────────────────────────────────

def test_09_the_warning_is_marked_advisory(repo, monkeypatch):
    """Proved by a real build: with the gnu++14 fix applied this project builds
    SUCCESSFULLY while still linking AssetsLibrary. The ObjC headers compile
    with a deprecation warning, so a removed framework is not automatically the
    blocker — and saying otherwise sends someone to change the wrong repo."""
    monkeypatch.setattr(app_builder, "_obsoleted_frameworks",
                        lambda: {"AssetsLibrary": "26.0"})
    _podspec(repo, "react-native-blob-util", links="AssetsLibrary")
    msg = app_builder.removed_framework_users(repo)[0]
    assert "advisory" in msg.lower()
    assert "may not be what fails the build" in msg


def test_10_it_says_what_would_actually_be_fatal(repo, monkeypatch):
    monkeypatch.setattr(app_builder, "_obsoleted_frameworks",
                        lambda: {"AssetsLibrary": "26.0"})
    _podspec(repo, "react-native-blob-util", links="AssetsLibrary")
    msg = app_builder.removed_framework_users(repo)[0]
    assert "Swift file imports" in msg


def test_11_the_build_failure_carries_the_warning():
    import inspect
    src = inspect.getsource(app_builder.build_ios)
    assert src.count("removed_framework_users") >= 2
