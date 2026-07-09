from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from sqlalchemy.orm import Session
from automation.database.config import get_db
from automation.database.database import get_ai_recommendations, update_ai_recommendation
import httpx
import json
import uuid

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


# ── SSE Streaming Chat ────────────────────────────────────────────────────────

@router.get("/chat/stream")
def stream_chat(
    session_id: str,
    message: str,
    current_user=Depends(get_current_user),
):
    """Server-Sent Events endpoint for streaming LLM responses.

    Query params:
      session_id — UUID string identifying the conversation thread.
      message    — The user's new message text.

    Returns text/event-stream.  Each event is one of:
      data: {"token": "...", "done": false}   — partial token
      data: {"token": "", "done": true}       — stream complete (text)
      data: {"type": "structured", "payload": {...}, "done": true}  — analysis card

    On error the stream still closes cleanly with a user-friendly message.
    """
    if not session_id or not message.strip():
        raise HTTPException(status_code=400, detail="session_id and message are required")

    def event_generator():
        try:
            yield from chat_assistant.stream_chat(session_id, message.strip())
        except Exception as exc:
            import logging
            logging.getLogger("intelligence.stream").error("SSE generator error: %s", exc)
            error_event = json.dumps({
                "token": "⚠️ Something went wrong. Please try again.",
                "done": True,
            })
            yield f"data: {error_event}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ── Chat Session Management ───────────────────────────────────────────────────

@router.get("/chat/sessions")
def list_chat_sessions(current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    """Return the 20 most recent chat sessions with metadata.

    Response shape:
      {"sessions": [{"session_id", "title", "created_at", "message_count"}]}
    """
    from automation.database.models import ChatSession, ChatMessage
    from sqlalchemy import func

    rows = (
        db.query(
            ChatSession.id,
            ChatSession.title,
            ChatSession.created_at,
            func.count(ChatMessage.id).label("message_count"),
        )
        .outerjoin(ChatMessage, ChatMessage.session_id == ChatSession.id)
        .group_by(ChatSession.id)
        .order_by(ChatSession.created_at.desc())
        .limit(20)
        .all()
    )

    return {
        "sessions": [
            {
                "session_id": r.id,
                "title": r.title or "Untitled chat",
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "message_count": r.message_count,
            }
            for r in rows
        ]
    }


@router.get("/chat/sessions/{session_id}")
def get_chat_session(
    session_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the full message history for a session.

    Response shape:
      {"session_id", "title", "messages": [{"role", "content", "message_type", "timestamp"}]}
    """
    from automation.database.models import ChatSession

    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "session_id": session.id,
        "title": session.title or "Untitled chat",
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                "message_type": m.message_type or "text",
                "timestamp": m.created_at.isoformat() if m.created_at else None,
            }
            for m in session.messages
        ],
    }


@router.delete("/chat/sessions/{session_id}")
def delete_chat_session(
    session_id: str,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a session and all its messages (cascade).

    Returns: {"deleted": true}
    """
    from automation.database.models import ChatSession

    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    db.delete(session)
    db.commit()
    return {"deleted": True}


# ── Script Generation (unchanged) ────────────────────────────────────────────

class GenerateScriptRequest(BaseModel):
    provider: str  # openai | gemini | claude
    api_key: str
    app_name: str
    platform: str = "Android"
    framework: str = "Appium + pytest (Python)"
    requirements: str


def _call_openai(api_key: str, model: str, system: str, user: str) -> str:
    resp = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": 0.2},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _call_gemini(api_key: str, user: str) -> str:
    resp = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}",
        json={"contents": [{"parts": [{"text": user}]}]},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


def _call_claude(api_key: str, system: str, user: str) -> str:
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": "claude-3-haiku-20240307", "max_tokens": 4096, "system": system, "messages": [{"role": "user", "content": user}]},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


@router.post("/generate-scripts")
def generate_scripts(req: GenerateScriptRequest, current_user=Depends(get_current_user)):
    """Use the user's own LLM API key to generate Appium test scripts and automation.yaml."""
    SYSTEM_PROMPT = """You are an expert mobile test automation engineer.
You write clean, production-ready Appium test scripts using Python + pytest and the Page Object Model pattern.
Always return a JSON object with exactly two keys:
  - test_script: the complete Python test file as a string
  - automation_yaml: the complete automation.yaml config as a string
Do NOT include any markdown fences, explanations, or extra text — only valid JSON."""

    USER_PROMPT = f"""Generate Appium test scripts for the following:

App: {req.app_name}
Platform: {req.platform}
Framework: {req.framework}

Test Requirements:
{req.requirements}

Requirements for the generated code:
1. Use Page Object Model (separate page classes from tests)
2. Use a pytest conftest.py fixture for the Appium driver session
3. Driver reads APPIUM_SERVER_URL from environment variable (default http://127.0.0.1:4723)
4. Include proper assertions, docstrings, and error handling
5. The automation.yaml must follow this schema exactly:
   project.name, repository.branch, framework.type, framework.language,
   environment.platform, environment.app, execution.command, execution.retries,
   evidence.screenshots, ai.enabled, ai.rca

Return only valid JSON with keys test_script and automation_yaml."""

    try:
        if req.provider == "openai":
            raw = _call_openai(req.api_key, "gpt-4o-mini", SYSTEM_PROMPT, USER_PROMPT)
        elif req.provider == "gemini":
            raw = _call_gemini(req.api_key, SYSTEM_PROMPT + "\n\n" + USER_PROMPT)
        elif req.provider == "claude":
            raw = _call_claude(req.api_key, SYSTEM_PROMPT, USER_PROMPT)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown provider: {req.provider}")

        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        clean = clean.strip().rstrip("`")

        data = json.loads(clean)
        return {"test_script": data["test_script"], "automation_yaml": data["automation_yaml"]}

    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"LLM API error ({req.provider}): {e.response.text[:300]}")
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"LLM returned invalid JSON: {str(e)}. Raw: {raw[:300]}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
