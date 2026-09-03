"""Source directories must not be swallowed by generated-output ignore rules.

`reports/` matched at ANY depth, so automation/reports/ (SOURCE — generator.py,
step_stats.py) was ignored too. Those were only ever committed with `git add -f`,
and step_stats.py nearly shipped missing, which would have crashed a fresh install
on import with no clue why.
"""
import subprocess
from pathlib import Path


def _ignored(path: str) -> bool:
    return subprocess.run(["git", "check-ignore", "-q", path]).returncode == 0


def test_source_under_automation_reports_is_tracked():
    assert not _ignored("automation/reports/step_stats.py")
    assert not _ignored("automation/reports/generator.py")


def test_a_new_file_there_would_also_be_tracked():
    """The real failure was a NEW file being dropped, not the existing ones."""
    assert not _ignored("automation/reports/some_new_module.py")


def test_generated_report_output_is_still_ignored():
    assert _ignored("reports/run-123.html")


def test_every_python_package_under_automation_is_visible_to_git():
    """A silently-ignored module is the worst kind: it works here and crashes there."""
    for init in Path("automation").rglob("__init__.py"):
        if "node_modules" in init.parts or ".venv" in init.parts:
            continue
        assert not _ignored(str(init)), f"{init.parent} is ignored — it will not ship"
