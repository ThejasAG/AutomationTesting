"""MCP servers in the platform: connect them in Settings, the AI Chat uses their tools.

Asked 2026-10-06 ("create an option to add MCPs in the application itself"),
choosing that the AI Chat can call the connected servers' tools.
"""
import inspect
import json
import sys
import textwrap
import types

import httpx
import pytest

from automation.mcp import client as mc
from automation.mcp import registry as mr

FAKE_SERVER = textwrap.dedent('''
    import json, sys
    def send(m): sys.stdout.write(json.dumps(m) + "\\n"); sys.stdout.flush()
    print("a stray log line, not JSON", flush=True)
    for line in sys.stdin:
        m = json.loads(line)
        if m.get("method") == "initialize":
            send({"jsonrpc": "2.0", "id": 99, "method": "ping"})       # server -> client
            send({"jsonrpc": "2.0", "id": m["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1"}}})
        elif m.get("method") == "tools/list":
            send({"jsonrpc": "2.0", "id": m["id"], "result": {"tools": [
                {"name": "echo", "description": "Echo text",
                 "inputSchema": {"$schema": "x", "type": "object",
                                 "properties": {"text": {"type": "string"}}}}]}})
        elif m.get("method") == "tools/call":
            a = m["params"]["arguments"]
            if m["params"]["name"] == "echo":
                send({"jsonrpc": "2.0", "id": m["id"], "result": {
                    "content": [{"type": "text", "text": "echo: " + a.get("text", "")}]}})
            else:
                send({"jsonrpc": "2.0", "id": m["id"],
                      "error": {"code": -32602, "message": "no such tool"}})
        elif m.get("id") == 99:
            pass                                    # our answer to the ping
''')


@pytest.fixture
def fake_server(tmp_path):
    path = tmp_path / "fake_mcp.py"
    path.write_text(FAKE_SERVER)
    return [str(path)]


def test_stdio_handshake_tools_and_a_call(fake_server):
    c = mc.StdioClient(sys.executable, fake_server, timeout=10)
    c.start()
    try:
        assert c.server_info["name"] == "fake"
        tools = c.list_tools()
        assert [t["name"] for t in tools] == ["echo"]
        out = c.call_tool("echo", {"text": "hi"})
        assert mc.result_text(out) == "echo: hi"
        with pytest.raises(mc.McpError, match="no such tool"):
            c.call_tool("nope", {})
    finally:
        c.close()
    assert not c.alive


def test_a_command_that_does_not_exist_says_so():
    c = mc.StdioClient("/no/such/mcp-binary", [], timeout=3)
    with pytest.raises(mc.McpError, match="could not start"):
        c.start()


def test_http_reply_as_server_sent_events_is_read():
    body = ('event: message\ndata: {"jsonrpc":"2.0","id":3,"result":{"ok":1}}\n\n')
    resp = httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
    assert mc.HttpClient._find_response(resp, 3) == {"jsonrpc": "2.0", "id": 3, "result": {"ok": 1}}
    plain = httpx.Response(200, json={"jsonrpc": "2.0", "id": 4, "result": {}})
    assert mc.HttpClient._find_response(plain, 4)["id"] == 4


def test_images_are_named_not_pasted_into_the_chat():
    out = mc.result_text({"content": [{"type": "text", "text": "done"},
                                      {"type": "image", "mimeType": "image/png", "data": "A" * 400}]})
    assert out.startswith("done") and "[image: image/png, 300 bytes]" in out


# -- the registry ------------------------------------------------------------------

def test_tool_names_are_valid_function_names():
    assert mr.tool_function_name("appium-mcp", "appium_screenshot") == "appium-mcp__appium_screenshot"
    assert mr.tool_function_name("my server", "do.it") == "my_server__do_it"
    assert len(mr.tool_function_name("x" * 50, "y" * 50)) == 64


def test_simulator_tools_are_refused_during_a_test_run(monkeypatch):
    monkeypatch.setattr(mr, "active_run", lambda: "1a2b3c4d")
    reg = mr.McpRegistry()
    monkeypatch.setattr(reg, "client", lambda cfg: pytest.fail("must not reach the device"))
    out = reg.call({"id": "s", "name": "appium-mcp", "drives_devices": True}, "appium_screenshot", {})
    assert out.startswith("REFUSED: test run 1a2b3c4d is using the simulators")


def test_other_servers_are_not_held_back_by_a_run(monkeypatch, fake_server):
    monkeypatch.setattr(mr, "active_run", lambda: "1a2b3c4d")
    reg = mr.McpRegistry()
    cfg = {"id": "s1", "name": "fake", "transport": "stdio", "command": sys.executable,
           "args": fake_server, "env": {}, "url": "", "headers": {}, "drives_devices": False}
    try:
        assert reg.call(cfg, "echo", {"text": "x"}) == "echo: x"
        monkeypatch.setattr(reg, "servers", lambda enabled_only=False: [cfg])
        functions, route = reg.chat_tools()
        assert functions[0]["function"]["name"] == "fake__echo"
        assert route["fake__echo"] == (cfg, "echo")
    finally:
        reg.stop_all()


def test_a_server_that_will_not_start_is_reported_not_fatal(monkeypatch):
    reg = mr.McpRegistry()
    cfg = {"id": "bad", "name": "broken", "transport": "stdio", "command": "/no/such/bin",
           "args": [], "env": {}, "url": "", "headers": {}, "drives_devices": False}
    monkeypatch.setattr(reg, "servers", lambda enabled_only=False: [cfg])
    functions, route = reg.chat_tools()
    assert functions == [] and reg.unavailable and reg.unavailable[0].startswith("broken:")


# -- the API --------------------------------------------------------------------------

def test_secrets_are_masked_and_kept_when_posted_back():
    from automation.api.v1.routers import mcp as api
    pub = api._public({"env": {"API_KEY": "s3cret"}, "headers": {"Authorization": "Bearer t"}})
    assert pub["env"] == {"API_KEY": api.MASK} and pub["headers"] == {"Authorization": api.MASK}
    assert api._merge_secret({"API_KEY": api.MASK, "NEW": "v"}, {"API_KEY": "s3cret"}) == \
        {"API_KEY": "s3cret", "NEW": "v"}


def test_only_admins_can_add_change_remove_or_test():
    from automation.api.v1.routers import mcp as api
    for fn in (api.create_server, api.update_server, api.delete_server, api.test_server):
        assert "user=_admin" in inspect.getsource(fn), fn.__name__
    assert 'require_role(["admin"])' in inspect.getsource(api)


def test_the_router_is_mounted():
    import automation.api.main as main
    assert "v1_router.include_router(mcp_router)" in inspect.getsource(main)


# -- the AI Chat ------------------------------------------------------------------------

def test_the_chat_calls_a_tool_and_answers_from_it(monkeypatch):
    from automation.intelligence import chat
    from automation.mcp import registry as reg_mod

    cfg = {"id": "s1", "name": "appium-mcp", "drives_devices": True}
    reg = reg_mod.McpRegistry()
    monkeypatch.setattr(reg, "chat_tools", lambda: ([{"type": "function", "function": {
        "name": "appium-mcp__appium_screenshot", "description": "[appium-mcp] shot",
        "parameters": {"$schema": "x", "type": "object", "properties": {}}}}],
        {"appium-mcp__appium_screenshot": (cfg, "appium_screenshot")}))
    calls = []
    monkeypatch.setattr(reg, "call", lambda c, t, a: calls.append((c["name"], t)) or "saved shot.png")
    monkeypatch.setattr(reg_mod, "registry", reg)

    replies = iter([
        {"choices": [{"message": {"content": "", "tool_calls": [{"id": "c1", "type": "function",
            "function": {"name": "appium-mcp__appium_screenshot", "arguments": "{}"}}]}}]},
        {"choices": [{"message": {"content": "Here is the iPad: saved shot.png"}}]},
    ])
    sent = []

    def post(url, headers=None, timeout=None, json=None):
        sent.append(json)
        r = types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: next(replies))
        return r
    monkeypatch.setattr(chat._requests, "post", post)
    out = "".join(chat._tool_chat("sys", "screenshot the iPad"))
    assert calls == [("appium-mcp", "appium_screenshot")]
    assert "🔧 `appium-mcp · appium_screenshot` ✓" in out
    assert out.endswith("Here is the iPad: saved shot.png")
    assert "$schema" not in json.dumps(sent[0]["tools"])
    assert sent[1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "saved shot.png"}


def test_without_servers_the_chat_is_unchanged(monkeypatch):
    from automation.intelligence import chat
    from automation.mcp import registry as reg_mod
    reg = reg_mod.McpRegistry()
    monkeypatch.setattr(reg, "chat_tools", lambda: ([], {}))
    monkeypatch.setattr(reg_mod, "registry", reg)
    monkeypatch.setattr(chat, "_generic_provider_stream", lambda s, p: iter(["plain answer"]))
    assert "".join(chat._tool_chat("sys", "hello")) == "plain answer"
