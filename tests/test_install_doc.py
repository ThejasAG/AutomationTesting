"""INSTALL.md must cover the steps whose absence breaks an install silently.

Each assertion is a real failure from 2026-09-02 that produced a confusing symptom
somewhere else entirely.
"""
from pathlib import Path

DOC = Path("INSTALL.md").read_text()


def test_every_stage_ends_in_something_you_can_check():
    """Otherwise you find out three steps later that step one failed."""
    assert DOC.count("**Check:**") >= 5


def test_it_warns_against_deleting_ios_runtimes():
    """Doing that broke WebDriverAgent for a morning: 'xcodebuild code 70'."""
    assert "code 70" in DOC and "Do not delete iOS simulator runtimes" in DOC


def test_it_says_the_database_must_live_outside_the_repo():
    assert "OUTSIDE the repo" in DOC and "DATABASE_URL=sqlite:////" in DOC


def test_it_flags_that_a_missing_llm_key_fails_silently():
    """Unset, the platform serves fabricated analysis and never errors."""
    assert "canned fake analysis" in DOC and "never errors" in DOC


def test_it_says_yarn_not_npm_for_app_repos():
    assert "NOT npm" in DOC


def test_it_gives_the_utf8_prefix_for_pod_install():
    assert "LANG=en_US.UTF-8" in DOC and "ASCII-8BIT" in DOC


def test_it_teaches_the_bundle_check_that_catches_an_empty_bundle():
    """A 200 with no registerComponent = blank screen, AppRegistry error."""
    assert "grep -c registerComponent" in DOC
    assert "no app" in DOC or "contains no app" in DOC


def test_it_covers_importing_scenarios_without_touching_projects():
    assert "--scenarios-only" in DOC


def test_it_points_at_the_doctor_for_failures():
    assert DOC.count("scripts/doctor.sh") >= 3
