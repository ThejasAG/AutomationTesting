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
    covers: List[str] = []      # screens/modules this scenario exercises


@router.get("/devices")
def scenario_devices(current_user=Depends(get_current_user)):
    """Available iOS simulators for the scenario's device picker (Booted first).

    Each one carries the app bundle ids installed on it, so the Run modal can say up
    front which environment/device pairs will actually work. Without it the only way
    to find out was to start a run and wait ~40s for Appium to fail with "App with
    bundle identifier '…' unknown" — an error that names neither the device nor what
    it does have.
    """
    from concurrent.futures import ThreadPoolExecutor
    from automation.scenarios.cross_app_config import list_ios_simulators
    from automation.projects.builder import app_builder
    sims = list_ios_simulators()

    # BOOTED ONLY, and in parallel. `simctl listapps` needs a running simulator: asking
    # all 26 took 30.6s and every shut-down one answered with an empty list anyway —
    # which would then read as "this device has no apps" and wrongly grey it out.
    # A shut-down sim keeps apps: null = UNKNOWN, and the UI must not gate on unknown.
    def _apps(sim):
        if (sim.get("state") or "") != "Booted":
            sim["apps"] = None
            return
        sim["apps"] = [b for b in app_builder.installed_bundles(sim["udid"])
                       if not b.startswith("com.apple.") and "WebDriverAgent" not in b]

    targets = [s for s in sims if isinstance(s, dict) and s.get("udid")]
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(_apps, targets))
    return {"simulators": sims}


def _app_screens(project_id: str) -> List[str]:
    """Screen/module names of the app under test — the vocabulary coverage tags
    should use so they line up with the dependency graph's affected modules."""
    import os
    from automation.projects.repository import repository_manager
    repo = repository_manager.get_repo_path(project_id)
    if not repo:
        return []
    screens = set()
    for root, dirs, _ in os.walk(repo):
        if any(skip in root for skip in ("node_modules", ".git", "Pods", "build", "ios/", "android/")):
            continue
        base = os.path.basename(root).lower()
        if base in ("screens", "views", "pages"):
            for d in dirs:
                if not d.startswith(".") and not d.startswith("_"):
                    screens.add(d)
    return sorted(screens)


class SuggestCoversIn(BaseModel):
    project_id: str
    name: str = ""
    steps: List[str] = []


@router.post("/suggest-covers")
def suggest_covers(body: SuggestCoversIn, current_user=Depends(get_current_user)):
    """Suggest coverage tags for a scenario: which of the app's screens its steps
    exercise. Grounded on the real screen list so the tags match the graph."""
    import json as _json
    import os
    import httpx
    screens = _app_screens(body.project_id)
    if not screens:
        return {"covers": [], "available": []}

    system = ("You map a mobile test scenario to the app screens it exercises. "
              "Return ONLY JSON {\"covers\": [screen names]}. Choose ONLY from the "
              "provided screen list; include every screen the steps clearly pass "
              "through (e.g. a browse-and-add-to-cart flow covers Home, StoreView, Cart).")
    user = (f"Scenario: {body.name}\nSteps:\n" + "\n".join(f"- {s}" for s in body.steps) +
            f"\n\nAvailable screens: {', '.join(screens)}\n\nReturn the JSON.")
    covers: List[str] = []
    try:
        base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        model = os.getenv("LLM_MODEL_NAME", "llama3.2")
        resp = httpx.post(f"{base}/api/generate",
                          json={"model": model, "system": system, "prompt": user,
                                "stream": False, "format": "json",
                                "options": {"temperature": 0.1, "num_predict": 512}},
                          timeout=120)
        resp.raise_for_status()
        data = _json.loads(resp.json().get("response", "{}"))
        low = {s.lower(): s for s in screens}
        for c in (data.get("covers") or []):
            hit = low.get(str(c).strip().lower())
            if hit and hit not in covers:
                covers.append(hit)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not reach the local model: {e}")
    return {"covers": covers, "available": screens}


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
        covers=[c.strip() for c in body.covers if c.strip()],
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
    row.covers = [c.strip() for c in body.covers if c.strip()]
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
