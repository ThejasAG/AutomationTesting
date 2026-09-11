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

# The header as folly ships it — the typedef is reachable.
UNPATCHED = """#pragma once
#if __MACH__ && (MAC_OS_X_VERSION_MIN_REQUIRED < MAC_OS_X_VERSION_10_12)
#define FOLLY_HAVE_CLOCK_GETTIME 1
#endif

#if !FOLLY_HAVE_CLOCK_GETTIME && (defined(__MACH__) || defined(_WIN32))
#define CLOCK_REALTIME 0
typedef uint8_t clockid_t;
extern "C" int clock_gettime(clockid_t clk_id, struct timespec* ts);
#endif
"""

# The header after the Podfile's post_install has run — TARGET_OS_IPHONE forces
# the macro on, so the typedef is dead code. Copied from a working machine.
PATCHED = """#pragma once
#if __MACH__ && ((!defined(TARGET_OS_OSX) || TARGET_OS_OSX) && \\
    (MAC_OS_X_VERSION_MIN_REQUIRED < MAC_OS_X_VERSION_10_12)) || (TARGET_OS_IPHONE)
#define FOLLY_HAVE_CLOCK_GETTIME 1
#endif

#if !FOLLY_HAVE_CLOCK_GETTIME && (defined(__MACH__) || defined(_WIN32))
typedef uint8_t clockid_t;
#endif
"""


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
