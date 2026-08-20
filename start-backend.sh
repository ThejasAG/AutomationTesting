#!/usr/bin/env bash
# Start the platform backend STABLY.
#
# Do NOT use `uvicorn --reload` for normal use: it watches the whole project and
# the platform writes files under it constantly (the SQLite DB on every request,
# screenshots + builds under repos/, the graphify cache, logs). Every write would
# restart the server mid-run — killing scenarios and dropping the Appium session.
#
# This runs with NO reload → the server stays up through runs.
#   ./start-backend.sh
#
# For active code development you DO want reload, but scoped to the code only so
# runtime file writes don't trigger it:
#   uvicorn automation.api.main:app --host 0.0.0.0 --port 8000 \
#     --reload --reload-dir automation --reload-exclude 'automation/dashboard/*'
# (The DB, repos/, graphify-out/ and logs/ live at the project root, outside
#  automation/, so they no longer trigger restarts.)

cd "$(dirname "$0")"

# Free the port if something is already bound (a wedged old server).
PIDS=$(lsof -tiTCP:8000 -sTCP:LISTEN 2>/dev/null)
[ -n "$PIDS" ] && kill -9 $PIDS 2>/dev/null && sleep 1

exec .venv/bin/python -m uvicorn automation.api.main:app --host 0.0.0.0 --port 8000
