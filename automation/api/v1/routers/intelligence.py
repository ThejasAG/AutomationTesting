from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict, Any
from pydantic import BaseModel
from typing import Optional
from sqlalchemy.orm import Session
from automation.database.config import get_db
from automation.database.database import get_ai_recommendations, update_ai_recommendation

from automation.auth.security import get_current_user
from automation.intelligence.recommendation import recommendation_engine
from automation.intelligence.flaky_detector import flaky_detector
from automation.intelligence.memory import failure_memory
from automation.intelligence.prediction import prediction_engine
from automation.intelligence.analytics import analytics_service
from automation.intelligence.chat import chat_assistant

router = APIRouter(prefix="/intelligence", tags=["intelligence"])

class RecommendRequest(BaseModel):
    project_id: str
    branch: str = "main"

class ChatRequest(BaseModel):
    query: str
    context: Dict[str, Any] = None

class UpdateRecommendationRequest(BaseModel):
    status: str
    feedback: Optional[str] = None

@router.get("/metrics")
def get_metrics(current_user=Depends(get_current_user)):
    return analytics_service.get_global_metrics()

@router.post("/recommend")
def get_recommendations(req: RecommendRequest, current_user=Depends(get_current_user)):
    # In a real impl, we'd lookup the repo path by project_id
    repo_path = "d:/AutomationTesting/demo-mobile-tests"
    try:
        recs = recommendation_engine.generate_recommendations(repo_path, "HEAD")
        return recs
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/flaky/{test_name}")
def get_flaky_status(test_name: str, current_user=Depends(get_current_user)):
    return flaky_detector.calculate_flakiness(test_name)

@router.get("/predict/{project_id}")
def get_prediction(project_id: str, current_user=Depends(get_current_user)):
    return prediction_engine.predict_risk(project_id)

@router.get("/memory/search")
def search_memory(error: str, test_name: str = "", current_user=Depends(get_current_user)):
    return failure_memory.search_similar_failures(error, test_name)

@router.post("/chat")
def ask_chat(req: ChatRequest, current_user=Depends(get_current_user)):
    response = chat_assistant.ask(req.query, req.context)
    return {"reply": response}

@router.get("/recommendations")
def list_recommendations(status: Optional[str] = None, db: Session = Depends(get_db)):
    recs = get_ai_recommendations(db, status=status)
    return {"recommendations": recs}

@router.put("/recommendations/{rec_id}")
def update_recommendation(rec_id: str, req: UpdateRecommendationRequest, db: Session = Depends(get_db)):
    rec = update_ai_recommendation(db, rec_id, {"status": req.status, "feedback": req.feedback})
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return {"status": "ok"}
