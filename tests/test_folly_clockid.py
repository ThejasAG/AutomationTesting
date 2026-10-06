"""RCT-Folly's clockid_t typedef conflicts with the iOS 26 SDK.

Reported from a second Mac running the same Xcode 26 as the machine where the
project builds:

    Pods/Headers/Private/RCT-Folly/folly/portability/Time.h:52:17: error:
    typedef redefinition with different types
    ('uint8_t' (aka 'unsigned char') vs 'enum clockid_t')

RCT-Folly 2021.06.28 (React Native 0.68) declares its own clockid_t behind
`#if !FOLLY_HAVE_CLOCK_GETTIME`; the iOS 26 SDK now defines it as an enum.

The project already solves this in its OWN Podfile post_install, which rewrites
Time.h so TARGET_OS_IPHONE forces FOLLY_HAVE_CLOCK_GETTIME on and the typedef
is never reached. That hook is a shelled-out `sed`, and CocoaPods reports
success whether or not it did anything — so the failure is not the SDK, it is
a silently unapplied hook, surfacing thousands of lines later against folly.
"""
import os

import pytest

from automation.projects.builder import app_builder

# RCT-Folly 2021.06.28.00-v2 EXACTLY as it ships, copied from a real Pods tree.
#
# The earlier fixture here was hand-written and, crucially, omitted
# TARGET_OS_IPHONE from the unpatched form. Every test passed while the detector
# was broken in the field: it asked whether TARGET_OS_IPHONE was PRESENT, and
# that token appears in BOTH forms — bare when patched, inside a version gate
# when not. So it declared every real unpatched header already fixed.
#
# Fixtures for this file must come from a real header, not from memory.
UNPATCHED = """#pragma once
#include <time.h>

#if __MACH__ &&                                                       \\
        ((!defined(TARGET_OS_OSX) || TARGET_OS_OSX) &&                \\
         (MAC_OS_X_VERSION_MIN_REQUIRED < MAC_OS_X_VERSION_10_12)) || \\
    (TARGET_OS_IPHONE && (__IPHONE_OS_VERSION_MIN_REQUIRED < __IPHONE_10_0))

#ifdef FOLLY_HAVE_CLOCK_GETTIME
#undef FOLLY_HAVE_CLOCK_GETTIME
#endif

#define FOLLY_HAVE_CLOCK_GETTIME 1
#define FOLLY_FORCE_CLOCK_GETTIME_DEFINITION 1

#endif

// These aren't generic implementations, so we can only declare them on
// platforms we support.
#if !FOLLY_HAVE_CLOCK_GETTIME && (defined(__MACH__) || defined(_WIN32))
#define CLOCK_REALTIME 0
#define CLOCK_MONOTONIC 1

typedef uint8_t clockid_t;
extern "C" int clock_gettime(clockid_t clk_id, struct timespec* ts);
extern "C" int clock_getres(clockid_t clk_id, struct timespec* ts);
#endif
"""

# The header after the Podfile's post_install has run — TARGET_OS_IPHONE forces
# the macro on, so the typedef is dead code. Copied from a working machine.
# The same file after React Native's own react_native_pods.rb has run: the
# version gate is stripped, leaving a bare (TARGET_OS_IPHONE). Copied from a
# machine where this project builds.
PATCHED = UNPATCHED.replace(
    "(TARGET_OS_IPHONE && (__IPHONE_OS_VERSION_MIN_REQUIRED < __IPHONE_10_0))",
    "(TARGET_OS_IPHONE)")


def _header(pod_dir, text):
    d = os.path.join(pod_dir, "Pods", "RCT-Folly", "folly", "portability")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "Time.h")
    open(p, "w").write(text)
    return p


@pytest.fixture
def ios(tmp_path):
    d = tmp_path / "ios"
    d.mkdir()
    return str(d)


def test_01_an_unpatched_header_is_detected(ios):
    _header(ios, UNPATCHED)
    assert app_builder._folly_clockid_conflict(ios) is True


def test_02_a_patched_header_is_not_flagged(ios):
    """The working machine's state must stay silent."""
    _header(ios, PATCHED)
    assert app_builder._folly_clockid_conflict(ios) is False


def test_03_a_project_without_folly_is_not_flagged(ios):
    assert app_builder._folly_clockid_conflict(ios) is False


def test_04_newer_folly_without_the_typedef_is_not_flagged(ios):
    """RN >=0.71 ships folly without its own clockid_t — nothing to guard."""
    _header(ios, "#pragma once\n#include <time.h>\n")
    assert app_builder._folly_clockid_conflict(ios) is False


# ── repair ──────────────────────────────────────────────────────────────────

def test_05_the_repair_makes_the_typedef_unreachable(ios):
    p = _header(ios, UNPATCHED)
    assert app_builder._repair_folly_clockid(ios) is True
    text = open(p).read()
    assert "TARGET_OS_IPHONE" in text, "the iOS guard was not applied"
    assert app_builder._folly_clockid_conflict(ios) is False


def test_06_the_repair_applies_the_podfiles_own_rename(ios):
    """The hook's actual edit: __IPHONE_13_0 -> __IPHONE_14_0."""
    p = _header(ios, UNPATCHED.replace("MAC_OS_X_VERSION_10_12",
                                       "__IPHONE_13_0") + "\n// TARGET_OS_IPHONE\n")
    app_builder._repair_folly_clockid(ios)
    text = open(p).read()
    assert "__IPHONE_14_0" in text and "__IPHONE_13_0" not in text


def test_07_repairing_a_healthy_header_changes_nothing(ios):
    p = _header(ios, PATCHED)
    before = open(p).read()
    assert app_builder._repair_folly_clockid(ios) is False
    assert open(p).read() == before, "a working header was rewritten"


def test_08_a_missing_header_is_not_an_error(ios):
    assert app_builder._repair_folly_clockid(ios) is False


# ── wired into pod install ──────────────────────────────────────────────────

def test_09_pod_install_repairs_and_says_so(ios):
    """CocoaPods exits 0 whether or not its post_install hook worked, so the
    check runs after a SUCCESSFUL install."""
    _header(ios, UNPATCHED)
    out = app_builder._verify_pods(ios, "Pod installation complete!")
    assert "RCT-Folly" in out and "repaired" in out
    assert app_builder._folly_clockid_conflict(ios) is False


def test_10_a_healthy_pod_install_output_is_untouched(ios):
    _header(ios, PATCHED)
    assert app_builder._verify_pods(ios, "Pod installation complete!") == \
        "Pod installation complete!"


def test_11_an_unrepairable_header_warns_instead_of_passing_silently(ios, monkeypatch):
    _header(ios, UNPATCHED)
    monkeypatch.setattr(app_builder, "_repair_folly_clockid", lambda d: False)
    out = app_builder._verify_pods(ios, "ok")
    assert "WARNING" in out and "NetOps.cpp" in out, \
        "an unfixable conflict must be named before the build buries it"


# ── the two reasons the repair did not actually run ─────────────────────────
#
# Shipped in e7af92e and it still failed on the reporting machine. Two separate
# bugs, both invisible to the tests above because those wrote plain files into
# a tmp dir and called the repair directly.

def test_12_a_read_only_header_is_still_repaired(ios):
    """CocoaPods installs pod sources 0444. `open(..., "w")` raised
    PermissionError, the repair caught it, logged a warning and returned False —
    so it never worked on a real Pods tree, only on the ones tests created."""
    import os
    p = _header(ios, UNPATCHED)
    os.chmod(p, 0o444)
    assert app_builder._repair_folly_clockid(ios) is True, \
        "the repair gave up on a read-only file"
    assert app_builder._folly_clockid_conflict(ios) is False


def test_13_the_original_file_mode_is_restored(ios):
    """Leaving pod sources writable would make a later `pod install` see a tree
    that no longer matches what it installed."""
    import os
    p = _header(ios, UNPATCHED)
    os.chmod(p, 0o444)
    app_builder._repair_folly_clockid(ios)
    assert oct(os.stat(p).st_mode & 0o777) == "0o444", "the file was left writable"


def test_14_pods_already_installed_is_still_verified(ios):
    """The second bug: `pod install` is SKIPPED when Pods look current, and that
    early return bypassed the check entirely. A broken header is precisely what
    an 'already installed' tree is carrying, so the rebuild path — the one that
    actually failed — never ran it."""
    _header(ios, UNPATCHED)
    out = app_builder._verify_pods(ios, "Pods already installed.")
    assert "repaired" in out
    assert app_builder._folly_clockid_conflict(ios) is False


def test_15_the_skip_path_is_wired_to_the_check(ios):
    """Structural: the early return must not go back to a bare string."""
    import inspect
    # Both skip paths: the cached one (same inputs) and the on-disk one.
    for fn in (app_builder._pod_install, app_builder._pod_install_uncached):
        src = inspect.getsource(fn)
        i = src.index("Pods already installed")
        line = src[src.rindex("\n", 0, i):i + 40]
        assert "_verify_pods" in line, \
            f"{fn.__name__}: 'already installed' returns without verifying the pod tree"
