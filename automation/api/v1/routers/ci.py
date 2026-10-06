"""CI trigger endpoint.

Lets a CI job (GitHub Actions on a self-hosted Mac runner) kick off the full
autonomous QA for a PR: smart-select the affected scenarios from the dependency
graph, build the PR branch, run them, and comment the verdict on the PR.

Guarded by an optional shared secret (CI_SECRET) rather than a user JWT, so a CI
runner can call it without logging in.
"""

from __future__ import annotations

import os
import threading

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from automation.database.config import SessionLocal
from automation.database.models import TestProject

router = APIRouter(prefix="/ci", tags=["CI"])


class CITrigger(BaseModel):
    repo: str        # "owner/name"
    pr_number: int


@router.post("/pr-autotest")
def ci_pr_autotest(body: CITrigger, x_ci_secret: str = Header(default="")):
    """Trigger smart PR QA from CI. Finds the project by repo, runs in background."""
    secret = os.getenv("CI_SECRET", "")
    if secret and x_ci_secret != secret:
        raise HTTPException(status_code=401, detail="Bad or missing X-CI-Secret.")

    owner_repo = body.repo.strip().lower().rstrip("/")
    with SessionLocal() as db:
        project = None
        for p in db.query(TestProject).filter(TestProject.git_url.isnot(None)).all():
            if owner_repo in (p.git_url or "").lower():
                project = p
                break
        if not project:
            raise HTTPException(status_code=404, detail=f"No project matches repo '{body.repo}'.")
        project_id = project.id

    from automation.intelligence.pr_autotest import run_pr_autotest
    threading.Thread(target=run_pr_autotest, args=(project_id, body.pr_number),
                     daemon=True, name=f"ci-autotest-{body.pr_number}").start()
    return {"started": True, "project_id": project_id, "pr_number": body.pr_number,
            "message": "Smart QA started — the platform will comment the verdict on the PR."}
