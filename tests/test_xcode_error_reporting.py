"""A failed build must report the error, not the warnings above it.

From the dashboard on a second Mac:

    xcodebuild failed. -441d-901e-dccc65e49bf7/ios/Pods/Pods.xcodeproj: warning:
    The iOS Simulator deployment target 'IPHONEOS_DEPLOYMENT_TARGET' is set to
    11.0 ... (in target 'SDWebImageWebPCoder') /Users/Aritro/

Every word of that is a WARNING, cut mid-path, and warnings of that kind appear
in successful builds too. The cause was not the summariser: _run truncated the
log to its last 2500 characters before any caller saw it, and an Xcode build
emits megabytes of deployment-target warnings — so the slice reliably contained
no `error:` line at all, and the summariser dutifully fell back to the tail.

A run-script phase failure (React Native's codegen) has no `error:` line in the
first place; the reason is in the script's own output, far above the summary.
"""
import pytest

from automation.projects.builder import _summarize_xcode_errors

WARN = ("/p/Pods.xcodeproj: warning: The iOS Simulator deployment target "
        "'IPHONEOS_DEPLOYMENT_TARGET' is set to 10.0, but the range of supported "
        "deployment target versions is 12.0 to 26.0.99. (in target 'FirebaseCore' "
        "from project 'Pods')\n")


def _noisy(tail: str, warnings: int = 400) -> str:
    """A realistic log: the failure buried under thousands of warning lines."""
    return WARN * warnings + tail


def test_01_a_compiler_error_survives_a_flood_of_warnings():
    log = _noisy("/p/folly/portability/Time.h:52:17: error: typedef redefinition "
                 "with different types ('uint8_t' vs 'enum clockid_t')\n"
                 "** BUILD FAILED **\n")
    out = _summarize_xcode_errors(log)
    assert "typedef redefinition" in out
    assert "IPHONEOS_DEPLOYMENT_TARGET" not in out, "warnings crowded out the error"


def test_02_warnings_alone_are_never_presented_as_the_failure():
    out = _summarize_xcode_errors(_noisy("** BUILD FAILED **\n"))
    assert "No compiler error found" in out, \
        "a warning was presented as though it were the error"


def test_03_a_failed_script_phase_names_the_phase():
    """React Native codegen: no compiler error exists to find."""
    log = _noisy(
        "env: node: No such file or directory\n"
        "Command PhaseScriptExecution failed with a nonzero exit code\n"
        "** BUILD FAILED **\n"
        "The following build commands failed:\n"
        "PhaseScriptExecution [CP-User]\\ Generate\\ Specs "
        "/p/FBReactNativeSpec.build/Script-23F20C4.sh (in target "
        "'FBReactNativeSpec' from project 'Pods')\n")
    out = _summarize_xcode_errors(log)
    assert "script phase failed" in out.lower()
    assert "FBReactNativeSpec" in out


def test_04_the_scripts_own_output_is_surfaced():
    """The actionable line — why the script failed — must appear."""
    log = _noisy(
        "env: node: No such file or directory\n"
        "** BUILD FAILED **\n"
        "PhaseScriptExecution [CP-User]\\ Generate\\ Specs /p/S.sh (in target 'X')\n")
    out = _summarize_xcode_errors(log)
    assert "env: node: No such file or directory" in out, \
        "the one line that says what to fix was dropped"


@pytest.mark.parametrize("diagnostic", [
    "env: node: No such file or directory",
    "/bin/sh: line 3: yarn: command not found",
    "error: Cannot find module 'react-native-codegen'",
    "Permission denied",
    "ReferenceError: x is not defined",
])
def test_05_common_script_diagnostics_are_recognised(diagnostic):
    log = _noisy(f"{diagnostic}\n** BUILD FAILED **\n"
                 "PhaseScriptExecution [CP-User]\\ Generate\\ Specs /p/S.sh (in target 'X')\n")
    assert diagnostic.split(":")[-1].strip()[:20] in _summarize_xcode_errors(log)


def test_06_deployment_target_noise_never_reaches_the_summary():
    log = _noisy("env: node: No such file or directory\n** BUILD FAILED **\n"
                 "PhaseScriptExecution [CP-User]\\ Generate\\ Specs /p/S.sh (in target 'X')\n")
    out = _summarize_xcode_errors(log)
    assert "deployment target" not in out.lower()


def test_07_multiple_distinct_errors_are_all_listed():
    log = _noisy("/a.m:1:1: error: first problem\n"
                 "/b.m:2:2: error: second problem\n** BUILD FAILED **\n")
    out = _summarize_xcode_errors(log)
    assert "first problem" in out and "second problem" in out


def test_08_a_warning_that_mentions_error_is_not_an_error():
    log = _noisy("/p: warning: this error: string appears inside a warning\n"
                 "/c.m:3:3: error: the real one\n** BUILD FAILED **\n")
    out = _summarize_xcode_errors(log)
    assert "the real one" in out
    assert "inside a warning" not in out


def test_09_libcxx_system_error_include_is_not_the_error():
    # libc++'s header is named `system_error`, so its include-trace line
    # contains "error:". Counted as the error, it hid the failed codegen script
    # (nvm with no default alias) behind a meaningless line.
    log = _noisy(
        "In file included from /sdk/usr/include/c++/v1/system_error:152:\n"
        "N/A: version \"default\" is not yet installed.\n"
        "You need to run `nvm install default` to install and use it.\n"
        "Command PhaseScriptExecution failed with a nonzero exit code\n"
        "The following build commands failed:\n"
        "\tPhaseScriptExecution [CP-User]\\ Generate\\ Specs /p/Script-1.sh\n")
    out = _summarize_xcode_errors(log)
    assert "system_error:152" not in out
    assert "Generate\\ Specs" in out
    assert "is not yet installed" in out
