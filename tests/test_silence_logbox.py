"""The platform silences the app's LogBox so its toasts stop covering real controls.

Measured 2026-09-02 (Vya Business): toasts stack along the bottom and are drawn over
'addNewEvent' (y=723), the lower half of 'saveBtn' (685..735) on the iPad, and the
whole time-slot row on the phone (slot y=730, toast 726..774). Tapping any of them
opened the LogBox viewer, whose own Dismiss button was itself covered.
"""
from automation.projects.builder import AppBuilder

ENTRY = """import {AppRegistry, Platform} from 'react-native';
import App from './App/Containers/App';

AppRegistry.registerComponent('vyaconsumer', () => App);
"""


def _write(tmp_path, text=ENTRY):
    (tmp_path / "index.js").write_text(text)
    return str(tmp_path)


def test_it_adds_the_call_and_the_import(tmp_path):
    repo = _write(tmp_path)
    assert AppBuilder()._silence_logbox(repo)
    out = (tmp_path / "index.js").read_text()
    assert "LogBox.ignoreAllLogs(true);" in out
    assert "import {AppRegistry, Platform, LogBox} from 'react-native';" in out


def test_existing_app_code_is_untouched(tmp_path):
    """It may only ADD a line — never edit or drop what the app already does."""
    repo = _write(tmp_path)
    AppBuilder()._silence_logbox(repo)
    out = (tmp_path / "index.js").read_text()
    assert "AppRegistry.registerComponent('vyaconsumer', () => App);" in out
    assert "import App from './App/Containers/App';" in out


def test_running_twice_changes_nothing(tmp_path):
    """It runs before every build; a second pass must be a no-op."""
    repo = _write(tmp_path)
    AppBuilder()._silence_logbox(repo)
    once = (tmp_path / "index.js").read_text()
    assert AppBuilder()._silence_logbox(repo) is None
    assert (tmp_path / "index.js").read_text() == once
    assert once.count("ignoreAllLogs") == 1


def test_an_app_that_already_silences_it_is_left_alone(tmp_path):
    text = ENTRY.replace("import App", "LogBox.ignoreAllLogs();\nimport App")
    repo = _write(tmp_path, text)
    assert AppBuilder()._silence_logbox(repo) is None
    assert (tmp_path / "index.js").read_text() == text


def test_an_entry_we_do_not_understand_is_not_rewritten(tmp_path):
    """No react-native import to extend — better to do nothing than mangle it."""
    repo = _write(tmp_path, "console.log('not a normal RN entry');\n")
    assert AppBuilder()._silence_logbox(repo) is None


def test_a_missing_entry_file_is_not_an_error(tmp_path):
    assert AppBuilder()._silence_logbox(str(tmp_path)) is None


def test_it_runs_as_part_of_preparing_a_build():
    from pathlib import Path
    src = Path("automation/projects/builder.py").read_text()
    assert "self._silence_logbox(repo_path)" in src
