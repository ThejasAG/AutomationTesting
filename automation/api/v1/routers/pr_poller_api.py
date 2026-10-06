"""Status + manual trigger for the PR auto-test poller."""

from fastapi import APIRouter, Depends

from automation.auth.security import get_current_user
from automation.ci_cd import pr_poller

router = APIRouter(prefix="/pr-poller", tags=["PR Poller"])


@router.get("")
def status(current_user=Depends(get_current_user)):
    """Current poller state: enabled, interval, last poll, and recent auto-runs."""
    return pr_poller.state


@router.post("/poll")
def poll_now(current_user=Depends(get_current_user)):
    """Run one poll cycle immediately and return the run_ids queued."""
    queued = pr_poller.poll_once()
    return {"queued": queued, "count": len(queued)}
