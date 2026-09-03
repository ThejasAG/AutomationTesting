"""The doctor must check the things that failed silently, not just the obvious ones.

Each assertion below is a fault that cost hours on 2026-09-02 because nothing
checked for it and the symptom appeared much later, somewhere else.
"""
from pathlib import Path

DOC = Path("scripts/doctor.sh").read_text()
MD = Path("docs/MACHINE_SETUP.md").read_text()


def test_it_checks_the_ios_platform_is_installed():
    """Deleting a simulator runtime to save disk broke WebDriverAgent for a whole
    morning; every run failed with 'xcodebuild code 70'."""
    assert "downloadPlatform iOS" in DOC and "iphonesimulator" in DOC


def test_it_checks_the_database_lives_outside_the_repo():
    """.db files are in this repo's git history — a rebase overwrote the live
    database with a committed one and lost every project and scenario."""
    assert "DATABASE_URL=sqlite:////" in DOC


def test_it_checks_the_llm_key_because_a_missing_one_fails_silently():
    """Unset, the platform serves a canned fake analysis and never errors."""
    assert "LLM_PROVIDER_TYPE" in DOC and "OPENAI_API_KEY" in DOC
    assert "silently" in DOC.lower()


def test_it_checks_the_PINNED_flow_devices_not_just_any_booted_one():
    assert "cross_app_config.json" in DOC and "PINNED" in DOC


def test_it_warns_about_memory_and_disk():
    """Both starve steps into 120s timeouts that look like flaky tests."""
    assert "PhysMem" in DOC and "df -g" in DOC


def test_it_tells_you_yarn_not_npm():
    """npm install in these Yarn workspaces corrupts the tree."""
    assert "npm corrupts them" in DOC


def test_it_exits_nonzero_when_something_is_missing():
    assert "exit $FAIL" in DOC


def test_the_setup_doc_starts_from_zero_and_points_at_the_doctor():
    assert "## 0. From zero on a new Mac" in MD
    assert "scripts/doctor.sh" in MD
    for step in ("python3 -m venv", "auth/seed", "react-native start --port", "simctl boot"):
        assert step in MD, f"setup doc never tells you to: {step}"
