"""RCT-Folly must not be compiled at a standard that dropped std::unary_function.

After the clockid_t repair let the build past folly's Time.h, it failed on:

    boost/container_hash/hash.hpp:131:33: error: no template named
    'unary_function' in namespace 'std'; did you mean '__unary_function'?

The error names boost and boost is not at fault. boost's own target compiles
fine — CocoaPods gives the Pods project gnu++14. React Native 0.68's
RCT-Folly.podspec overrides that for ITS target:

    pod_target_xcconfig = { "CLANG_CXX_LANGUAGE_STANDARD" => "c++17" }

and folly includes boost's headers, so they get compiled at C++17, where
std::unary_function was removed and the iOS 26 SDK's libc++ no longer has it.

Measured on the reporting machine: folly 2021.06 builds clean at gnu++14 — all
22 objects including the json.cpp that was failing, zero errors. So the pin is
aspirational for that vintage, not required. Fixing the standard is the honest
fix; patching boost's header would work around the symptom, and
std::unary_function supplies argument_type/result_type typedefs that downstream
code expects.

Target-level, because the podspec's xcconfig overrides anything set on the
project — exactly as it already overrides the gnu++14 that is there.
"""
import os

import pytest

from automation.projects.builder import app_builder

# Verbatim first lines of the real RCT-Folly.debug.xcconfig.
REAL = ("CLANG_CXX_LANGUAGE_STANDARD = c++17\n"
        "GCC_PREPROCESSOR_DEFINITIONS = $(inherited) COCOAPODS=1 "
        "FOLLY_NO_CONFIG FOLLY_MOBILE=1\n"
        "HEADER_SEARCH_PATHS = $(inherited) \"${PODS_ROOT}/Headers/Private\"\n"
        "USE_HEADERMAP = NO\n")


def _cfgs(ios, standard="c++17"):
    d = os.path.join(ios, "Pods", "Target Support Files", "RCT-Folly")
    os.makedirs(d, exist_ok=True)
    out = []
    for name in ("RCT-Folly.debug.xcconfig", "RCT-Folly.release.xcconfig"):
        p = os.path.join(d, name)
        open(p, "w").write(REAL.replace("c++17", standard))
        out.append(p)
    return out


@pytest.fixture
def ios(tmp_path):
    d = tmp_path / "ios"
    d.mkdir()
    return str(d)


def test_01_the_cxx17_pin_is_detected(ios):
    _cfgs(ios)
    assert len(app_builder._folly_cxx_standard(ios)) == 2


@pytest.mark.parametrize("std", ["c++17", "gnu++17", "c++20", "gnu++20", "c++23"])
def test_02_every_standard_without_unary_function_is_detected(ios, std):
    _cfgs(ios, std)
    assert app_builder._folly_cxx_standard(ios), f"{std} drops std::unary_function"


@pytest.mark.parametrize("std", ["gnu++14", "c++14", "gnu++11", "c++11"])
def test_03_a_standard_that_still_has_it_is_left_alone(ios, std):
    _cfgs(ios, std)
    assert app_builder._folly_cxx_standard(ios) == [], f"{std} is fine"


def test_04_the_repair_pins_gnu14_in_both_configs(ios):
    paths = _cfgs(ios)
    assert app_builder._repair_folly_cxx_standard(ios) == 2
    for p in paths:
        assert open(p).readline().strip() == "CLANG_CXX_LANGUAGE_STANDARD = gnu++14"


def test_05_nothing_else_in_the_xcconfig_is_disturbed(ios):
    """Only the one line changes: the rest configures folly's own compile."""
    paths = _cfgs(ios)
    app_builder._repair_folly_cxx_standard(ios)
    text = open(paths[0]).read()
    assert "FOLLY_NO_CONFIG FOLLY_MOBILE=1" in text
    assert "USE_HEADERMAP = NO" in text
    assert text.count("CLANG_CXX_LANGUAGE_STANDARD") == 1


def test_06_the_repair_is_idempotent(ios):
    _cfgs(ios)
    assert app_builder._repair_folly_cxx_standard(ios) == 2
    assert app_builder._repair_folly_cxx_standard(ios) == 0, "rewrote a fixed file"
    assert app_builder._folly_cxx_standard(ios) == []


def test_07_a_read_only_xcconfig_is_still_repaired(ios):
    """CocoaPods writes these read-only, as it does the pod sources."""
    paths = _cfgs(ios)
    for p in paths:
        os.chmod(p, 0o444)
    assert app_builder._repair_folly_cxx_standard(ios) == 2
    assert oct(os.stat(paths[0]).st_mode & 0o777) == "0o444", "left writable"


def test_08_a_project_without_folly_is_a_no_op(ios):
    assert app_builder._folly_cxx_standard(ios) == []
    assert app_builder._repair_folly_cxx_standard(ios) == 0


def test_09_pod_install_applies_it_and_explains_why(ios):
    _cfgs(ios)
    out = app_builder._verify_pods(ios, "Pod installation complete!")
    assert "gnu++14" in out and "unary_function" in out
    assert app_builder._folly_cxx_standard(ios) == []


def test_10_a_healthy_tree_produces_no_message(ios):
    _cfgs(ios, "gnu++14")
    assert app_builder._verify_pods(ios, "ok") == "ok"
