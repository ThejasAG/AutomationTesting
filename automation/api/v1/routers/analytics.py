from fastapi import APIRouter
from pydantic import BaseModel
from typing import List

router = APIRouter(prefix="/analytics", tags=["analytics"])

class ImpactRequest(BaseModel):
    changed_files: List[str]
    commit_sha: str = "HEAD"

@router.post("/impact")
def analyze_impact(req: ImpactRequest):
    return {
        "recommended_to_run": [],
        "recommended_to_skip": [],
        "reasoning": "Impact analysis is not currently configured. Requires integration with a dependency graph tool."
    }
