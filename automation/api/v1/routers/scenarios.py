"""Saved-scenario CRUD + device list for the Scenarios tab.

A saved scenario is a named, reusable list of plain-language steps bound to an app
(project/bundle) and a target simulator. Users create/edit/delete them here; the
actual run streams through the existing scenario runner at /scenario/run/stream,
which these rows feed.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from automation.auth.security import get_current_user
from automation.database.config import get_db
from automation.database.models import SavedScenario

router = APIRouter(prefix="/scenarios", tags=["Saved Scenarios"])


class ScenarioIn(BaseModel):
    name: str
    description: Optional[str] = None
    project_id: Optional[str] = None
    bundle_id: Optional[str] = None
    device_id: Optional[str] = None
    steps: List[str] = []


@router.get("/devices")
def scenario_devices(current_user=Depends(get_current_user)):
    """Available iOS simulators for the scenario's device picker (Booted first)."""
    from automation.scenarios.cross_app_config import list_ios_simulators
    return {"simulators": list_ios_simulators()}


@router.get("")
def list_scenarios(db: Session = Depends(get_db), current_user=Depends(get_current_user)):
    rows = db.query(SavedScenario).order_by(SavedScenario.updated_at.desc()).all()
    return [r.to_dict() for r in rows]


@router.post("", status_code=201)
def create_scenario(body: ScenarioIn, db: Session = Depends(get_db),
                    current_user=Depends(get_current_user)):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Name is required.")
    row = SavedScenario(
        name=body.name.strip(), description=body.description,
        project_id=body.project_id, bundle_id=body.bundle_id,
        device_id=body.device_id, steps=[s for s in body.steps if s.strip()],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.to_dict()


@router.get("/{scenario_id}")
def get_scenario(scenario_id: str, db: Session = Depends(get_db),
                 current_user=Depends(get_current_user)):
    row = db.query(SavedScenario).filter(SavedScenario.id == scenario_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Scenario not found.")
    return row.to_dict()


@router.put("/{scenario_id}")
def update_scenario(scenario_id: str, body: ScenarioIn, db: Session = Depends(get_db),
                    current_user=Depends(get_current_user)):
    row = db.query(SavedScenario).filter(SavedScenario.id == scenario_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Scenario not found.")
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Name is required.")
    row.name = body.name.strip()
    row.description = body.description
    row.project_id = body.project_id
    row.bundle_id = body.bundle_id
    row.device_id = body.device_id
    row.steps = [s for s in body.steps if s.strip()]
    db.commit()
    db.refresh(row)
    return row.to_dict()


@router.delete("/{scenario_id}", status_code=204)
def delete_scenario(scenario_id: str, db: Session = Depends(get_db),
                    current_user=Depends(get_current_user)):
    row = db.query(SavedScenario).filter(SavedScenario.id == scenario_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Scenario not found.")
    db.delete(row)
    db.commit()
