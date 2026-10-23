"""Where the AI Agent keeps things. One place, so the runner, the API and the tools agree."""
from __future__ import annotations

import os

# The user's checkout. Code in a session's worktree imports its own copy of this
# module, so the runner passes the real one down as AGENT_MAIN_ROOT.
_HERE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAIN_ROOT = os.environ.get("AGENT_MAIN_ROOT") or _HERE_ROOT

SESSIONS_DIR = os.path.join(MAIN_ROOT, "logs", "agent_sessions")
BASELINES_DIR = os.path.join(SESSIONS_DIR, "baselines")      # unit-test failures per commit
STATE_FILE = os.path.join(SESSIONS_DIR, "state.json")         # app heads seen by the last analysis
LOCK_FILE = os.path.join(SESSIONS_DIR, ".lock")               # one session at a time
# Outside the repo: pytest, grep, Vite and VS Code never wander into them.
WORKTREES_DIR = os.path.expanduser("~/.vya-platform/agent-worktrees")

PROMPTS_DIR = os.path.join(MAIN_ROOT, "automation", "claude_code", "prompts")
GUARD = os.path.join(MAIN_ROOT, "automation", "claude_code", "guard.py")
TOOLS_REL = "scripts/agent_tools.py"
TOOLS = os.path.join(MAIN_ROOT, TOOLS_REL)
PYTHON = os.path.join(MAIN_ROOT, ".venv", "bin", "python")

# Untracked things a worktree needs to run flows: linked, never copied.
WORKTREE_LINKS = (".env", "cross_app_config.json", "repos", ".venv")


def session_dir(session_id: str) -> str:
    return os.path.join(SESSIONS_DIR, session_id)
