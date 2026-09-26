"""Bring a Mac to a buildable state by itself -- no terminal, no hand-typed fixes.

Every item here is a problem met bringing Vya up on a fresh Intel Mac after an
Xcode update. The preflight (macos_environment) only *names* them; this module
*fixes* them, so a new machine needs nothing beyond running the platform.

Each fixer is idempotent and does nothing on a healthy machine, so it is safe
to call before every build. Order matters: Xcode's components, then its iOS
platform, then the tools that build against it.

    xcode first launch   admin rights: one macOS password dialog, no terminal
    iOS platform         xcodebuild -downloadPlatform iOS (no admin needed)
    simulators           an iPhone + iPad on Xcode's iOS (WDA cannot run on older)
    idb                  fb-idb client in ~/.idb-venv (no admin needed)
    CocoaPods            gem --user-install, pinned for system Ruby 2.6
    nvm default          ~/.nvm/alias/default -> system

CLI (one-time setup of a new Mac):  python -m automation.projects.machine_setup
"""
from __future__ import annotations

import glob
import logging
import os
import subprocess
from typing import Callable, List, Optional

from automation.projects import macos_environment as me

logger = logging.getLogger(__name__)

Step = Callable[[str], None]

# macOS's own Ruby is 2.6. Every current CocoaPods dependency has moved past it,
# and `gem install cocoapods` resolves the newest of each and fails. These are the
# last versions that install there, in dependency order. (Homebrew is no answer on
# Intel: it has no Intel bottles, so it compiles LLVM + Rust + Ruby -- hours.)
_RUBY26_PINS = [
    ("zeitwerk", "2.6.18"), ("i18n", "1.14.8"), ("minitest", "5.15.0"),
    ("public_suffix", "4.0.7"), ("drb", "2.0.6"), ("activesupport", "6.1.7.10"),
    ("cocoapods", "1.15.2"),
]

FIRST_LAUNCH_TIMEOUT = 15 * 60
PLATFORM_DOWNLOAD_TIMEOUT = 3 * 60 * 60   # ~10 GB
GEM_TIMEOUT = 15 * 60


def user_gem_bin_dirs() -> List[str]:
    """Where `gem install --user-install` puts executables (e.g. `pod`)."""
    return sorted(glob.glob(os.path.expanduser("~/.gem/ruby/*/bin")))


def _run(cmd: List[str], timeout: int) -> "tuple[bool, str]":
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=me._env())
        return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()
    except FileNotFoundError:
        return False, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return False, f"{' '.join(cmd[:2])}: timed out after {timeout}s"


def _as_admin(shell_cmd: str, prompt: str) -> "tuple[bool, str]":
    """Run *shell_cmd* as root via the standard macOS password dialog.

    The platform runs in the logged-in user's session (start.sh), so the dialog
    appears on that Mac's screen. Cancelling it is reported, never retried."""
    esc = shell_cmd.replace("\\", "\\\\").replace('"', '\\"')
    script = (f'do shell script "{esc}" with prompt "{prompt}" '
              f'with administrator privileges')
    return _run(["osascript", "-e", script], FIRST_LAUNCH_TIMEOUT)


def fix_xcode_first_launch(step: Step) -> bool:
    ok, _ = _run(["xcodebuild", "-checkFirstLaunchStatus"], 60)
    if ok or not me.which("xcodebuild"):
        return True
    step("Xcode's system components are not installed for this Xcode version "
         "(Xcode.app would quit at launch). Asking for an administrator password "
         "on this Mac to install them…")
    ok, out = _as_admin(
        "xcodebuild -license accept && xcodebuild -runFirstLaunch",
        "The automation platform needs to finish setting up Xcode.")
    if ok:
        step("Xcode first-launch setup: done.")
        return True
    step(f"Xcode first-launch setup did not complete ({out[-200:] or 'cancelled'}). "
         "Run once: sudo xcodebuild -license accept && sudo xcodebuild -runFirstLaunch")
    return False


def _has_simulator_sdk() -> bool:
    ok, out = _run(["xcodebuild", "-showsdks"], 120)
    return ok and "iphonesimulator" in out


def fix_ios_platform(step: Step) -> bool:
    if not me.which("xcodebuild") or _has_simulator_sdk():
        return True
    step("Xcode has no iOS platform installed. Downloading it now "
         "(about 10 GB — this can take a while)…")
    ok, out = _run(["xcodebuild", "-downloadPlatform", "iOS"], PLATFORM_DOWNLOAD_TIMEOUT)
    if ok and _has_simulator_sdk():
        step("iOS platform installed.")
        return True
    step(f"iOS platform download failed: {out[-300:]}. "
         "Install it from Xcode › Settings › Components.")
    return False


def _ruby_version() -> Optional[tuple]:
    ok, out = _run(["ruby", "-e", "print RUBY_VERSION"], 30)
    try:
        return tuple(int(x) for x in out.strip().split(".")[:2]) if ok else None
    except ValueError:
        return None


def fix_cocoapods(step: Step) -> bool:
    if me.which("pod"):
        return True
    ruby = _ruby_version()
    if not ruby:
        step("CocoaPods is missing and no Ruby was found to install it with.")
        return False
    step(f"CocoaPods not found — installing it for this user (Ruby "
         f"{'.'.join(map(str, ruby))}, no admin rights needed)…")
    pins = _RUBY26_PINS if ruby < (3, 0) else [("cocoapods", None)]
    for name, version in pins:
        cmd = ["gem", "install", "--user-install", "--no-document", name]
        if version:
            cmd += ["-v", version]
        ok, out = _run(cmd, GEM_TIMEOUT)
        if not ok:
            step(f"Installing {name} {version or ''} failed: {out[-300:]}")
            return False
    # _build_env puts the user gem bin dirs on PATH, so this now resolves.
    if me.which("pod"):
        step(f"CocoaPods installed: {me.which('pod')}")
        return True
    step("CocoaPods installed but `pod` is still not on PATH.")
    return False


def fix_simulators(step: Step) -> bool:
    """Make sure an iPhone and an iPad exist on Xcode's own iOS version -- the
    only ones WebDriverAgent can run on."""
    from automation.projects import simulators
    simulators.create_missing(step)
    return True


def fix_idb(step: Step) -> bool:
    """The idb client, which the flows read screens and tap through.

    Installed into its own venv (~/.idb-venv) with the platform's Python -- no
    admin rights, and isolated from the platform's own packages. idb_companion
    (the device-side half) comes from Homebrew's facebook/fb tap and cannot be
    installed silently, so its absence is reported with the command."""
    import sys
    from automation.scenarios import idb_path
    ok = True
    if not idb_path.find_idb():
        step("idb client not found — installing fb-idb into ~/.idb-venv…")
        venv_ok, out = _run([sys.executable, "-m", "venv", idb_path.IDB_VENV], 300)
        if venv_ok:
            venv_ok, out = _run([os.path.join(idb_path.IDB_VENV, "bin", "pip"),
                                 "install", "--quiet", "fb-idb"], GEM_TIMEOUT)
        if venv_ok and idb_path.find_idb():
            step(f"idb installed: {idb_path.find_idb()}")
        else:
            step(f"Installing fb-idb failed: {out[-300:]}")
            ok = False
    if not me.which("idb_companion"):
        step("idb_companion is not installed — screen reads and taps will not work. "
             "Install once: brew tap facebook/fb && brew install idb-companion")
        ok = False
    return ok


def fix_nvm_default(step: Step) -> bool:
    msg = me.ensure_nvm_default()
    if msg:
        step(msg)
    return True


FIXERS = (fix_xcode_first_launch, fix_ios_platform, fix_simulators, fix_cocoapods,
          fix_idb, fix_nvm_default)


def auto_setup(step: Step = lambda m: logger.info(m)) -> bool:
    """Run every fixer. True when the machine is ready to build iOS.

    Never raises: a fixer that crashes is reported and the rest still run --
    one broken check must not stop a build the others would have allowed."""
    ready = True
    for fixer in FIXERS:
        try:
            ready = fixer(step) and ready
        except Exception as e:                           # pragma: no cover
            logger.warning("%s crashed: %s", fixer.__name__, e)
            step(f"{fixer.__name__} could not run: {e}")
            ready = False
    return ready


if __name__ == "__main__":
    import sys
    ok = auto_setup(lambda m: print(m, flush=True))
    print("Machine ready for iOS builds." if ok else
          "Machine NOT ready — see the messages above.")
    sys.exit(0 if ok else 1)
