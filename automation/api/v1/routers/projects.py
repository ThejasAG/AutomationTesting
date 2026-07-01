from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Dict, Any
import uuid
import os

from automation.database import database
from automation.database.config import get_db
from automation.projects.repository import repository_manager
from automation.auth.security import get_current_user
from pydantic import BaseModel

router = APIRouter(prefix="/projects", tags=["projects"])

class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    git_url: str
    default_branch: str = "main"

@router.get("/")
def list_projects(db: Session = Depends(get_db)):
    projects = database.get_test_projects(db)
    result = []
    for p in projects:
        health = repository_manager.get_health(p.id)
        result.append({
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "git_url": p.git_url,
            "default_branch": p.default_branch,
            "status": p.status,
            "health": health
        })
    return {"projects": result}

@router.post("/")
def create_project(project: ProjectCreate, db: Session = Depends(get_db)):
    project_id = str(uuid.uuid4())
    data = {
        "id": project_id,
        "name": project.name,
        "description": project.description,
        "git_url": project.git_url,
        "default_branch": project.default_branch,
        "status": "active"
    }
    database.insert_test_project(db, data)
    
    # Optionally trigger clone immediately
    repository_manager.clone_or_pull(project_id, project.git_url, project.default_branch)
    
    return {"status": "success", "id": project_id}
