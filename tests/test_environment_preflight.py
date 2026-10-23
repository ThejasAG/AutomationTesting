"""Machine problems met on a fresh Intel Mac after an Xcode update.

- Xcode.app aborted at launch: its first-launch components were not installed.
- xcodebuild: "iOS 26.5 is not installed" -- the platform is a separate download.
- Every RN script phase failed: ~/.nvm existed with no default alias.
"""
import os

from automation.projects import macos_environment as me


def _names(checks):
    return {c.name: c for c in checks}


def test_pending_first_launch_fails_with_the_command():
    c = _names(me._check_toolchain({"Xcode": "26.5", "iOS SDK": "26.5",
                                    "Xcode First Launch": "pending"}))["Xcode first launch"]
    assert c.status == me.FAIL and "runFirstLaunch" in c.fix


def test_missing_ios_platform_fails_with_the_download():
    c = _names(me._check_toolchain({"Xcode": "26.5", "iOS SDK": None}))["iOS platform"]
    assert c.status == me.FAIL and "downloadPlatform" in c.fix


def test_healthy_xcode_adds_neither():
    names = _names(me._check_toolchain({"Xcode": "26.5", "iOS SDK": "26.5",
                                        "Xcode First Launch": "done"}))
    assert "Xcode first launch" not in names and "iOS platform" not in names


def test_nvm_without_default_is_given_one(tmp_path, monkeypatch):
    nvm = tmp_path / ".nvm"
    nvm.mkdir()
    (nvm / "nvm.sh").write_text("# nvm")
    monkeypatch.setattr(me, "which", lambda t: "/usr/local/bin/node")
    assert me._nvm_needs_default(str(tmp_path))
    assert me.ensure_nvm_default(str(tmp_path))
    assert (nvm / "alias" / "default").read_text().strip() == "system"
    assert me.ensure_nvm_default(str(tmp_path)) is None, "must be idempotent"


def test_existing_nvm_default_is_never_touched(tmp_path, monkeypatch):
    alias = tmp_path / ".nvm" / "alias"
    alias.mkdir(parents=True)
    (tmp_path / ".nvm" / "nvm.sh").write_text("# nvm")
    (alias / "default").write_text("18\n")
    monkeypatch.setattr(me, "which", lambda t: "/usr/local/bin/node")
    assert me.ensure_nvm_default(str(tmp_path)) is None
    assert (alias / "default").read_text() == "18\n"


def test_no_nvm_means_nothing_to_do(tmp_path):
    assert not me._nvm_needs_default(str(tmp_path))
    assert me.ensure_nvm_default(str(tmp_path)) is None
