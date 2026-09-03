"""Projects and scenarios must travel to a new machine.

The database lives OUTSIDE the repo on purpose (.db files are in git history and a
checkout overwrote a live one, losing everything). The cost is that a fresh clone
starts empty — so the data ships as a committed JSON seed instead.
"""
import json
import subprocess
import sys
from pathlib import Path

SEED = Path("seeds/platform.json")
SCRIPT = Path("scripts/platform_seed.py")


def _run(cmd, db):
    return subprocess.run(
        [sys.executable, str(SCRIPT), cmd],
        capture_output=True, text=True, timeout=120,
        env={**dict(__import__("os").environ), "DATABASE_URL": f"sqlite:////{db}"})


def test_the_seed_is_committed_and_populated():
    """If this is empty, a new machine gets a working platform with no tests in it."""
    d = json.loads(SEED.read_text())
    assert d["projects"] and d["scenarios"]


def test_every_scenario_keeps_its_steps():
    for s in json.loads(SEED.read_text())["scenarios"]:
        steps = json.loads(s["steps"]) if isinstance(s["steps"], str) else s["steps"]
        assert steps, f"{s['name']} exported with no steps"


def test_scenarios_reference_a_project_by_a_STABLE_key():
    """Ids are generated per machine; exporting them would orphan or duplicate."""
    d = json.loads(SEED.read_text())
    keys = {f"{(p['git_url'] or '').strip().lower()}@{(p['default_branch'] or '').strip()}"
            f"|{(p['name'] or '').strip()}" for p in d["projects"]}
    linked = [s for s in d["scenarios"] if s.get("project_key")]
    assert linked, "no scenario is linked to a project"
    for s in linked:
        assert s["project_key"] in keys, f"{s['name']} points at an unknown project"


def test_import_populates_an_empty_database(tmp_path):
    db = tmp_path / "fresh.db"
    out = _run("import", db)
    assert out.returncode == 0, out.stderr[-400:]
    assert "imported 10" in out.stdout or "imported " in out.stdout
    assert db.exists()


def test_import_twice_adds_nothing(tmp_path):
    """It runs on a machine that may already be half set up."""
    db = tmp_path / "fresh.db"
    _run("import", db)
    second = _run("import", db)
    assert "imported 0 project(s) and 0 scenario(s)" in second.stdout


def test_run_history_is_not_exported():
    """Runs belong to the machine that produced them — copying them would invent
    a test history the new machine never had."""
    d = json.loads(SEED.read_text())
    assert set(d) <= {"exported_at", "projects", "scenarios"}
