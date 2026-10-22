"""Turn a session's stream-json transcript into short lines for the AI Agent page.

`claude -p --output-format stream-json --verbose` writes one JSON event per line:
system/init, assistant messages (text + tool_use blocks), user messages
(tool_result blocks) and a final `result`. The page shows what Claude says, each
tool call in plain words ("Ran flow3 (fix)", "Edited automation/…"), and each
result's first lines.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

_TOOLS_CMD = re.compile(r"^\.venv/bin/python scripts/agent_tools\.py\s+")


def _rel(path: str, roots: List[str]) -> str:
    for r in roots:
        if r and path.startswith(r.rstrip("/") + "/"):
            return path[len(r.rstrip("/")) + 1:]
    return path


def describe_tool(name: str, inp: Dict[str, Any], roots: List[str]) -> str:
    if name == "Bash":
        cmd = (inp.get("command") or "").strip()
        m = _TOOLS_CMD.match(cmd)
        if m:
            args = cmd[m.end():].split()
            sub, rest = (args[0] if args else ""), args[1:]
            if sub == "run" and rest:
                label = next((rest[i + 1] for i, a in enumerate(rest[:-1]) if a == "--label"), "")
                return f"Run {rest[0]} on the simulators" + (f" ({label})" if label else "")
            return f"Tool: {sub} {' '.join(rest)}".strip()
        return f"$ {cmd[:200]}"
    if name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        verb = {"Read": "Read", "Write": "Wrote"}.get(name, "Edited")
        return f"{verb} {_rel(inp.get('file_path') or inp.get('notebook_path') or '', roots)}"
    if name == "Grep":
        return f"Searched for {inp.get('pattern', '')!r}" + (
            f" in {_rel(inp['path'], roots)}" if inp.get("path") else "")
    if name == "Glob":
        return f"Listed {inp.get('pattern', '')}"
    if name in ("Agent", "Task"):
        return f"Helper agent: {inp.get('description') or inp.get('subagent_type') or ''}"
    if name == "Skill":
        return f"Skill: {inp.get('skill') or inp.get('name') or ''}"
    if name == "TodoWrite":
        todos = inp.get("todos") or []
        doing = next((t.get("activeForm") or t.get("content") for t in todos
                      if t.get("status") == "in_progress"), "")
        return f"Plan: {sum(t.get('status') == 'completed' for t in todos)}/{len(todos)} done" + (
            f" — {doing}" if doing else "")
    return name


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text") or "")
            elif isinstance(c, dict) and c.get("type") == "image":
                parts.append("[image]")
        return "\n".join(parts)
    return ""


def condense(path: str, after: int = 0, roots: Optional[List[str]] = None,
             limit: int = 400) -> Tuple[List[Dict[str, Any]], int]:
    """Events with index > `after` (at most `limit`), and the last index read."""
    roots = roots or []
    out: List[Dict[str, Any]] = []
    i = 0
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return out, after
    with f:
        for line in f:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            for item in _items(ev, roots):
                i += 1
                if i <= after:
                    continue
                item["i"] = i
                out.append(item)
                if len(out) >= limit:
                    return out, i
    return out, max(i, after)


def _items(ev: Dict[str, Any], roots: List[str]) -> List[Dict[str, Any]]:
    t = ev.get("type")
    sub = bool(ev.get("parent_tool_use_id"))          # a helper agent's own step
    if t == "system" and ev.get("subtype") == "init":
        return [{"kind": "info", "text": f"Claude Code started ({ev.get('model', '')})"}]
    if t == "assistant":
        items = []
        for c in (ev.get("message") or {}).get("content") or []:
            if c.get("type") == "text" and (c.get("text") or "").strip():
                items.append({"kind": "text", "text": c["text"].strip()[:3000], "sub": sub})
            elif c.get("type") == "tool_use":
                items.append({"kind": "tool", "id": c.get("id"), "tool": c.get("name"),
                              "text": describe_tool(c.get("name") or "", c.get("input") or {}, roots),
                              "sub": sub})
        return items
    if t == "user":
        items = []
        content = (ev.get("message") or {}).get("content")
        for c in content if isinstance(content, list) else []:
            if isinstance(c, dict) and c.get("type") == "tool_result":
                txt = _result_text(c.get("content")).strip()
                items.append({"kind": "result", "id": c.get("tool_use_id"), "error": bool(c.get("is_error")),
                              "text": txt[:1500] + ("…" if len(txt) > 1500 else ""), "sub": sub})
        return items
    if t == "result":
        return [{"kind": "end", "error": bool(ev.get("is_error")),
                 "text": f"Claude Code finished: {ev.get('subtype')} after {ev.get('num_turns')} turns"
                         + (f" — {str(ev.get('result') or '')[:500]}" if ev.get("is_error") else "")}]
    return []


def last_activity(path: str, roots: Optional[List[str]] = None) -> str:
    """The latest tool call or message, for the session's `phase` line."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 200_000))
            tail = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(tail):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        for item in reversed(_items(ev, roots or [])):
            if item["kind"] in ("tool", "text"):
                return item["text"].splitlines()[0][:280]
    return ""


def final_result(path: str) -> Dict[str, Any]:
    """The stream's `result` event: did the session finish, and why not."""
    res: Dict[str, Any] = {}
    lines: List[str] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return res
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            res = {"subtype": ev.get("subtype"), "is_error": ev.get("is_error"),
                   "turns": ev.get("num_turns"), "duration_ms": ev.get("duration_ms"),
                   "result": (ev.get("result") or "")[:2000],
                   "api_equivalent_usd": ev.get("total_cost_usd")}
    if not res and lines:
        res = {"is_error": True, "result": "\n".join(lines[-5:])[:2000]}
    return res
