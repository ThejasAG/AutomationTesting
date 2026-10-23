#!/usr/bin/env python3
"""PreToolUse hook for the AI Chat on Claude Code: an MCP server that drives the
simulators is refused while a test run is using them (the registry's own rule,
automation/mcp/registry.py McpRegistry.call), so a chat can never break a run.

Env: CHAT_DEVICE_SERVERS — comma list of the Claude Code MCP server names that
drive devices. Exit 2 + stderr refuses the call; exit 0 lets it run.
"""
import json
import os
import sys


def main() -> int:
    try:
        d = json.load(sys.stdin)
    except ValueError:
        return 0
    name = d.get("tool_name") or ""
    parts = name.split("__")
    devices = {s for s in (os.environ.get("CHAT_DEVICE_SERVERS") or "").split(",") if s}
    if len(parts) < 3 or parts[0] != "mcp" or parts[1] not in devices:
        return 0
    try:
        from automation.mcp.registry import active_run
        busy = active_run()
    except Exception:
        busy = ""
    if busy:
        print(f"REFUSED: test run {busy} is using the simulators. {parts[1]} drives the same "
              f"devices and would break it. Try again when the run has finished.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
