"""Live connections to the configured MCP servers, shared by the API and the chat.

One client per server, started on first use and reused. A client is restarted
when its settings change or its process has died. Tool lists are cached briefly
so a chat message does not spawn every server each time.

Device guard: a server marked `drives_devices` (Appium, Maestro) acts on the same
simulators the test runs use, and two drivers on one simulator kill each other's
sessions (measured on this platform: WDA "Session does not exist"). Its tools
are refused while a run is active.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from automation.mcp.client import HttpClient, McpError, StdioClient, result_text

logger = logging.getLogger("mcp.registry")

TOOL_CACHE_SECONDS = 60.0
ACTIVE_RUN_STATES = ("running", "queued", "preparing", "collecting_evidence")


def _cfg_key(cfg: Dict[str, Any]) -> str:
    keep = {k: cfg.get(k) for k in ("transport", "command", "args", "env", "url", "headers")}
    return hashlib.sha1(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()


def server_dict(row: Any) -> Dict[str, Any]:
    return {"id": row.id, "name": row.name, "transport": row.transport or "stdio",
            "command": row.command or "", "args": list(row.args or []),
            "env": dict(row.env or {}), "url": row.url or "",
            "headers": dict(row.headers or {}), "enabled": bool(row.enabled),
            "drives_devices": bool(row.drives_devices),
            "description": row.description or ""}


def tool_function_name(server: str, tool: str) -> str:
    """'appium-mcp' + 'appium_screenshot' -> 'appium_mcp__appium_screenshot': the
    name a model calls (letters, digits, _ and -; at most 64 characters)."""
    raw = f"{server}__{tool}"
    return re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:64]


class McpRegistry:
    def __init__(self) -> None:
        self._clients: Dict[str, Tuple[str, Any]] = {}   # id -> (cfg key, client)
        self._tools: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._lock = threading.RLock()

    # -- configuration ----------------------------------------------------------
    def servers(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        from automation.database.config import SessionLocal
        from automation.database.models import McpServer
        try:
            with SessionLocal() as db:
                q = db.query(McpServer)
                if enabled_only:
                    q = q.filter(McpServer.enabled.is_(True))
                return [server_dict(r) for r in q.order_by(McpServer.name).all()]
        except Exception as e:                       # table missing on an old DB, etc.
            logger.debug("mcp servers unavailable: %s", e)
            return []

    # -- connections --------------------------------------------------------------
    def _make(self, cfg: Dict[str, Any]):
        if cfg["transport"] == "http":
            if not cfg["url"]:
                raise McpError("no URL configured")
            return HttpClient(cfg["url"], cfg["headers"])
        if not cfg["command"]:
            raise McpError("no command configured")
        return StdioClient(cfg["command"], cfg["args"], cfg["env"])

    def client(self, cfg: Dict[str, Any]):
        key = _cfg_key(cfg)
        with self._lock:
            have = self._clients.get(cfg["id"])
            if have and have[0] == key and have[1].alive:
                return have[1]
            if have:
                self._close(cfg["id"])
            c = self._make(cfg)
            c.start()
            self._clients[cfg["id"]] = (key, c)
            return c

    def _close(self, server_id: str) -> None:
        have = self._clients.pop(server_id, None)
        self._tools.pop(server_id, None)
        if have:
            try:
                have[1].close()
            except Exception:
                pass

    def stop(self, server_id: str) -> None:
        with self._lock:
            self._close(server_id)

    def stop_all(self) -> None:
        with self._lock:
            for sid in list(self._clients):
                self._close(sid)

    # -- tools ----------------------------------------------------------------------
    def tools(self, cfg: Dict[str, Any], fresh: bool = False) -> List[Dict[str, Any]]:
        cached = self._tools.get(cfg["id"])
        if cached and not fresh and time.time() - cached[0] < TOOL_CACHE_SECONDS:
            return cached[1]
        tools = self.client(cfg).list_tools()
        self._tools[cfg["id"]] = (time.time(), tools)
        return tools

    def test(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        """Connect (restarting it) and list the tools: what the Test button shows."""
        self.stop(cfg["id"])
        t0 = time.time()
        try:
            tools = self.tools(cfg, fresh=True)
        except McpError as e:
            return {"ok": False, "error": str(e), "tools": []}
        c = self._clients.get(cfg["id"], (None, None))[1]
        return {"ok": True, "seconds": round(time.time() - t0, 1),
                "server": getattr(c, "server_info", {}) or {},
                "tools": [{"name": t.get("name"), "description": (t.get("description") or "")[:300]}
                          for t in tools]}

    def chat_tools(self) -> Tuple[List[Dict[str, Any]], Dict[str, Tuple[Dict[str, Any], str]]]:
        """Every enabled server's tools as model function definitions, and a map
        from function name back to (server config, tool name). A server that will
        not start is left out (the chat says so), never fails the chat."""
        functions: List[Dict[str, Any]] = []
        route: Dict[str, Tuple[Dict[str, Any], str]] = {}
        self.unavailable: List[str] = []
        for cfg in self.servers(enabled_only=True):
            try:
                tools = self.tools(cfg)
            except McpError as e:
                self.unavailable.append(f"{cfg['name']}: {e}")
                continue
            for t in tools:
                fname = tool_function_name(cfg["name"], t.get("name", ""))
                if fname in route:
                    continue
                route[fname] = (cfg, t.get("name", ""))
                functions.append({"type": "function", "function": {
                    "name": fname,
                    "description": f"[{cfg['name']}] {(t.get('description') or '')[:900]}",
                    "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                }})
        return functions, route

    def call(self, cfg: Dict[str, Any], tool: str, arguments: Dict[str, Any],
             timeout: float = 120.0) -> str:
        """Run one tool; its result as text for the model. Never raises."""
        if cfg.get("drives_devices"):
            busy = active_run()
            if busy:
                return (f"REFUSED: test run {busy} is using the simulators. "
                        f"{cfg['name']} drives the same devices and would break it. "
                        f"Try again when the run has finished.")
        try:
            return result_text(self.client(cfg).call_tool(tool, arguments, timeout=timeout))
        except McpError as e:
            return f"ERROR: {cfg['name']} · {tool}: {e}"


def active_run() -> str:
    """The id (8 chars) of a test run in progress, or ''."""
    try:
        from automation.database.config import SessionLocal
        from automation.database.models import TestRun
        with SessionLocal() as db:
            r = (db.query(TestRun.id).filter(TestRun.status.in_(ACTIVE_RUN_STATES))
                 .order_by(TestRun.created_at.desc()).first())
            return r[0][:8] if r else ""
    except Exception:
        return ""


registry = McpRegistry()
