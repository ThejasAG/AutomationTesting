"""The PR poller skips a commit only if a TestRun carries its sha.

Before _persist_pr_run existed the autotest path wrote no such row, so every
poll cycle re-tested every open PR. This is the check that would fail again.

Run:  .venv/bin/python tests/test_pr_autotest_dedup.py
"""
import os
import tempfile

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/pr_dedup.db"

from automation.database.config import SessionLocal, initialize_database  # noqa: E402
from automation.database.models import TestRun  # noqa: E402
from automation.intelligence.pr_autotest import _persist_pr_run  # noqa: E402

SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
PROJECT = "proj-1234-5678"


def _poller_would_skip(sha: str) -> bool:
    """The exact dedup query pr_poller.poll_once() runs."""
    with SessionLocal() as db:
        return db.query(TestRun).filter(TestRun.commit_sha == sha).first() is not None


def main() -> None:
    initialize_database()
    assert not _poller_would_skip(SHA), "clean db should not dedup"

    run_id = _persist_pr_run(PROJECT, 42, SHA, "feature/x",
                             status="running", job_state="running")
    assert run_id, "persist returned no id"
    assert _poller_would_skip(SHA), "poller would re-test the same commit forever"

    # Finishing the run must UPDATE the same row, not add a second one.
    again = _persist_pr_run(PROJECT, 42, SHA, "feature/x",
                            status="passed", job_state="completed")
    assert again == run_id, f"id not stable: {again} != {run_id}"
    with SessionLocal() as db:
        rows = db.query(TestRun).filter(TestRun.commit_sha == SHA).all()
        assert len(rows) == 1, f"expected 1 row per commit, got {len(rows)}"
        assert rows[0].status == "passed", rows[0].status
        assert rows[0].bot_type == "ios-pr-qa", "reaper filters on this bot_type"

    # A different commit on the same PR is a separate run.
    other = _persist_pr_run(PROJECT, 42, "ff" * 20, "feature/x", status="running")
    assert other != run_id, "a new commit must get its own run"

    print("ok")


if __name__ == "__main__":
    main()
