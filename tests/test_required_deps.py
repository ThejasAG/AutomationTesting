"""An imported-but-undeclared package must still be installed.

RN_KNOWN_FIXES only corrects versions of dependencies package.json already lists.
react-native-compressor is imported by App/Utils/videoUploadTracker.js and declared
nowhere — the case that silently truncates the Metro bundle and blanks the app.
"""
import json
from automation.projects.builder import RN_REQUIRED_DEPS, AppBuilder


def _repo(tmp_path, deps):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": deps}))
    nm = tmp_path / "node_modules" / "react-native"
    nm.mkdir(parents=True)
    (nm / "package.json").write_text(json.dumps({"version": "0.68.7"}))
    return str(tmp_path)


def test_an_undeclared_import_is_still_targeted(tmp_path):
    """No recognised source tree — the injection stays, as it always did.

    Absence cannot be proven here, and a missed import breaks the bundle with a
    symptom that points nowhere near the missing package.
    """
    repo = _repo(tmp_path, {"react-native": "0.68.7"})      # compressor absent
    targets = AppBuilder()._conflicting_packages(repo, "0.68.7")
    assert targets.get("react-native-compressor") == RN_REQUIRED_DEPS["0.68"][
        "react-native-compressor"]


def test_a_branch_that_does_not_import_it_gets_nothing_injected(tmp_path):
    """vya-consumer's main: videoUploadTracker.js is a Set with no requires.

    Injecting react-native-compressor there installed an unused native
    dependency whose `import AssetsLibrary` cannot build against the iOS 26 SDK,
    so a branch with no problem stopped building at all.
    """
    repo = _repo(tmp_path, {"react-native": "0.68.7"})
    src = tmp_path / "App" / "Utils"
    src.mkdir(parents=True)
    (src / "videoUploadTracker.js").write_text(
        "export const pendingVideoUploads = new Set();\n")

    targets = AppBuilder()._conflicting_packages(repo, "0.68.7")

    assert "react-native-compressor" not in targets


def test_a_branch_that_does_import_it_still_gets_it(tmp_path):
    """Thai-filter's lazy require — the case the table exists for."""
    repo = _repo(tmp_path, {"react-native": "0.68.7"})
    src = tmp_path / "App" / "Utils"
    src.mkdir(parents=True)
    (src / "videoUploadTracker.js").write_text(
        "const Video = require('react-native-compressor')?.Video;\n")

    targets = AppBuilder()._conflicting_packages(repo, "0.68.7")

    assert targets.get("react-native-compressor") == RN_REQUIRED_DEPS["0.68"][
        "react-native-compressor"]


def test_a_curated_version_fix_still_wins_over_the_required_default(tmp_path):
    """setdefault, not overwrite: RN_KNOWN_FIXES stays authoritative on version."""
    for pkg in RN_REQUIRED_DEPS["0.68"]:
        assert pkg not in ("react-native-qrcode-svg",), (
            "a package in both tables would make precedence ambiguous")
