"""Application Groups — a family of related apps (e.g. Food Delivery →
Business App / Consumer App / Admin Portal).

Each member project stays fully independent (its own repo, branch, platform and
runs). The group only records that they belong together, which is what future
cross-app impact analysis will fan out over: given a change in one app, run the
affected suites of every app in the same group.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Any, Dict, List, Optional
from pydantic import BaseModel
import uuid

from automation.database.config import get_db
from automation.database.models import ApplicationGroup, TestProject

router = APIRouter(prefix="/groups", tags=["Application Groups"])


class GroupCreate(BaseModel):
    name: str
    description: str = ""


class GroupUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class GroupMembers(BaseModel):
    """Project ids to assign to this group (replaces the current membership)."""
    project_ids: List[str]


def _get_group_or_404(group_id: str, db: Session) -> ApplicationGroup:
    group = db.query(ApplicationGroup).filter(ApplicationGroup.id == group_id).first()
    if not group:
        raise HTTPException(status_code=404, detail="Application group not found")
    return group


def _serialize(g: ApplicationGroup) -> Dict[str, Any]:
    return {
        "id": g.id,
        "name": g.name,
        "description": g.description,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "project_count": len(g.projects),
        "projects": [
            {
                "id": p.id,
                "name": p.name,
                "platform": p.platform,
                "project_type": p.project_type,
                "clone_status": p.clone_status,
            }
            for p in g.projects
        ],
    }


@router.get("/")
def list_groups(db: Session = Depends(get_db)):
    groups = db.query(ApplicationGroup).order_by(ApplicationGroup.name.asc()).all()
    return {"groups": [_serialize(g) for g in groups]}


@router.get("/{group_id}")
def get_group(group_id: str, db: Session = Depends(get_db)):
    return {"group": _serialize(_get_group_or_404(group_id, db))}


@router.post("/")
def create_group(body: GroupCreate, db: Session = Depends(get_db)):
    if db.query(ApplicationGroup).filter(ApplicationGroup.name == body.name).first():
        raise HTTPException(status_code=400, detail=f"A group named '{body.name}' already exists.")

    group = ApplicationGroup(
        id=str(uuid.uuid4()), name=body.name, description=body.description
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    return {"status": "created", "group": _serialize(group)}


@router.put("/{group_id}")
def update_group(group_id: str, body: GroupUpdate, db: Session = Depends(get_db)):
    group = _get_group_or_404(group_id, db)
    for key, value in body.model_dump(exclude_unset=True, exclude_none=True).items():
        setattr(group, key, value)
    db.commit()
    db.refresh(group)
    return {"status": "updated", "group": _serialize(group)}


@router.delete("/{group_id}")
def delete_group(group_id: str, db: Session = Depends(get_db)):
    """Delete a group. Member projects are detached, never deleted."""
    group = _get_group_or_404(group_id, db)

    detached = db.query(TestProject).filter(TestProject.group_id == group_id).update(
        {"group_id": None}, synchronize_session=False
    )
    db.delete(group)
    db.commit()
    return {"status": "deleted", "id": group_id, "projects_detached": detached}


@router.put("/{group_id}/projects")
def set_group_members(group_id: str, body: GroupMembers, db: Session = Depends(get_db)):
    """Replace this group's membership with *project_ids*."""
    _get_group_or_404(group_id, db)

    # Detach everything currently in the group, then attach the requested set.
    db.query(TestProject).filter(TestProject.group_id == group_id).update(
        {"group_id": None}, synchronize_session=False
    )
    if body.project_ids:
        db.query(TestProject).filter(TestProject.id.in_(body.project_ids)).update(
            {"group_id": group_id}, synchronize_session=False
        )
    db.commit()

    return {"status": "updated", "group": _serialize(_get_group_or_404(group_id, db))}


@router.get("/{group_id}/impact-scope")
def get_impact_scope(group_id: str, db: Session = Depends(get_db)):
    """Every project a change in this group could affect.

    This is the hook future cross-app impact analysis executes against: it
    returns the sibling apps whose suites should also run when one app in the
    group changes.
    """
    group = _get_group_or_404(group_id, db)

    return {
        "group_id": group.id,
        "group_name": group.name,
        "scope": [
            {
                "project_id": p.id,
                "name": p.name,
                "platform": p.platform,
                "project_type": p.project_type,
                "default_branch": p.default_branch,
                "executable": p.clone_status in ("cloned", "ready", "outdated"),
            }
            for p in group.projects
        ],
    }
