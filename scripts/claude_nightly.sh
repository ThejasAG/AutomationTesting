#!/bin/bash
# The nightly AI Agent run (launchd job com.vyapy.ai-agent.nightly, see
# automation/claude_code/schedule.py). Claude Code on the logged-in Claude plan —
# no API key. Starts the platform first if it is down (the Mac reboots between days).
# By hand: scripts/claude_nightly.sh --force
cd "$(dirname "$0")/.." || exit 1
export PATH="/usr/local/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
export PYTHONPATH="$PWD"
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN   # Claude Code must use the logged-in plan
echo "[$(date '+%F %H:%M:%S')] nightly AI Agent run"
if ! curl -s -o /dev/null -m 5 http://localhost:8000/docs; then
  echo "platform is down — ./start.sh"
  ./start.sh > logs/start.out 2>&1
fi
# Keep the Mac awake (idle + system sleep) for as long as the run takes.
exec /usr/bin/caffeinate -is .venv/bin/python -m automation.claude_code.cli nightly "$@"
