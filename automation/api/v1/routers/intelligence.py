from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from typing import Dict, Any
from pydantic import BaseModel
from sqlalchemy.orm import Session
from automation.database.config import get_db
import json

from automation.auth.security import get_current_user
from automation.intelligence.chat import chat_assistant

router = APIRouter(prefix="/intelligence", tags=["intelligence"])


class ChatRequest(BaseModel):
    query: str
    context: Dict[str, Any] = None

@router.get("/runs/{run_id}/rca")
def get_run_rca(run_id: str, db: Session = Depends(get_db)):
    """Return the stored RCA report for a run.

    Queries the RCAReport table directly by run_id. Returns 200 with
    ``available: false`` when no report exists yet (instead of a hard 404) so
    the dashboard can render a friendly message.
    """
    from automation.database.models import RCAReport

    report = db.query(RCAReport).filter(RCAReport.run_id == run_id).first()
    if not report:
        return {"run_id": run_id, "available": False, "rca_summary": None,
                "root_cause": None, "suggested_fix": None}

    return {
        "run_id": run_id,
        "available": True,
        "rca_summary": report.summary,
        "root_cause": report.root_cause,
        "suggested_fix": report.suggested_fix,
        "failure_category": report.failure_category,
        "confidence": report.confidence,
        "severity": report.severity,
        "priority": report.priority,
        "llm_provider": report.llm_provider,
        "llm_model": report.llm_model,
        "generated_at": report.generated_at.isoformat() if report.generated_at else None,
    }

@router.post("/chat")
def ask_chat(req: ChatRequest, current_user=Depends(get_current_user)):
    response = chat_assistant.ask(req.query, req.context)
    return {"reply": response}

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
