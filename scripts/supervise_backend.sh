#!/usr/bin/env bash
# Keep the platform backend alive — if it ever exits (crash, OOM), restart it.
# This is what makes the platform "application-ready": a backend blip during a demo
# self-heals in ~2s instead of needing a manual restart.
cd "$(dirname "$0")/.."
mkdir -p logs
echo $$ > logs/supervisor.pid

# The backend spawns node tools (Appium via preflight, Metro via the watchdog), whose
# shebang needs node on PATH — a detached supervisor may lack it. Add nvm's node bin.
NODEBIN="$(dirname "$(command -v node 2>/dev/null)" 2>/dev/null)"
[ ! -x "$NODEBIN/node" ] && NODEBIN="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | tail -1)"
[ -n "$NODEBIN" ] && export PATH="$NODEBIN:$PATH"

# On stop (./stop.sh sends TERM), take the running backend down with us.
trap 'kill "$(cat logs/backend.pid 2>/dev/null)" 2>/dev/null; rm -f logs/supervisor.pid; exit 0' TERM INT

while true; do
  echo "[$(date '+%F %H:%M:%S')] starting backend"
  .venv/bin/python -m uvicorn automation.api.main:app --host 0.0.0.0 --port 8000 &
  child=$!
  echo "$child" > logs/backend.pid
  wait "$child"
  code=$?
  echo "[$(date '+%F %H:%M:%S')] backend exited (code $code) — restarting in 2s"
  sleep 2
done
