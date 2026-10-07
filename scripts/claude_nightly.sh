#!/bin/bash
# One nightly regression run driven by Claude Code on the Claude plan (no API key).
# Usage: scripts/claude_nightly.sh [--mode auto|delta|full] [--hours N]
# Details: scripts/claude_nightly.py. Logs: logs/claude_nightly/<date>/.
cd "$(dirname "$0")/.." || exit 1
export PATH="/usr/local/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
export PYTHONPATH="$PWD"
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN   # Claude Code must use the logged-in plan
# Keep the Mac awake (idle + system sleep) for as long as the run takes.
exec /usr/bin/caffeinate -is .venv/bin/python scripts/claude_nightly.py run "$@"
