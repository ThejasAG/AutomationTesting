"""A new Mac must become buildable without anyone typing commands.

Each case is a machine problem met bringing Vya up after an Xcode update; the
platform now fixes it itself (machine_setup) instead of printing a command.
"""
import os

import pytest

from automation.projects import machine_setup as ms


@pytest.fixture(autouse=True)
def _isolated_decline_mark(tmp_path, monkeypatch):
    """Never write the real ~/.vya-platform marker from a test."""
    monkeypatch.setattr(ms, "_FIRST_LAUNCH_MARK", str(tmp_path / "declined-mark"))


class Fake:
    """Scripted _run: maps a command prefix to (ok, out), records calls."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, cmd, timeout):
        self.calls.append(cmd)
        for prefix, result in self.answers:
            if cmd[:len(prefix)] == prefix:
                return result(cmd) if callable(result) else result
        return True, ""


def _setup(monkeypatch, answers, tools=("xcodebuild", "pod", "ruby", "idb_companion")):
    fake = Fake(answers)
    monkeypatch.setattr(ms, "_run", fake)
    from automation.scenarios import idb_path
    monkeypatch.setattr(idb_path, "find_idb", lambda: "/x/idb")
    monkeypatch.setattr(ms.me, "which", lambda t: f"/x/{t}" if t in tools else None)
    return fake


def test_healthy_machine_changes_nothing(monkeypatch):
    fake = _setup(monkeypatch, [(["xcodebuild", "-showsdks"], (True, "iphonesimulator26.5"))])
    monkeypatch.setattr(ms.me, "ensure_nvm_default", lambda: None)
    assert ms.auto_setup(lambda m: None)
    run = [c[:2] for c in fake.calls]
    assert ["osascript", "-e"] not in run
    assert ["xcodebuild", "-downloadPlatform"] not in run
    assert not any(c[0] == "gem" for c in fake.calls)


def test_pending_first_launch_asks_for_admin_via_dialog(monkeypatch):
    fake = _setup(monkeypatch, [(["xcodebuild", "-checkFirstLaunchStatus"], (False, ""))])
    assert ms.fix_xcode_first_launch(lambda m: None)
    osa = next(c for c in fake.calls if c[0] == "osascript")
    assert "runFirstLaunch" in osa[2] and "license accept" in osa[2]
    assert "administrator privileges" in osa[2]


def test_cancelled_password_dialog_is_reported_not_fatal(monkeypatch):
    _setup(monkeypatch, [(["xcodebuild", "-checkFirstLaunchStatus"], (False, "")),
                         (["osascript"], (False, "User canceled."))])
    msgs = []
    assert ms.fix_xcode_first_launch(msgs.append) is False
    assert any("runFirstLaunch" in m for m in msgs), "must say how to do it by hand"


def test_missing_ios_platform_is_downloaded(monkeypatch):
    state = {"sdk": False}

    def download(cmd):
        state["sdk"] = True
        return True, ""
    fake = _setup(monkeypatch, [
        (["xcodebuild", "-showsdks"], lambda c: (True, "iphonesimulator26.5" if state["sdk"] else "macosx26")),
        (["xcodebuild", "-downloadPlatform", "iOS"], download)])
    assert ms.fix_ios_platform(lambda m: None)
    assert ["xcodebuild", "-downloadPlatform", "iOS"] in fake.calls


def test_cocoapods_on_ruby_26_installs_the_pinned_chain(monkeypatch):
    installed = set()

    def gem(cmd):
        installed.add(cmd[4])
        return True, ""
    fake = _setup(monkeypatch, [(["ruby"], (True, "2.6.10")), (["gem"], gem)],
                  tools=("ruby",))
    # `pod` appears once cocoapods is installed.
    monkeypatch.setattr(ms.me, "which",
                        lambda t: "/x/pod" if t == "pod" and "cocoapods" in installed
                        else ("/x/ruby" if t == "ruby" else None))
    assert ms.fix_cocoapods(lambda m: None)
    gems = [c for c in fake.calls if c[0] == "gem"]
    assert all("--user-install" in c for c in gems), "no admin rights needed"
    names = [c[4] for c in gems]
    assert names.index("zeitwerk") < names.index("activesupport") < names.index("cocoapods")
    assert ["-v", "1.15.2"] == gems[-1][-2:]


def test_cocoapods_on_modern_ruby_installs_latest(monkeypatch):
    installed = set()

    def gem(cmd):
        installed.add(cmd[4])
        return True, ""
    fake = _setup(monkeypatch, [(["ruby"], (True, "3.3.0")), (["gem"], gem)], tools=("ruby",))
    monkeypatch.setattr(ms.me, "which",
                        lambda t: "/x/pod" if t == "pod" and installed else None)
    assert ms.fix_cocoapods(lambda m: None)
    assert [c for c in fake.calls if c[0] == "gem"] == [
        ["gem", "install", "--user-install", "--no-document", "cocoapods"]]


def test_build_env_puts_user_gem_bin_on_path(tmp_path, monkeypatch):
    gem_bin = tmp_path / ".gem" / "ruby" / "2.6.0" / "bin"
    gem_bin.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    from automation.projects.builder import _build_env
    assert str(gem_bin) in _build_env()["PATH"].split(os.pathsep)


def test_one_crashing_fixer_does_not_stop_the_others(monkeypatch):
    ran = []

    def boom(step):
        raise RuntimeError("x")
    monkeypatch.setattr(ms, "FIXERS", (boom, lambda s: ran.append(1) or True))
    assert ms.auto_setup(lambda m: None) is False
    assert ran == [1]


def test_missing_idb_client_is_installed_into_its_own_venv(monkeypatch):
    from automation.scenarios import idb_path
    state = {"idb": None}

    def pip(cmd):
        state["idb"] = "/home/.idb-venv/bin/idb"
        return True, ""
    _setup(monkeypatch, [])
    monkeypatch.setattr(idb_path, "find_idb", lambda: state["idb"])
    monkeypatch.setattr(ms, "_run", lambda cmd, timeout: pip(cmd) if "pip" in cmd[0] else (True, ""))
    assert ms.fix_idb(lambda m: None)
    assert state["idb"]


def test_missing_idb_companion_is_reported_with_the_command(monkeypatch):
    _setup(monkeypatch, [], tools=("xcodebuild",))
    msgs = []
    assert ms.fix_idb(msgs.append) is False
    assert any("idb-companion" in m for m in msgs)


def test_idb_found_outside_path(tmp_path, monkeypatch):
    # The daemon's PATH lacks ~/.local/bin, where pipx/--user put idb.
    from automation.scenarios import idb_path
    local = tmp_path / ".local" / "bin"
    local.mkdir(parents=True)
    (local / "idb").write_text("#!/bin/sh\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("IDB_BINARY", raising=False)
    monkeypatch.setattr(idb_path, "IDB_VENV", str(tmp_path / ".idb-venv"))
    assert idb_path.find_idb() == str(local / "idb")


def test_declined_admin_prompt_is_not_repeated(monkeypatch, tmp_path):
    mark = tmp_path / "declined"
    monkeypatch.setattr(ms, "_FIRST_LAUNCH_MARK", str(mark))
    fake = _setup(monkeypatch, [(["xcodebuild", "-checkFirstLaunchStatus"], (False, "")),
                                (["xcode-select"], (True, "/Applications/Xcode.app/Contents/Developer")),
                                (["osascript"], (False, "User canceled."))])
    assert ms.fix_xcode_first_launch(lambda m: None) is False
    assert mark.exists()
    n = sum(1 for c in fake.calls if c[0] == "osascript")
    assert ms.fix_xcode_first_launch(lambda m: None) is False
    assert sum(1 for c in fake.calls if c[0] == "osascript") == n, \
        "unattended Prepares must not keep opening the dialog"


def test_command_line_tools_is_reported_not_prompted(monkeypatch):
    fake = _setup(monkeypatch, [(["xcodebuild", "-checkFirstLaunchStatus"], (False, "")),
                                (["xcode-select"], (True, "/Library/Developer/CommandLineTools"))])
    msgs = []
    assert ms.fix_xcode_first_launch(msgs.append) is False
    assert not any(c[0] == "osascript" for c in fake.calls)
    assert any("xcode-select -s" in m for m in msgs)
