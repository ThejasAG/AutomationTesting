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


def _pod(repo, name, *, framework=None, swift=False):
    d = os.path.join(repo, "ios", "Pods", name)
    os.makedirs(d, exist_ok=True)
    body = f'#import <{framework}/{framework}.h>\n' if framework else "// nothing\n"
    open(os.path.join(d, f"{name}.m"), "w").write(body)
    if swift:
        open(os.path.join(d, f"{name}.swift"), "w").write("import Foundation\n")
    return d


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


# ── which pods are actually at risk ─────────────────────────────────────────

def test_03_a_swift_pod_using_a_removed_framework_is_named(repo, monkeypatch):
    monkeypatch.setattr(app_builder, "_obsoleted_frameworks",
                        lambda: {"AssetsLibrary": "26.0"})
    _pod(repo, "FBSDKCoreKit", framework="AssetsLibrary", swift=True)
    warnings = app_builder.removed_framework_users(repo)
    assert len(warnings) == 1
    assert "FBSDKCoreKit" in warnings[0] and "AssetsLibrary" in warnings[0]


def test_04_a_pure_objc_pod_is_not_flagged(repo, monkeypatch):
    """The whole point. These compile with a deprecation warning and were the
    three false leads that cost a day."""
    monkeypatch.setattr(app_builder, "_obsoleted_frameworks",
                        lambda: {"AssetsLibrary": "26.0"})
    for name in ("react-native-camera", "react-native-contacts",
                 "react-native-blob-util"):
        _pod(repo, name, framework="AssetsLibrary", swift=False)
    assert app_builder.removed_framework_users(repo) == []


def test_05_only_the_swift_pod_is_named_among_many(repo, monkeypatch):
    monkeypatch.setattr(app_builder, "_obsoleted_frameworks",
                        lambda: {"AssetsLibrary": "26.0"})
    _pod(repo, "react-native-camera", framework="AssetsLibrary")
    _pod(repo, "react-native-contacts", framework="AssetsLibrary")
    _pod(repo, "FBSDKCoreKit", framework="AssetsLibrary", swift=True)
    warnings = app_builder.removed_framework_users(repo)
    assert len(warnings) == 1 and "FBSDKCoreKit" in warnings[0]


def test_06_a_swift_pod_not_using_it_is_not_flagged(repo, monkeypatch):
    monkeypatch.setattr(app_builder, "_obsoleted_frameworks",
                        lambda: {"AssetsLibrary": "26.0"})
    _pod(repo, "SomeSwiftPod", framework="Photos", swift=True)
    assert app_builder.removed_framework_users(repo) == []


def test_07_no_removed_frameworks_means_no_warnings(repo, monkeypatch):
    monkeypatch.setattr(app_builder, "_obsoleted_frameworks", lambda: {})
    _pod(repo, "FBSDKCoreKit", framework="AssetsLibrary", swift=True)
    assert app_builder.removed_framework_users(repo) == []


def test_08_a_project_without_pods_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(app_builder, "_obsoleted_frameworks",
                        lambda: {"AssetsLibrary": "26.0"})
    assert app_builder.removed_framework_users(str(tmp_path)) == []


# ── the message has to be actionable ────────────────────────────────────────

def test_09_the_warning_says_it_cannot_be_patched(repo, monkeypatch):
    """Two 'fixes' were shipped in this area that could not work. Saying so is
    the honest outcome, not a gap to be filled later."""
    monkeypatch.setattr(app_builder, "_obsoleted_frameworks",
                        lambda: {"AssetsLibrary": "26.0"})
    _pod(repo, "FBSDKCoreKit", framework="AssetsLibrary", swift=True)
    msg = app_builder.removed_framework_users(repo)[0]
    assert "no longer exists" in msg
    assert "Update or remove" in msg


def test_10_it_warns_that_the_error_will_name_apples_header(repo, monkeypatch):
    monkeypatch.setattr(app_builder, "_obsoleted_frameworks",
                        lambda: {"AssetsLibrary": "26.0"})
    _pod(repo, "FBSDKCoreKit", framework="AssetsLibrary", swift=True)
    msg = app_builder.removed_framework_users(repo)[0]
    assert "rather than this pod" in msg


def test_11_the_build_failure_carries_the_warning():
    """Structural: it must reach the dashboard, not only the log."""
    import inspect
    src = inspect.getsource(app_builder.build_ios)
    assert src.count("removed_framework_users") >= 2, \
        "the warning must appear in the failure message, not just the log"
