#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Vya Test Platform — EXECUTION AGENT launcher
#     ./start-agent.sh
#  Starts the Execution Agent: the worker that pulls QUEUED jobs (PR test runs)
#  from the backend and drives the device. Without it, queued jobs sit forever.
#  (The waiter/kitchen demos run in-process and do NOT need this.)
#  Long-lived — keep it running. Stop with ./stop-agent.sh (or ./stop.sh --all).
# ─────────────────────────────────────────────────────────────────────────────
set -u
cd "$(dirname "$0")"
mkdir -p logs

# node (nvm) on PATH — Appium/simctl helpers the agent shells out to expect it.
NODEBIN="$(dirname "$(command -v node 2>/dev/null)" 2>/dev/null)"
[ ! -x "$NODEBIN/node" ] && NODEBIN="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | tail -1)"
[ -n "$NODEBIN" ] && export PATH="$NODEBIN:$PATH"

say(){  printf "\n\033[1;35m▸ %s\033[0m\n" "$*"; }
ok(){   printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
warn(){ printf "  \033[1;33m!\033[0m %s\n" "$*"; }

say "Execution Agent"

# Backend must be up first — the agent registers against it on start.
if [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8000/docs 2>/dev/null)" != "200" ]; then
  warn "backend not healthy on :8000 — run ./start.sh first, then ./start-agent.sh"
  exit 1
fi

# Already running? (pid file live, or a main.py process alive)
if [ -f logs/agent.pid ] && kill -0 "$(cat logs/agent.pid)" 2>/dev/null; then
  ok "already running (pid $(cat logs/agent.pid)) — logs/agent.log"
  exit 0
fi
if pgrep -f "automation/agent/main.py" >/dev/null 2>&1; then
  ok "already running — logs/agent.log"
  exit 0
fi

# Launch detached; it registers then polls /jobs/poll and drains the queue FIFO.
PYTHONPATH=. nohup .venv/bin/python automation/agent/main.py > logs/agent.log 2>&1 &
echo $! > logs/agent.pid

# Confirm it registered (agent logs "Registered successfully. Agent ID: …").
for _ in $(seq 1 20); do
  grep -q "Registered successfully" logs/agent.log 2>/dev/null && break
  kill -0 "$(cat logs/agent.pid)" 2>/dev/null || break
  sleep 0.5
done
if grep -q "Registered successfully" logs/agent.log 2>/dev/null; then
  ok "registered + polling (pid $(cat logs/agent.pid)) — draining the queue one job at a time"
  ok "logs: ./logs/agent.log   ·   stop: ./stop-agent.sh"
else
  warn "started (pid $(cat logs/agent.pid)) but no 'Registered' yet — check logs/agent.log"
fi
