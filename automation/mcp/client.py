"""A small MCP client: JSON-RPC 2.0 over stdio or Streamable HTTP.

Only what the platform needs -- initialize, tools/list, tools/call, ping -- so it
carries no third-party SDK. Both transports expose the same three calls:

    client.start()            # spawn / connect, then the initialize handshake
    client.list_tools()       # [{"name", "description", "inputSchema"}, ...]
    client.call_tool(n, args) # the tools/call result: {"content": [...], "isError"}
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import queue
import subprocess
import threading
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("mcp.client")

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "vyapy-automation-platform", "version": "1.0"}


class McpError(Exception):
    """A server that could not be started, did not answer, or returned an error."""


class _Base:
    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self._ids = itertools.count(1)
        self.server_info: Dict[str, Any] = {}

    # -- the protocol, shared ---------------------------------------------------
    def _handshake(self) -> None:
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }, timeout=max(self.timeout, 60.0))       # npx may download on first start
        self.server_info = result.get("serverInfo") or {}
        self.notify("notifications/initialized", {})

    def list_tools(self) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        cursor = None
        for _ in range(20):                         # paginated; bounded
            params = {"cursor": cursor} if cursor else {}
            result = self.request("tools/list", params)
            tools += result.get("tools") or []
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None,
                  timeout: Optional[float] = None) -> Dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}},
                            timeout=timeout)

    # -- transport-specific ---------------------------------------------------------
    def start(self) -> None: ...
    def request(self, method: str, params: Dict[str, Any],
                timeout: Optional[float] = None) -> Dict[str, Any]: ...
    def notify(self, method: str, params: Dict[str, Any]) -> None: ...
    def close(self) -> None: ...

    @property
    def alive(self) -> bool:
        return True


class StdioClient(_Base):
    """A local server started as a subprocess, one JSON message per line."""

    def __init__(self, command: str, args: Optional[List[str]] = None,
                 env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None,
                 timeout: float = 30.0):
        super().__init__(timeout)
        self.command, self.args = command, list(args or [])
        self.env, self.cwd = dict(env or {}), cwd
        self._proc: Optional[subprocess.Popen] = None
        self._pending: Dict[int, "queue.Queue[dict]"] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self.stderr_tail: List[str] = []

    def start(self) -> None:
        try:
            self._proc = subprocess.Popen(
                [self.command, *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env={**os.environ, **self.env}, cwd=self.cwd,
                bufsize=0)
        except (OSError, ValueError) as e:
            raise McpError(f"could not start {self.command!r}: {e}") from e
        threading.Thread(target=self._read_stdout, daemon=True,
                         name=f"mcp-out-{os.path.basename(self.command)}").start()
        threading.Thread(target=self._read_stderr, daemon=True,
                         name=f"mcp-err-{os.path.basename(self.command)}").start()
        try:
            self._handshake()
        except Exception:
            self.close()
            raise

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _send(self, msg: Dict[str, Any]) -> None:
        if not self.alive:
            raise McpError(self._dead_reason())
        data = (json.dumps(msg) + "\n").encode()
        with self._write_lock:
            try:
                self._proc.stdin.write(data)          # type: ignore[union-attr]
                self._proc.stdin.flush()              # type: ignore[union-attr]
            except (BrokenPipeError, OSError) as e:
                raise McpError(f"the server closed its input: {e}") from e

    def request(self, method: str, params: Dict[str, Any],
                timeout: Optional[float] = None) -> Dict[str, Any]:
        rid = next(self._ids)
        box: "queue.Queue[dict]" = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[rid] = box
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            try:
                msg = box.get(timeout=timeout or self.timeout)
            except queue.Empty:
                raise McpError(f"{method} got no answer in {timeout or self.timeout:.0f}s"
                               + ("" if self.alive else f" ({self._dead_reason()})"))
        finally:
            with self._lock:
                self._pending.pop(rid, None)
        if "error" in msg:
            err = msg["error"] or {}
            raise McpError(f"{method}: {err.get('message') or err}")
        return msg.get("result") or {}

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("mcp %s stdout: %s", self.command, line[:200])
                continue                              # a server's stray log line
            if not isinstance(msg, dict):
                continue
            if "method" in msg and "id" in msg:      # a request FROM the server
                self._answer_server(msg)
                continue
            rid = msg.get("id")
            with self._lock:
                box = self._pending.get(rid)
            if box is not None:
                box.put(msg)

    def _answer_server(self, msg: Dict[str, Any]) -> None:
        """We offer no client capabilities (sampling, roots, elicitation); answer
        ping, refuse the rest, so the server never waits on us."""
        try:
            if msg.get("method") == "ping":
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
            else:
                self._send({"jsonrpc": "2.0", "id": msg["id"],
                            "error": {"code": -32601, "message": "not supported"}})
        except McpError:
            pass

    def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.stderr_tail = (self.stderr_tail + [line])[-20:]

    def _dead_reason(self) -> str:
        code = self._proc.poll() if self._proc else None
        tail = " | ".join(self.stderr_tail[-3:])
        return (f"the server exited (code {code})" if code is not None else "not started") \
            + (f": {tail[:300]}" if tail else "")

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()                       # type: ignore[union-attr]
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


class HttpClient(_Base):
    """A remote server over Streamable HTTP: POST JSON-RPC, answer as JSON or SSE."""

    def __init__(self, url: str, headers: Optional[Dict[str, str]] = None,
                 timeout: float = 30.0):
        super().__init__(timeout)
        self.url = url
        self._http = httpx.Client(timeout=timeout, headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **(headers or {}),
        })
        self._session: Optional[str] = None

    def start(self) -> None:
        try:
            self._handshake()
        except httpx.HTTPError as e:
            raise McpError(f"could not reach {self.url}: {e}") from e

    def _post(self, payload: Dict[str, Any], timeout: Optional[float]) -> httpx.Response:
        headers = {"MCP-Protocol-Version": PROTOCOL_VERSION}
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        resp = self._http.post(self.url, json=payload, headers=headers,
                               timeout=timeout or self.timeout)
        if resp.headers.get("mcp-session-id"):
            self._session = resp.headers["mcp-session-id"]
        if resp.status_code >= 400:
            raise McpError(f"{payload.get('method')}: HTTP {resp.status_code} "
                           f"{resp.text[:200]}")
        return resp

    def request(self, method: str, params: Dict[str, Any],
                timeout: Optional[float] = None) -> Dict[str, Any]:
        rid = next(self._ids)
        try:
            resp = self._post({"jsonrpc": "2.0", "id": rid, "method": method,
                               "params": params}, timeout)
        except httpx.HTTPError as e:
            raise McpError(f"{method}: {e}") from e
        msg = self._find_response(resp, rid)
        if msg is None:
            raise McpError(f"{method}: no response in the server's reply")
        if "error" in msg:
            err = msg["error"] or {}
            raise McpError(f"{method}: {err.get('message') or err}")
        return msg.get("result") or {}

    @staticmethod
    def _find_response(resp: httpx.Response, rid: int) -> Optional[Dict[str, Any]]:
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            for block in resp.text.split("\n\n"):
                data = "\n".join(l[5:].lstrip() for l in block.splitlines()
                                 if l.startswith("data:"))
                if not data:
                    continue
                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == rid:
                    return msg
            return None
        try:
            msg = resp.json()
        except ValueError:
            return None
        if isinstance(msg, list):                    # a batch: pick ours
            return next((m for m in msg if isinstance(m, dict) and m.get("id") == rid), None)
        return msg if isinstance(msg, dict) else None

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        try:
            self._post({"jsonrpc": "2.0", "method": method, "params": params}, None)
        except (McpError, httpx.HTTPError) as e:
            logger.debug("mcp notify %s: %s", method, e)

    def close(self) -> None:
        try:
            if self._session:
                self._http.delete(self.url, headers={"Mcp-Session-Id": self._session})
        except Exception:
            pass
        self._http.close()


def result_text(result: Dict[str, Any], limit: int = 6000) -> str:
    """A tools/call result as text for a model: text parts kept, images and other
    binary parts named (their bytes would only waste the context)."""
    parts: List[str] = []
    for item in result.get("content") or []:
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind == "image":
            parts.append(f"[image: {item.get('mimeType', 'image')}, "
                         f"{len(item.get('data') or '') * 3 // 4} bytes]")
        elif kind == "resource":
            res = item.get("resource") or {}
            parts.append(str(res.get("text") or f"[resource {res.get('uri', '')}]"))
        else:
            parts.append(f"[{kind}]")
    if result.get("structuredContent") and not parts:
        parts.append(json.dumps(result["structuredContent"])[:limit])
    text = "\n".join(parts).strip() or "(no output)"
    if result.get("isError"):
        text = "ERROR: " + text
    return text if len(text) <= limit else text[:limit] + f"\n… ({len(text) - limit} more chars)"
