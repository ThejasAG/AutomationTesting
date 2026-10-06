#!/usr/bin/env bash
# Stop the Execution Agent (started by ./start-agent.sh). Queued jobs will then
# sit until an agent is started again. Safe to run even if it isn't running.
cd "$(dirname "$0")"
echo "Stopping Execution Agent…"
if [ -f logs/agent.pid ]; then kill "$(cat logs/agent.pid)" 2>/dev/null; rm -f logs/agent.pid; fi
pkill -f "automation/agent/main.py" 2>/dev/null
echo "  ✓ agent stopped"
