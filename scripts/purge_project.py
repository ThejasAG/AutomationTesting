"""Remove a project and every trace of it, keeping its test history.

Deleting a project through the dashboard removes one row and one directory. This
removes the rest: the clone, the visual-regression baselines, project settings
(which hold jira/github tokens), saved scenarios, tickets, AI recommendations and
the learned-locator entry. Test runs are KEPT and detached, so the history of what
was tested survives the project being gone.

Usage
    # what is on disk with no project behind it
    .venv/bin/python -m scripts.purge_project --list-orphans

    # what a purge would touch, changing nothing
    .venv/bin/python -m scripts.purge_project <project_id> --dry-run

    # do it (asks first; --yes skips the prompt)
    .venv/bin/python -m scripts.purge_project <project_id>

    # every orphaned clone in one go
    .venv/bin/python -m scripts.purge_project --purge-orphans --dry-run
"""
import argparse
import sys

from automation.database.config import SessionLocal
from automation.projects.purge import find_orphans, plan_purge, purge_project


def _confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="purge_project",
        description="Remove a project and its traces. Test history is kept.")
    ap.add_argument("project_id", nargs="?", help="the project UUID to purge")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be removed, change nothing")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="do not ask for confirmation")
    ap.add_argument("--list-orphans", action="store_true",
                    help="list clones and baselines with no project row")
    ap.add_argument("--purge-orphans", action="store_true",
                    help="purge every orphaned clone/baseline directory")
    args = ap.parse_args(argv)

    if not (args.project_id or args.list_orphans or args.purge_orphans):
        ap.error("give a project_id, or --list-orphans / --purge-orphans")

    db = SessionLocal()
    try:
        if args.list_orphans or args.purge_orphans:
            orphans = find_orphans(db)
            targets = sorted(set(orphans["clones"]) | set(orphans["baselines"]))
            if not targets:
                print("No orphaned clones or baselines.")
                return 0
            print(f"{len(targets)} orphaned director{'y' if len(targets) == 1 else 'ies'} "
                  f"(no project row):")
            for t in targets:
                where = []
                if t in orphans["clones"]:
                    where.append("repos")
                if t in orphans["baselines"]:
                    where.append("baselines")
                print(f"  {t}  ({', '.join(where)})")

            if args.list_orphans:
                print("\nRe-run with --purge-orphans to remove them.")
                return 0

            print()
            for t in targets:
                print(plan_purge(db, t).render())
                print()
            if args.dry_run:
                print("dry run — nothing was removed")
                return 0
            if not args.yes and not _confirm(f"Purge all {len(targets)} directories?"):
                print("aborted")
                return 1
            for t in targets:
                purge_project(db, t)
            print(f"purged {len(targets)} orphaned director"
                  f"{'y' if len(targets) == 1 else 'ies'}")
            return 0

        plan = plan_purge(db, args.project_id)
        print(plan.render())
        if plan.is_empty:
            return 0
        if args.dry_run:
            print("\ndry run — nothing was removed")
            return 0
        print()
        if not args.yes and not _confirm("Purge this project? This cannot be undone."):
            print("aborted")
            return 1
        done = purge_project(db, args.project_id)
        freed = f", freed {done.repo_bytes / 1024 ** 3:.1f}G" if done.repo_bytes else ""
        print(f"purged {args.project_id}{freed}")
        if done.runs_detached:
            print(f"kept {done.runs_detached} test run(s) — detached from the project")
        for w in done.warnings:
            print(f"  ! {w}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
