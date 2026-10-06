"""
AI Chat Assistant with streaming, context injection, and structured output.

Architecture
------------
- ``AIChatAssistant.ask()``        — legacy sync call (kept for backward compat)
- ``AIChatAssistant.stream_chat()``— SSE generator used by the stream endpoint
- Context injection detects platform-related keywords and auto-prepends
  recent run data / RCA summaries so the LLM can give specific answers.
- Structured output is triggered by analysis-intent keywords; the LLM is
  asked for a JSON analysis payload that the frontend renders as cards.
- All DB lookups fail silently — a DB error never blocks the chat.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime
from typing import Dict, Any, Generator, Optional

import requests as _requests  # sync requests for streaming; avoids async complexity

from automation.ai.provider import LLMProvider, create_provider

logger = logging.getLogger(__name__)

# ── Keyword sets ──────────────────────────────────────────────────────────────

_CONTEXT_KEYWORDS: frozenset[str] = frozenset({
    "run_id", "test", "fail", "failed", "why did", "what failed",
    "last run", "error", "suite", "flaky", "broken", "crash",
    "assertion", "timeout", "device", "status", "result",
})

_ANALYSIS_KEYWORDS: frozenset[str] = frozenset({
    "flaky", "pattern", "recommend", "analyze", "analysis",
    "summary", "which tests", "top failures", "most common",
    "failure trend", "stability", "report", "overview",
})

# Cap injected context at roughly 2000 chars (not tokens, but close enough
# for llama3.2 with its ~4096 token context window).
_MAX_CONTEXT_CHARS = 2000

# Ollama base URL (can be overridden via environment).
_OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
_OLLAMA_MODEL = os.getenv("LLM_MODEL_NAME", "llama3.2")

# Chat runs on local Ollama unless the environment says otherwise — matching
# automation.ai.service.default_rca_config(). The module-level provider in
# automation.ai.provider defaults to the RCA-shaped mock, which is useless for
# chat, so the assistant builds its own provider here.
_PROVIDER_TYPE = os.getenv("LLM_PROVIDER_TYPE", "ollama").lower()

_provider_cache: Optional[LLMProvider] = None


def _chat_provider() -> LLMProvider:
    """The chat LLM provider, built once and reused."""
    global _provider_cache
    if _provider_cache is None:
        _provider_cache = create_provider({
            "provider": _PROVIDER_TYPE,
            "type": _PROVIDER_TYPE,
            "model": _OLLAMA_MODEL,
            "model_name": _OLLAMA_MODEL,
            "base_url": _OLLAMA_BASE,
            "api_key": os.getenv("OPENAI_API_KEY", "dummy"),
        })
    return _provider_cache


def _generate_text(system: str, prompt: str) -> str:
    """Single-shot completion as plain text.

    ``LLMProvider.generate()`` takes the system and user prompts separately and
    returns an ``LLMResponse`` — not a string.
    """
    return _chat_provider().generate(system_prompt=system, user_prompt=prompt).content

SYSTEM_PROMPT = """You are an AI Test Intelligence Assistant for a Mobile Test \
Automation Platform. You help QA engineers understand test failures, flaky tests, \
module stability, and execution history. Be concise, specific, and actionable.
When you have platform context, reference it directly in your answer.
Format responses with markdown: **bold** for key terms, `code` for identifiers."""

ANALYSIS_SYSTEM_PROMPT = """You are a test analytics engine. Return ONLY valid JSON \
with no prose, no markdown fences. Schema:
{
  "type": "analysis",
  "cards": [
    {
      "title": "string",
      "value": "string",
      "trend": "up" | "down" | "stable",
      "detail": "string"
    }
  ],
  "recommendation": "string"
}
Produce 2-4 cards summarising the platform state. Base answers on the context provided."""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _matches_keywords(text: str, keywords: frozenset[str]) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in keywords)


def _build_context_block(message: str) -> str:
    """Query the DB for recent runs / RCA and build a plain-text context block.

    Returns an empty string if the DB is unavailable or nothing relevant found.
    All exceptions are swallowed.
    """
    try:
        from automation.database.config import SessionLocal
        from automation.database.models import TestRun, RCAReport

        with SessionLocal() as db:
            # Recent 5 runs
            runs = (
                db.query(TestRun)
                .order_by(TestRun.created_at.desc())
                .limit(5)
                .all()
            )

            lines = ["PLATFORM CONTEXT:"]
            lines.append("Recent runs:")
            for r in runs:
                ts = r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "?"
                lines.append(
                    f"  - [{ts}] {r.test_name} | {r.status} | {r.duration_ms or 0}ms"
                    f" | device={r.device_name or '?'} | job_state={r.job_state or '?'}"
                )

            # If message mentions a run_id-like token, fetch its RCA
            run_id_match = re.search(
                r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
                message,
                re.I,
            )
            if run_id_match:
                rid = run_id_match.group(1)
                rca = db.query(RCAReport).filter(RCAReport.run_id == rid).first()
                if rca:
                    lines.append(f"\nRCA for run {rid}:")
                    lines.append(f"  Category : {rca.failure_category}")
                    lines.append(f"  Root cause: {rca.root_cause}")
                    lines.append(f"  Fix       : {rca.suggested_fix}")
                else:
                    # Try to find RCA for most recent failed run if no explicit id
                    pass

            context = "\n".join(lines)
            return context[:_MAX_CONTEXT_CHARS]

    except Exception as exc:
        logger.debug("Context injection skipped (DB error): %s", exc)
        return ""


def _ollama_stream(system: str, prompt: str) -> Generator[str, None, None]:
    """Stream tokens from Ollama's /api/generate endpoint.

    Yields one decoded token string at a time.
    Falls back to a single-chunk yield if streaming fails.
    """
    try:
        resp = _requests.post(
            f"{_OLLAMA_BASE}/api/generate",
            json={
                "model": _OLLAMA_MODEL,
                "system": system,
                "prompt": prompt,
                "stream": True,
                "options": {"temperature": 0.3, "num_predict": 1024},
            },
            stream=True,
            timeout=120,
        )
        resp.raise_for_status()
        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            try:
                chunk = json.loads(raw_line)
                token = chunk.get("response", "")
                if token:
                    yield token
                if chunk.get("done", False):
                    break
            except json.JSONDecodeError:
                continue
    except Exception as exc:
        logger.warning("Ollama streaming failed at %s: %s", _OLLAMA_BASE, exc)
        # Retrying the same unreachable server through the sync provider would
        # only land on its mock fallback, which answers chat with RCA-shaped
        # JSON. Say what is actually wrong instead.
        yield (
            f"⚠️ I can't reach the Ollama server at `{_OLLAMA_BASE}`. "
            f"Start it with `ollama serve` and make sure the `{_OLLAMA_MODEL}` "
            f"model is pulled (`ollama pull {_OLLAMA_MODEL}`)."
        )


def _mcp_enabled() -> bool:
    try:
        from automation.mcp.registry import registry
        return bool(registry.servers(enabled_only=True))
    except Exception:
        return False


def _generic_provider_stream(system: str, prompt: str) -> Generator[str, None, None]:
    """For non-Ollama providers: call generate() synchronously and yield at once."""
    try:
        yield _generate_text(system, prompt)
    except Exception as exc:
        logger.error("LLM generate failed: %s", exc)
        yield "I'm temporarily unable to process your request. Please try again."


# ── MCP tools (Settings → MCP servers) ──────────────────────────────────────

_MAX_TOOL_ROUNDS = 6

_TOOLS_NOTE = """

You can call tools from MCP servers connected to this platform (each tool's \
description starts with its server in [brackets]). Use them only when the user \
asks you to look at or act on a device, app or service; answer from what they \
return, and say plainly when a tool fails or is refused. Never invent results."""


def _openai_compatible() -> bool:
    return _PROVIDER_TYPE in ("groq", "openai")


def _clean_schema(schema):
    """Model APIs reject some JSON-Schema keys that MCP servers include."""
    if isinstance(schema, dict):
        return {k: _clean_schema(v) for k, v in schema.items() if k not in ("$schema", "$id")}
    if isinstance(schema, list):
        return [_clean_schema(v) for v in schema]
    return schema


def _tool_chat(system: str, prompt: str) -> Generator[str, None, None]:
    """The chat with the connected MCP servers' tools (OpenAI-compatible API:
    Groq or OpenAI). Each tool call is shown as a line in the reply, then the
    model answers from the results. Without tools it is the plain chat."""
    from automation.mcp.registry import registry
    try:
        functions, route = registry.chat_tools()
    except Exception as exc:
        logger.warning("MCP tools unavailable: %s", exc)
        functions, route = [], {}
    if not functions:
        yield from _generic_provider_stream(system, prompt)
        return
    for f in functions:
        f["function"]["parameters"] = _clean_schema(f["function"]["parameters"])
    base = (os.getenv("LLM_API_BASE") or ("https://api.groq.com/openai/v1"
            if _PROVIDER_TYPE == "groq" else "https://api.openai.com/v1")).rstrip("/")
    headers = {"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY', '')}",
               "Content-Type": "application/json"}
    messages = [{"role": "system", "content": system + _TOOLS_NOTE},
                {"role": "user", "content": prompt}]
    for _ in range(_MAX_TOOL_ROUNDS):
        try:
            resp = _requests.post(f"{base}/chat/completions", headers=headers, timeout=120, json={
                "model": _OLLAMA_MODEL, "messages": messages, "tools": functions,
                "tool_choice": "auto", "temperature": 0.2, "max_tokens": 2048})
            resp.raise_for_status()
            msg = resp.json()["choices"][0]["message"]
        except Exception as exc:
            logger.error("tool chat failed: %s", exc)
            yield "\n\n⚠️ The model could not complete the tool call; answering without tools.\n\n"
            yield from _generic_provider_stream(system, prompt)
            return
        calls = msg.get("tool_calls") or []
        if not calls:
            yield msg.get("content") or ""
            return
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
        for call in calls:
            fname = (call.get("function") or {}).get("name", "")
            try:
                args = json.loads((call.get("function") or {}).get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            cfg, tool = route.get(fname, (None, ""))
            label = f"{cfg['name']} · {tool}" if cfg else fname
            yield f"\n> 🔧 `{label}`"
            out = registry.call(cfg, tool, args) if cfg else f"ERROR: unknown tool {fname}"
            yield (" ✗\n" if out.startswith(("ERROR", "REFUSED")) else " ✓\n")
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": out})
        yield "\n"
    yield f"\n(stopped after {_MAX_TOOL_ROUNDS} rounds of tool calls)"


def _get_token_stream(system: str, prompt: str) -> Generator[str, None, None]:
    """Route streaming to the appropriate provider."""
    if _openai_compatible() and _mcp_enabled():
        yield from _tool_chat(system, prompt)
        return
    if _PROVIDER_TYPE == "ollama":
        yield from _ollama_stream(system, prompt)
    else:
        yield from _generic_provider_stream(system, prompt)


# ── Persistence helpers ───────────────────────────────────────────────────────

def _ensure_session(session_id: str, first_user_msg: str) -> None:
    """Create the ChatSession row if it doesn't exist yet."""
    try:
        from automation.database.config import SessionLocal
        from automation.database.models import ChatSession

        with SessionLocal() as db:
            existing = db.query(ChatSession).filter(ChatSession.id == session_id).first()
            if not existing:
                title = first_user_msg[:100].strip()
                db.add(ChatSession(id=session_id, title=title))
                db.commit()
    except Exception as exc:
        logger.debug("Session creation skipped: %s", exc)


def _save_message(session_id: str, role: str, content: str, message_type: str = "text") -> None:
    """Persist a single chat message."""
    try:
        from automation.database.config import SessionLocal
        from automation.database.models import ChatMessage

        with SessionLocal() as db:
            db.add(
                ChatMessage(
                    id=str(uuid.uuid4()),
                    session_id=session_id,
                    role=role,
                    content=content,
                    message_type=message_type,
                )
            )
            db.commit()
    except Exception as exc:
        logger.debug("Message persist skipped: %s", exc)


# ── Main assistant class ──────────────────────────────────────────────────────

class AIChatAssistant:
    """Primary AI chat assistant.

    Maintains backward compatibility with the existing ``ask()`` method while
    adding SSE streaming via ``stream_chat()``.
    """

    def __init__(self) -> None:
        self.system_prompt = SYSTEM_PROMPT

    # ------------------------------------------------------------------
    # Legacy sync method — keeps the existing POST /chat endpoint working
    # ------------------------------------------------------------------

    def ask(self, query: str, context: Optional[Dict[str, Any]] = None) -> str:
        """Synchronous chat — returns full reply string."""
        ctx_str = f"Context: {context}" if context else ""
        auto_ctx = _build_context_block(query) if _matches_keywords(query, _CONTEXT_KEYWORDS) else ""

        full_context = "\n\n".join(filter(None, [auto_ctx, ctx_str]))
        prompt = f"{full_context}\n\nUser: {query}" if full_context else f"User: {query}"

        try:
            return _generate_text(self.system_prompt, prompt)
        except Exception as exc:
            logger.error("ChatAssistant.ask failed: %s", exc)
            return "I'm currently unable to process your request due to a backend error."

    # ------------------------------------------------------------------
    # Streaming method — yields SSE-formatted strings
    # ------------------------------------------------------------------

    def stream_chat(
        self,
        session_id: str,
        message: str,
    ) -> Generator[str, None, None]:
        """SSE generator.  Caller wraps this in a StreamingResponse.

        Yields:
            SSE-formatted strings:
              ``data: {"token": "...", "done": false}\\n\\n``
              ``data: {"token": "", "done": true}\\n\\n``
            For structured responses:
              ``data: {"type": "structured", "payload": {...}, "done": true}\\n\\n``
        """
        # 1. Persist session + user message
        _ensure_session(session_id, message)
        _save_message(session_id, "user", message)

        # 2. Decide prompt strategy
        is_analysis = _matches_keywords(message, _ANALYSIS_KEYWORDS)
        has_run_context = _matches_keywords(message, _CONTEXT_KEYWORDS)

        context_block = ""
        if has_run_context or is_analysis:
            context_block = _build_context_block(message)

        if is_analysis:
            yield from self._stream_structured(session_id, message, context_block)
            return

        # 3. Normal streaming response
        system = self.system_prompt
        prompt_parts = []
        if context_block:
            prompt_parts.append(context_block)
        prompt_parts.append(f"User: {message}")
        prompt = "\n\n".join(prompt_parts)

        accumulated = []
        try:
            for token in _get_token_stream(system, prompt):
                accumulated.append(token)
                payload = json.dumps({"token": token, "done": False})
                yield f"data: {payload}\n\n"

            # Final "done" event
            yield f'data: {json.dumps({"token": "", "done": True})}\n\n'

            # Persist assembled reply
            full_reply = "".join(accumulated)
            _save_message(session_id, "assistant", full_reply, "text")

        except Exception as exc:
            logger.error("stream_chat error: %s", exc)
            error_payload = json.dumps({
                "token": "\n\n⚠️ An error occurred while generating the response.",
                "done": True,
            })
            yield f"data: {error_payload}\n\n"

    def _stream_structured(
        self,
        session_id: str,
        message: str,
        context_block: str,
    ) -> Generator[str, None, None]:
        """Ask the LLM for a JSON analysis payload and yield it as one SSE event."""
        prompt_parts = []
        if context_block:
            prompt_parts.append(context_block)
        prompt_parts.append(f"User request: {message}")
        prompt = "\n\n".join(prompt_parts)

        try:
            # For structured output we call synchronously and parse the JSON.
            raw = _generate_text(ANALYSIS_SYSTEM_PROMPT, prompt)

            # Strip markdown fences if the model added them
            clean = raw.strip()
            if clean.startswith("```"):
                parts = clean.split("```")
                clean = parts[1] if len(parts) > 1 else clean
                if clean.startswith("json"):
                    clean = clean[4:]
            clean = clean.strip()

            parsed = json.loads(clean)
            # Ensure "type" is set
            parsed["type"] = "analysis"

            payload = json.dumps({"type": "structured", "payload": parsed, "done": True})
            yield f"data: {payload}\n\n"

            # Persist as structured message
            _save_message(session_id, "assistant", clean, "structured")

        except (json.JSONDecodeError, Exception) as exc:
            logger.warning("Structured output failed, falling back to text: %s", exc)
            # Degrade gracefully: stream a plain text answer instead
            system = self.system_prompt
            prompt_text = f"{context_block}\n\nUser: {message}" if context_block else f"User: {message}"
            accumulated = []
            for token in _get_token_stream(system, prompt_text):
                accumulated.append(token)
                yield f"data: {json.dumps({'token': token, 'done': False})}\n\n"
            yield f'data: {json.dumps({"token": "", "done": True})}\n\n'
            _save_message(session_id, "assistant", "".join(accumulated), "text")


chat_assistant = AIChatAssistant()
