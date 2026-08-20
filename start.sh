#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Vya Test Platform — ONE-COMMAND LAUNCHER
#     ./start.sh
#  Brings the whole application up, idempotently, and keeps it up:
#     simulators → Appium → SUPERVISED backend (auto-restart) → dashboard.
#  Metros are kept alive by the backend's own watchdog. Stop with ./stop.sh.
# ─────────────────────────────────────────────────────────────────────────────
set -u
cd "$(dirname "$0")"
mkdir -p logs

# Ensure node (nvm) is on PATH — Appium & Metro shebangs are `#!/usr/bin/env node`,
# and a detached process without node on PATH dies with "env: node: not found".
NODEBIN="$(dirname "$(command -v node 2>/dev/null)" 2>/dev/null)"
[ ! -x "$NODEBIN/node" ] && NODEBIN="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | tail -1)"
[ -n "$NODEBIN" ] && export PATH="$NODEBIN:$PATH"
say(){  printf "\n\033[1;35m▸ %s\033[0m\n" "$*"; }
ok(){   printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
warn(){ printf "  \033[1;33m!\033[0m %s\n" "$*"; }

# ── 1. Simulators ────────────────────────────────────────────────────────────
say "Simulators"
SIMS=$(.venv/bin/python -c "import json;d=json.load(open('cross_app_config.json'))['devices'];print(' '.join(sorted(set(d.values()))))" 2>/dev/null)
[ -z "$SIMS" ] && SIMS="DA24A392-FF1B-4283-A5CE-CDDE0D000D21 D19D3EC7-5494-4B69-AC7B-3AB8AE0B4D1B"
open -a Simulator 2>/dev/null
for u in $SIMS; do xcrun simctl boot "$u" 2>/dev/null; done
sleep 3
for u in $SIMS; do
  xcrun simctl list devices booted 2>/dev/null | grep -q "$u" && ok "sim ${u:0:8}… booted" || warn "sim ${u:0:8}… NOT booted"
done

# ── 2. Appium ────────────────────────────────────────────────────────────────
say "Appium :4723"
if curl -s --max-time 3 http://127.0.0.1:4723/status 2>/dev/null | grep -q '"ready":true'; then
  ok "already running"
else
  APPIUM="$(command -v appium || echo appium)"
  NODE_OPTIONS="--max-old-space-size=4096" nohup "$APPIUM" --address 127.0.0.1 --port 4723 \
    --relaxed-security --log-timestamp > logs/appium.log 2>&1 &
  for _ in $(seq 1 30); do curl -s --max-time 2 http://127.0.0.1:4723/status 2>/dev/null | grep -q '"ready":true' && break; sleep 1; done
  curl -s --max-time 3 http://127.0.0.1:4723/status 2>/dev/null | grep -q '"ready":true' && ok "started" || warn "not ready (see logs/appium.log)"
fi

# ── 3. Backend (SUPERVISED — auto-restarts on crash) ─────────────────────────
say "Backend :8000  (supervised)"
[ -f logs/supervisor.pid ] && kill "$(cat logs/supervisor.pid)" 2>/dev/null
lsof -tiTCP:8000 -sTCP:LISTEN 2>/dev/null | xargs kill -9 2>/dev/null
sleep 1
nohup bash scripts/supervise_backend.sh > logs/supervisor.log 2>&1 &
for _ in $(seq 1 30); do curl -s --max-time 2 -o /dev/null http://127.0.0.1:8000/docs 2>/dev/null && break; sleep 1; done
[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8000/docs 2>/dev/null)" = "200" ] \
  && ok "healthy + auto-restart on" || warn "not healthy yet (see logs/backend.log)"

# ── 4. Dashboard ─────────────────────────────────────────────────────────────
say "Dashboard :5173"
if curl -s --max-time 3 -o /dev/null http://localhost:5173 2>/dev/null; then
  ok "already running"
else
  ( cd automation/dashboard && nohup npm run dev > "$OLDPWD/logs/dashboard.log" 2>&1 & echo $! > "$OLDPWD/logs/dashboard.pid" )
  for _ in $(seq 1 40); do curl -s --max-time 2 -o /dev/null http://localhost:5173 2>/dev/null && break; sleep 1; done
  curl -s --max-time 3 -o /dev/null http://localhost:5173 2>/dev/null && ok "started" || warn "still starting (see logs/dashboard.log)"
fi

echo
printf "\033[1;32m✅ Platform ready\033[0m   →   \033[1;4mhttp://localhost:5173\033[0m    (API: http://localhost:8000)\n"
printf "   stop: ./stop.sh   ·   status: ./status.sh   ·   logs: ./logs/\n\n"
