"""MCP servers connected to the platform (Settings → MCP servers).

Listing is for any signed-in user. Adding, changing, removing and testing a
server is admin-only: a stdio server is a command this Mac runs.

Env values and HTTP headers often hold keys: they are stored, never sent back.
A masked value posted back unchanged keeps the stored one.
"""
import os
import uuid
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from automation.auth.security import get_current_user, require_role
from automation.database.config import SessionLocal
from automation.database.models import McpServer
from automation.mcp.registry import registry, server_dict

router = APIRouter(prefix="/mcp", tags=["MCP servers"])

MASK = "••••••"
_admin = Depends(require_role(["admin"]))


class McpServerIn(BaseModel):
    name: str
    transport: str = "stdio"                 # "stdio" | "http"
    command: str = ""
    args: List[str] = []
    env: Dict[str, str] = {}
    url: str = ""
    headers: Dict[str, str] = {}
    enabled: bool = True
    drives_devices: bool = False
    description: str = ""


def _public(d: dict) -> dict:
    out = dict(d)
    out["env"] = {k: MASK for k in (d.get("env") or {})}
    out["headers"] = {k: MASK for k in (d.get("headers") or {})}
    return out


def _merge_secret(new: Dict[str, str], old: Optional[Dict[str, str]]) -> Dict[str, str]:
    old = old or {}
    return {k: (old.get(k, "") if v == MASK else v) for k, v in new.items()}


def _validate(body: McpServerIn) -> None:
    name = body.name.strip()
    if not name or len(name) > 64:
        raise HTTPException(400, "Give the server a name (up to 64 characters).")
    if body.transport not in ("stdio", "http"):
        raise HTTPException(400, "Transport must be 'stdio' or 'http'.")
    if body.transport == "stdio" and not body.command.strip():
        raise HTTPException(400, "A stdio server needs the command that starts it.")
    if body.transport == "http" and not body.url.strip().startswith(("http://", "https://")):
        raise HTTPException(400, "An HTTP server needs its URL (http:// or https://).")


def _get(db, server_id: str) -> McpServer:
    row = db.query(McpServer).filter(McpServer.id == server_id).first()
    if not row:
        raise HTTPException(404, "No such MCP server.")
    return row


@router.get("/servers", dependencies=[Depends(get_current_user)])
def list_servers():
    return {"servers": [_public(s) for s in registry.servers()]}


@router.get("/presets", dependencies=[Depends(get_current_user)])
def presets():
    """One-click setups for the servers this platform already uses with Claude Code."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
    return {"presets": [
        {"name": "appium-mcp", "transport": "stdio", "command": "npx",
         "args": ["-y", "appium-mcp@1.95.1"],
         "env": {"NO_UI": "true",
                 "SCREENSHOTS_DIR": os.path.join(root, "logs", "mcp-screenshots")},
         "drives_devices": True,
         "description": "Appium: drive the simulators through the platform's Appium "
                        "server (127.0.0.1:4723). Avoid page-source on Vya screens."},
        {"name": "maestro", "transport": "stdio",
         "command": os.path.expanduser("~/.maestro/bin/maestro"), "args": ["mcp"],
         "env": {"MAESTRO_CLI_NO_ANALYTICS": "1",
                 "JAVA_HOME": "/Library/Java/JavaVirtualMachines/zulu-17.jdk/Contents/Home"},
         "drives_devices": True,
         "description": "Maestro: inspect screens and run YAML flows on the simulators."},
    ]}


@router.post("/servers")
def create_server(body: McpServerIn, user=_admin):
    _validate(body)
    with SessionLocal() as db:
        if db.query(McpServer).filter(McpServer.name == body.name.strip()).first():
            raise HTTPException(400, f"A server named '{body.name}' already exists.")
        row = McpServer(id=str(uuid.uuid4()), name=body.name.strip(),
                        transport=body.transport, command=body.command.strip(),
                        args=body.args, env=body.env, url=body.url.strip(),
                        headers=body.headers, enabled=body.enabled,
                        drives_devices=body.drives_devices, description=body.description)
        db.add(row)
        db.commit()
        return {"server": _public(server_dict(row))}


@router.put("/servers/{server_id}")
def update_server(server_id: str, body: McpServerIn, user=_admin):
    _validate(body)
    with SessionLocal() as db:
        row = _get(db, server_id)
        clash = db.query(McpServer).filter(McpServer.name == body.name.strip(),
                                           McpServer.id != server_id).first()
        if clash:
            raise HTTPException(400, f"A server named '{body.name}' already exists.")
        row.name, row.transport = body.name.strip(), body.transport
        row.command, row.args, row.url = body.command.strip(), body.args, body.url.strip()
        row.env = _merge_secret(body.env, row.env)
        row.headers = _merge_secret(body.headers, row.headers)
        row.enabled, row.drives_devices = body.enabled, body.drives_devices
        row.description = body.description
        db.commit()
        out = server_dict(row)
    registry.stop(server_id)                         # restart with the new settings
    return {"server": _public(out)}


@router.delete("/servers/{server_id}")
def delete_server(server_id: str, user=_admin):
    with SessionLocal() as db:
        row = _get(db, server_id)
        db.delete(row)
        db.commit()
    registry.stop(server_id)
    return {"deleted": server_id}


@router.post("/servers/{server_id}/test")
def test_server(server_id: str, user=_admin):
    """Start (or restart) the server and list its tools."""
    with SessionLocal() as db:
        cfg = server_dict(_get(db, server_id))
    return registry.test(cfg)
