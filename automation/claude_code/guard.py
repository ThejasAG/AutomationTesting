#!/usr/bin/env python3
"""PreToolUse hook for AI Agent sessions: the second lock on what Claude may do.

The session's permission rules (runner.session_settings) are the first lock; this
hook checks every tool call again with real paths (symlinks resolved), so a
relative path, a `..` or the worktree's links to repos/ and .env cannot widen them.

Claude Code runs it before each tool call with the call as JSON on stdin. Exit 2
blocks the call and the message on stderr is shown to Claude; exit 0 lets it run.
Standalone on purpose (no project imports): a session's edits can't break it.

Environment (set by the runner):
  AGENT_WORKTREE      the session's git worktree (its working directory)
  AGENT_SESSION_DIR   logs/agent_sessions/<id> — reports, notes, screenshots
  AGENT_MAIN_ROOT     the user's checkout (never edited)
"""
import json
import os
import re
import sys

EDITABLE = ("automation", "tests", "scripts")
# The agent's own guardrails and the tool that records its runs.
PROTECTED = (os.path.join("automation", "claude_code"), ".claude",
             os.path.join("scripts", "agent_tools.py"))
# In the session folder: the run ledger and transcript are evidence, written by the
# tools and the runner only.
LEDGER = ("runs.jsonl", "runs", "transcript.jsonl", "result.json", "gates.json")
SECRETS = (".env", "cross_app_config.json")
_SAFE_ARGS = r"(\s+[^;&|`$<>\\\n\r]*)?$"
BASH_ALLOWED = (
    re.compile(r"^\.venv/bin/python scripts/agent_tools\.py" + _SAFE_ARGS),
    re.compile(r"^git (diff|status|log|show)" + _SAFE_ARGS),
)
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def _real(path: str, cwd: str) -> str:
    if not path:
        return ""
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    return os.path.realpath(path)


def _under(path: str, root: str) -> bool:
    root = os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def check(tool: str, inp: dict, cwd: str, env: dict) -> str:
    """'' when allowed, else the reason it is blocked."""
    wt = env.get("AGENT_WORKTREE") or cwd
    session = env.get("AGENT_SESSION_DIR") or ""
    main = env.get("AGENT_MAIN_ROOT") or ""
    secrets = {os.path.realpath(os.path.join(r, s)) for r in (wt, main) if r for s in SECRETS}

    if tool in ("WebFetch", "WebSearch"):
        return "no web access in agent sessions"

    if tool in EDIT_TOOLS:
        p = _real(inp.get("file_path") or inp.get("notebook_path") or "", cwd)
        if p in secrets:
            return "secrets files are never edited"
        if session and _under(p, session):
            if any(_under(p, os.path.join(session, x)) for x in LEDGER):
                return "the run ledger and transcript are written by the tools only"
            return ""
        if any(_under(p, os.path.join(wt, d)) for d in PROTECTED):
            return ("the agent's own guardrails (automation/claude_code/, .claude/, "
                    "scripts/agent_tools.py) are not editable in a session; describe the change "
                    "in the report instead")
        if any(_under(p, os.path.join(wt, d)) for d in EDITABLE):
            return ""
        if main and _under(p, os.path.join(main, "repos")):
            return ("the app checkouts are never modified; write an app patch as a .patch file "
                    "in the session folder instead")
        return ("you may edit only automation/, tests/ and scripts/ inside your worktree "
                f"({wt}) and files in the session folder")

    if tool in ("Read", "Grep", "Glob", "NotebookRead"):
        p = _real(inp.get("file_path") or inp.get("path") or "", cwd)
        if p and p in secrets:
            return "secrets files (.env, cross_app_config.json) are not readable in agent sessions"
        return ""

    if tool == "Bash":
        cmd = (inp.get("command") or "").strip()
        if any(rx.match(cmd) for rx in BASH_ALLOWED):
            return ""
        return ("only `.venv/bin/python scripts/agent_tools.py <subcommand> …` and read-only "
                "`git diff|status|log|show` may run — one command, no pipes, `&&`, `;`, "
                "redirects or `cd`. Run `.venv/bin/python scripts/agent_tools.py --help` for "
                "what the tools can do.")
    return ""


def main() -> int:
    try:
        d = json.load(sys.stdin)
    except ValueError:
        print("guard: unreadable hook input", file=sys.stderr)
        return 2
    why = check(d.get("tool_name") or "", d.get("tool_input") or {}, d.get("cwd") or os.getcwd(),
                dict(os.environ))
    if why:
        print(f"Blocked by the agent guard: {why}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
