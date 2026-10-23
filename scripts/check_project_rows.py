"""Check the LIVE platform DB for project rows that contradict reality.

Not a pytest: conftest.py points tests at an empty throwaway DB, so a test asserting
on real rows can only ever pass vacuously. This inspects the actual database and is
meant to be run by hand or from doctor.sh.

Two real failures it catches:

1. clone_status='cloned' on a project whose checkout is gone. check_updates() filters
   on is_cloned(), so such a row vanishes from "Latest build" with no explanation --
   the repos/ wipe left two rows like this.

2. A staging bundle id whose default_branch is a production branch. check_updates()
   keys off default_branch ON PURPOSE (preferring the checked-out branch built the
   wrong variant), so this builds a staging bundle from production source: it
   installs, launches, and runs the wrong code with no error anywhere.

Exit 0 = clean, 1 = problems found.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from automation.database.config import SessionLocal          # noqa: E402
from automation.database.models import TestProject           # noqa: E402
from automation.projects.repository import repository_manager  # noqa: E402

PROD_BRANCHES = ("main", "master")


def main() -> int:
    with SessionLocal() as db:
        rows = db.query(TestProject).all()

    problems = []

    for p in rows:
        if p.clone_status == "cloned" and not repository_manager.is_cloned(p.id):
            problems.append(
                f"{p.name!r} ({p.id}): clone_status='cloned' but no checkout on disk "
                f"-- check_updates() drops it from the build list"
            )

    for p in rows:
        if not p.app_bundle_id or "staging" not in p.app_bundle_id:
            continue
        if (p.default_branch or "main") in PROD_BRANCHES:
            problems.append(
                f"{p.name!r} ({p.id}): staging bundle {p.app_bundle_id} but "
                f"default_branch={p.default_branch!r} -- would build staging from prod source"
            )

    if problems:
        print("project row problems:")
        for x in problems:
            print("  -", x)
        return 1
    print(f"project rows OK ({len(rows)} checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
