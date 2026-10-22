"""Claude Code as the platform's language model (LLM_PROVIDER_TYPE=claude_code).

Every AI feature — PR planning, run summaries, RCA, the AI Chat — calls `claude -p`
on the logged-in Claude plan instead of an API (no key: ANTHROPIC_API_KEY is
removed from its environment). Calls run from an empty folder with the built-in
tools off, so no project files are read and nothing is edited: it answers from
the prompt it is given.

The AI Chat also gets the platform's enabled MCP servers (the Settings page) as
Claude Code MCP servers. A server that drives the simulators is refused while a
test run is using them — the same rule as the registry's own calls — by the
PreToolUse hook automation/ai/claude_chat_guard.py.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any, Dict, Generator, List, Optional, Tuple

WORKDIR = os.path.expanduser("~/.vya-platform/claude-llm")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_GUARD = os.path.join(_ROOT, "automation", "ai", "claude_chat_guard.py")
_PY = os.path.join(_ROOT, ".venv", "bin", "python")
_MODELS = ("opus", "sonnet", "haiku", "fable")


class ClaudeCodeError(RuntimeError):
    pass


def model_for(name: Optional[str]) -> str:
    """A Claude model name; anything else (an old Groq/OpenAI model id) → sonnet."""
    n = (name or "").strip().lower()
    if n in _MODELS or n.startswith("claude-"):
        return n
    return os.getenv("CLAUDE_CODE_MODEL", "sonnet")


def exe() -> str:
    return shutil.which("claude") or "/usr/local/bin/claude"


def _env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")}
    env["PATH"] = "/usr/local/bin:/opt/homebrew/bin:" + env.get("PATH", "/usr/bin:/bin")
    return env


def _base(prompt: str, system: str, model: str) -> List[str]:
    os.makedirs(WORKDIR, exist_ok=True)
    cmd = [exe(), "-p", prompt, "--model", model_for(model), "--tools", "",
           "--setting-sources", "project", "--no-session-persistence"]
    if system:
        cmd += ["--system-prompt", system]
    return cmd


def generate(system: str, prompt: str, json_schema: Optional[dict] = None,
             model: str = "", timeout: float = 240.0) -> Tuple[str, Dict[str, Any]]:
    """One answer. With `json_schema` the answer is that JSON (as text)."""
    cmd = _base(prompt, system, model) + ["--strict-mcp-config", "--output-format", "json"]
    if json_schema:
        cmd += ["--json-schema", json.dumps(json_schema)]
    try:
        out = subprocess.run(cmd, cwd=WORKDIR, capture_output=True, text=True, timeout=timeout, env=_env())
    except FileNotFoundError:
        raise ClaudeCodeError("Claude Code is not installed on this machine")
    except subprocess.TimeoutExpired:
        raise ClaudeCodeError(f"Claude Code did not answer within {int(timeout)}s")
    try:
        d = json.loads(out.stdout or "{}")
    except ValueError:
        raise ClaudeCodeError((out.stderr or out.stdout or "no output").strip()[:500])
    if d.get("is_error"):
        raise ClaudeCodeError(str(d.get("result") or d.get("subtype") or "Claude Code failed")[:500])
    if json_schema and d.get("structured_output") is not None:
        text = json.dumps(d["structured_output"])
    else:
        text = d.get("result") or ""
    usage_by_model = d.get("modelUsage") or {}
    main = max(usage_by_model, key=lambda m: (usage_by_model[m] or {}).get("outputTokens", 0),
               default=model_for(model))
    meta = {"model": main,
            "usage": d.get("usage") or {}, "duration_ms": d.get("duration_ms")}
    return text, meta


# ── the AI Chat ──────────────────────────────────────────────────────────────

def _server_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:40] or "server"


def mcp_config(servers: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """(--mcp-config JSON, allowed tool prefixes, servers that drive the simulators)."""
    conf: Dict[str, Any] = {}
    allow, devices = [], []
    for s in servers:
        name = _server_name(s.get("name") or s.get("id") or "")
        if (s.get("transport") or "stdio") == "http":
            if not s.get("url"):
                continue
            conf[name] = {"type": "http", "url": s["url"], "headers": s.get("headers") or {}}
        else:
            if not s.get("command"):
                continue
            conf[name] = {"type": "stdio", "command": s["command"], "args": list(s.get("args") or []),
                          "env": dict(s.get("env") or {})}
        allow.append(f"mcp__{name}")
        if s.get("drives_devices"):
            devices.append(name)
    return {"mcpServers": conf}, allow, devices


def stream_chat(system: str, prompt: str, servers: Optional[List[Dict[str, Any]]] = None,
                model: str = "", timeout: float = 600.0) -> Generator[str, None, None]:
    """The chat answer as it is written; each MCP tool call is shown as a line."""
    cmd = _base(prompt, system, model) + ["--output-format", "stream-json", "--verbose",
                                          "--include-partial-messages"]
    env = _env()
    conf, allow, devices = mcp_config(servers or [])
    if conf["mcpServers"]:
        settings = {"hooks": {"PreToolUse": [{"matcher": "mcp__.*", "hooks": [
            {"type": "command", "command": f"{_PY} {_GUARD}"}]}]}}
        cmd += ["--mcp-config", json.dumps(conf), "--strict-mcp-config",
                "--permission-mode", "dontAsk", "--allowedTools", *allow,
                "--settings", json.dumps(settings)]
        env["CHAT_DEVICE_SERVERS"] = ",".join(devices)
        env["PYTHONPATH"] = _ROOT
    else:
        cmd += ["--strict-mcp-config"]
    try:
        proc = subprocess.Popen(cmd, cwd=WORKDIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=env)
    except FileNotFoundError:
        yield "⚠️ Claude Code is not installed on this machine."
        return
    tool_names: Dict[str, str] = {}
    wrote = False
    try:
        for line in proc.stdout:            # type: ignore[union-attr]
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type")
            if t == "stream_event" and not ev.get("parent_tool_use_id"):
                e = ev.get("event") or {}
                d = e.get("delta") or {}
                if e.get("type") == "content_block_delta" and d.get("type") == "text_delta":
                    wrote = True
                    yield d.get("text") or ""
            elif t == "assistant":
                for c in (ev.get("message") or {}).get("content") or []:
                    if c.get("type") == "tool_use":
                        name = c.get("name") or ""
                        parts = name.split("__")
                        label = f"{parts[1]} · {'__'.join(parts[2:])}" if len(parts) >= 3 else name
                        tool_names[c.get("id") or ""] = label
                        yield f"\n> 🔧 `{label}`"
            elif t == "user":
                content = (ev.get("message") or {}).get("content")
                for c in content if isinstance(content, list) else []:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        yield " ✗\n" if c.get("is_error") else " ✓\n"
            elif t == "result":
                if ev.get("is_error"):
                    yield f"\n\n⚠️ Claude Code stopped: {str(ev.get('result') or ev.get('subtype'))[:300]}"
                elif not wrote and ev.get("result"):
                    yield ev["result"]
        proc.wait(timeout=timeout)
    finally:
        if proc.poll() is None:
            proc.kill()
