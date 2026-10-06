"""The platform applies patches/, because on these projects nothing else does.

vya-consumer's preprod-2-May18 ships four patches and declares neither
patch-package nor a postinstall that runs it. A clean install therefore applies
none of them: the Mac where the app builds has a node_modules someone patched by
hand, and a fresh clone silently gets unpatched source. That difference is
invisible until a build fails for an unrelated-looking reason.
"""

import json
import os
import subprocess

import pytest

from automation.projects.preparation import preparation_service


def _git_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    return str(tmp_path)


def _installed(root, name, version, rel, contents):
    d = os.path.join(root, "node_modules", *name.split("/"))
    os.makedirs(os.path.join(d, os.path.dirname(rel)) or d, exist_ok=True)
    with open(os.path.join(d, "package.json"), "w") as f:
        json.dump({"name": name, "version": version}, f)
    with open(os.path.join(d, rel), "w") as f:
        f.write(contents)


def _patch(root, fname, target_rel, before, after):
    d = os.path.join(root, "patches")
    os.makedirs(d, exist_ok=True)
    body = (
        f"--- a/node_modules/{target_rel}\n"
        f"+++ b/node_modules/{target_rel}\n"
        "@@ -1,1 +1,1 @@\n"
        f"-{before}\n"
        f"+{after}\n"
    )
    with open(os.path.join(d, fname), "w") as f:
        f.write(body)


def test_a_patch_is_applied_and_changes_the_source(tmp_path):
    root = _git_repo(tmp_path)
    _installed(root, "left-pad", "1.0.0", "index.js", "old\n")
    _patch(root, "left-pad+1.0.0.patch", "left-pad/index.js", "old", "new")

    messages = preparation_service.apply_patch_package(root)

    src = open(os.path.join(root, "node_modules", "left-pad", "index.js")).read()
    assert src.strip() == "new", "the patch must actually change the source"
    assert any("applied" in m for m in messages)


def test_running_it_three_times_is_idempotent(tmp_path):
    """Preparation reruns constantly; a patch must not be applied twice."""
    root = _git_repo(tmp_path)
    _installed(root, "left-pad", "1.0.0", "index.js", "old\n")
    _patch(root, "left-pad+1.0.0.patch", "left-pad/index.js", "old", "new")

    for _ in range(3):
        preparation_service.apply_patch_package(root)

    src = open(os.path.join(root, "node_modules", "left-pad", "index.js")).read()
    assert src.strip() == "new", "repeated runs must not corrupt the file"


def test_a_patch_for_an_uninstalled_package_is_skipped_not_failed(tmp_path):
    """An inert patch is not a broken one — it must not stop the build."""
    root = _git_repo(tmp_path)
    _patch(root, "react-native-compressor+1.10.3.patch",
           "react-native-compressor/ios/Video/VideoMain.swift", "a", "b")

    messages = preparation_service.apply_patch_package(root)

    assert any("not installed" in m for m in messages)
    assert "0 failed" in messages[-1]


def test_a_version_mismatch_is_reported_before_applying(tmp_path):
    """The filename's version is the only claim it makes; a mismatch is said aloud."""
    root = _git_repo(tmp_path)
    _installed(root, "left-pad", "2.0.0", "index.js", "old\n")
    _patch(root, "left-pad+1.0.0.patch", "left-pad/index.js", "old", "new")

    messages = preparation_service.apply_patch_package(root)

    assert any("targets left-pad@1.0.0 but 2.0.0 is installed" in m for m in messages)


def test_a_patch_that_does_not_apply_is_skipped_not_fatal(tmp_path):
    """One unappliable patch must not fail the whole build."""
    root = _git_repo(tmp_path)
    _installed(root, "left-pad", "1.0.0", "index.js", "something else entirely\n")
    _patch(root, "left-pad+1.0.0.patch", "left-pad/index.js", "old", "new")

    messages = preparation_service.apply_patch_package(root)

    assert any("does NOT apply" in m for m in messages)
    assert "1 failed" in messages[-1], "reported, so it can be judged"


def test_no_patches_directory_is_silent(tmp_path):
    assert preparation_service.apply_patch_package(_git_repo(tmp_path)) == []


def test_patching_never_modifies_a_tracked_file(tmp_path):
    """patch-package patches edit node_modules, which is gitignored.

    If one ever touched tracked source, the platform's change would land in the
    application's diff.
    """
    root = _git_repo(tmp_path)
    with open(os.path.join(root, ".gitignore"), "w") as f:
        f.write("node_modules/\n")
    with open(os.path.join(root, "app.js"), "w") as f:
        f.write("tracked\n")
    _installed(root, "left-pad", "1.0.0", "index.js", "old\n")
    _patch(root, "left-pad+1.0.0.patch", "left-pad/index.js", "old", "new")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)

    preparation_service.apply_patch_package(root)

    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                           capture_output=True, text=True).stdout.strip()
    assert dirty == "", f"patching modified tracked files: {dirty}"
