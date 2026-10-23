"""Switching to an already-built branch must not recompile or re-pod the app.

Execute on a cloned repo spent minutes after a 1-second checkout:
- pod install ran every time: the committed Podfile.lock differs from what pod
  install writes, each checkout restores it, so the on-disk comparison always
  saw a mismatch;
- xcodebuild ran whenever HEAD moved, even when only JavaScript changed, which a
  Debug app fetches from Metro anyway.
"""

import os
import subprocess

from automation.projects.builder import app_builder


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repo(tmp_path):
    root = str(tmp_path)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    os.makedirs(os.path.join(root, "ios"))
    os.makedirs(os.path.join(root, "App"))
    for rel, text in {"package.json": "{}", "ios/Podfile": "pod 'A'",
                      "ios/Podfile.lock": "PODS: A 1.0\n", "App/a.js": "1"}.items():
        with open(os.path.join(root, rel), "w") as f:
            f.write(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _commit(root, rel, text):
    with open(os.path.join(root, rel), "w") as f:
        f.write(text)
    _git(root, "commit", "-qam", rel)
    return app_builder._git_head(root)


def test_js_only_change_reuses_the_build(tmp_path):
    root = _repo(tmp_path)
    base = app_builder._git_head(root)
    js = _commit(root, "App/a.js", "2")
    assert app_builder._only_js_changed(root, base, js)


def test_native_changes_rebuild(tmp_path):
    root = _repo(tmp_path)
    base = app_builder._git_head(root)
    for rel in ("ios/Podfile", "package.json"):
        head = _commit(root, rel, "changed " + rel)
        assert not app_builder._only_js_changed(root, base, head), rel


def test_unknown_commit_rebuilds(tmp_path):
    root = _repo(tmp_path)
    assert not app_builder._only_js_changed(root, "deadbeef", app_builder._git_head(root))
    assert not app_builder._only_js_changed(root, None, app_builder._git_head(root))


def test_pod_fingerprint_ignores_on_disk_rewrites(tmp_path):
    # pod install rewrites Podfile.lock; the fingerprint must not move with it,
    # or every Execute re-runs pod install.
    root = _repo(tmp_path)
    fp = app_builder._pod_inputs_fingerprint(root)
    with open(os.path.join(root, "ios", "Podfile.lock"), "w") as f:
        f.write("PODS: A 1.1\n")
    assert app_builder._pod_inputs_fingerprint(root) == fp
    _commit(root, "package.json", '{"x":1}')
    assert app_builder._pod_inputs_fingerprint(root) != fp


def test_pod_install_skipped_when_inputs_match(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    pods = os.path.join(root, "ios", "Pods")
    os.makedirs(os.path.join(pods, "Pods.xcodeproj"))
    with open(os.path.join(pods, "Manifest.lock"), "w") as f:
        f.write("PODS: A 1.1\n")
    with open(os.path.join(pods, app_builder._POD_STAMP), "w") as f:
        f.write(app_builder._pod_inputs_fingerprint(root))

    def boom(*a, **k):
        raise AssertionError("pod install must not run")
    monkeypatch.setattr(app_builder, "_pod_install_uncached", boom)
    for name in ("_silence_logbox", "_expose_table_sheet", "_expose_assign_table_sheet"):
        monkeypatch.setattr(app_builder, name, lambda *a, **k: None)
    monkeypatch.setattr(app_builder, "_verify_pods", lambda d, out: out)

    ok, _ = app_builder._pod_install(root)
    assert ok
    # Xcode's manifest check needs the two locks identical.
    assert open(os.path.join(root, "ios", "Podfile.lock")).read() == "PODS: A 1.1\n"


def test_local_package_json_pin_invalidates_pods(tmp_path):
    # fix_rn_compatibility pins package.json locally (uncommitted). Pods made
    # before the pin point at files the pinned version lacks, so the pin must
    # change the fingerprint even though nothing was committed.
    root = _repo(tmp_path)
    fp = app_builder._pod_inputs_fingerprint(root)
    with open(os.path.join(root, "package.json"), "w") as f:
        f.write('{"dependencies":{"react-native-gesture-handler":"2.9.0"}}')
    assert app_builder._pod_inputs_fingerprint(root) != fp
