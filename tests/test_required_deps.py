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
    repo = _repo(tmp_path, {"react-native": "0.68.7"})      # compressor absent
    targets = AppBuilder()._conflicting_packages(repo, "0.68.7")
    assert targets.get("react-native-compressor") == "1.10.3"


def test_a_curated_version_fix_still_wins_over_the_required_default(tmp_path):
    """setdefault, not overwrite: RN_KNOWN_FIXES stays authoritative on version."""
    for pkg in RN_REQUIRED_DEPS["0.68"]:
        assert pkg not in ("react-native-qrcode-svg",), (
            "a package in both tables would make precedence ambiguous")
