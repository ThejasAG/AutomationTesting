#!/usr/bin/env bash
# Stop the platform cleanly. By default leaves the sims + Appium + Metros up
# (they're slow to restart); pass --all to stop those too.
cd "$(dirname "$0")"
echo "Stopping platform…"

# Stop the supervisor FIRST so it doesn't restart the backend we're about to kill.
if [ -f logs/supervisor.pid ]; then kill "$(cat logs/supervisor.pid)" 2>/dev/null; rm -f logs/supervisor.pid; fi
if [ -f logs/backend.pid ];    then kill "$(cat logs/backend.pid)"    2>/dev/null; rm -f logs/backend.pid; fi
lsof -tiTCP:8000 -sTCP:LISTEN 2>/dev/null | xargs kill -9 2>/dev/null
echo "  ✓ backend + supervisor stopped"

if [ -f logs/dashboard.pid ]; then kill "$(cat logs/dashboard.pid)" 2>/dev/null; rm -f logs/dashboard.pid; fi
lsof -tiTCP:5173 -sTCP:LISTEN 2>/dev/null | xargs kill -9 2>/dev/null
echo "  ✓ dashboard stopped"

# Execution Agent (if it was started via ./start-agent.sh)
if [ -f logs/agent.pid ]; then kill "$(cat logs/agent.pid)" 2>/dev/null; rm -f logs/agent.pid; fi
pkill -f "automation/agent/main.py" 2>/dev/null && echo "  ✓ execution agent stopped"

if [ "${1:-}" = "--all" ]; then
  pkill -f "appium --address 127.0.0.1 --port 4723" 2>/dev/null && echo "  ✓ appium stopped"
  pkill -f "react-native start" 2>/dev/null && echo "  ✓ metros stopped"
  echo "  (simulators left booted — 'xcrun simctl shutdown all' to stop them)"
fi
echo "Done."
