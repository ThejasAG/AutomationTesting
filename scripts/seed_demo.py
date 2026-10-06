"""Seed a zero-setup demo project + scenarios.

Runs against Apple's stock Settings app, which is preinstalled on every iOS
simulator — so there is nothing to clone, build or install. The repo dir is
created empty on purpose: no package.json means ensure_metro() short-circuits.

Usage:  .venv/bin/python -m scripts.seed_demo   (add --reset to re-seed)
"""
import os
import sys

from automation.database.config import SessionLocal, initialize_database
from automation.database.models import SavedScenario, TestProject
from automation.projects.repository import repository_manager

DEMO_ID = "demo-0000-0000-0000-000000000001"
BUNDLE = "com.apple.Preferences"

SCENARIOS = [
    # Passes: these labels exist on every stock Settings app.
    ("Demo — Settings smoke (passes)",
     "A green run in under a minute. No repo, no build.",
     ["tap General", "tap About", "verify Name is visible"]),
    # Fails on purpose: gives the new user an AI root-cause report to look at.
    ("Demo — failing run (shows AI root cause)",
     "Deliberately looks for a screen that does not exist, so RCA has something to explain.",
     ["tap General", "tap Nonexistent Menu", "verify Checkout is visible"]),
]


def main(reset: bool = False) -> None:
    initialize_database()
    db = SessionLocal()
    try:
        if reset:
            db.query(SavedScenario).filter(SavedScenario.project_id == DEMO_ID).delete()
            db.query(TestProject).filter(TestProject.id == DEMO_ID).delete()
            db.commit()

        if db.query(TestProject).filter(TestProject.id == DEMO_ID).first():
            print("Demo project already seeded — pass --reset to recreate.")
            return

        db.add(TestProject(
            id=DEMO_ID,
            name="Demo — iOS Settings",
            description="Try the platform with zero setup. Runs on any booted simulator.",
            git_url="local://demo",       # column is NOT NULL; nothing ever clones it
            repo_type="local",
            platform="ios",
            project_type="ios",
            clone_status="ready",
            build_status="built",
            app_bundle_id=BUNDLE,
        ))
        for name, desc, steps in SCENARIOS:
            db.add(SavedScenario(project_id=DEMO_ID, name=name,
                                 description=desc, steps=steps, covers=["Settings"]))
        db.commit()

        # Runs write screenshots into <repo>/reports/scenario.
        os.makedirs(os.path.join(repository_manager.get_repo_path(DEMO_ID), "reports", "scenario"),
                    exist_ok=True)
        print(f"Seeded demo project {DEMO_ID} with {len(SCENARIOS)} scenarios.")
    finally:
        db.close()


if __name__ == "__main__":
    main(reset="--reset" in sys.argv)
