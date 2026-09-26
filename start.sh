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
# Resolved for THIS Mac, never hardcoded. A UDID identifies a simulator inside
# one Mac's CoreSimulator and means nothing on another, so the previous fallback
# ("boot these two UDIDs") silently booted nothing on every machine but the one
# they came from — `2>/dev/null` hid the reason, leaving only "NOT booted".
#
# Order of preference:
#   1. cross_app_config.json, when it exists  (gitignored — per-machine by design)
#   2. SIM_NAMES, or the platform's seeded device names, resolved by NAME here
#   3. whatever iPhone this Mac actually has
say "Simulators"
open -a Simulator 2>/dev/null

SIM_NAMES="${SIM_NAMES:-iPhone 16 Pro|iPad Pro 11-inch (M4)}"
SIMS=$(.venv/bin/python - "$SIM_NAMES" <<'PY' 2>/dev/null
import json, os, subprocess, sys

def available():
    """(name, udid, state) for every iPhone/iPad simulator on THIS Mac."""
    try:
        out = subprocess.run(["xcrun", "simctl", "list", "devices", "available", "-j"],
                             capture_output=True, text=True, timeout=20).stdout
        data = json.loads(out).get("devices") or {}
    except Exception:
        return []
    return [(d.get("name", ""), d["udid"], d.get("state", ""))
            for devs in data.values() for d in devs
            if d.get("udid") and d.get("name", "").startswith(("iPhone", "iPad"))]

devices = available()
by_name = {}
for name, udid, _state in devices:
    by_name.setdefault(name, udid)          # first wins: stable across runs

# 0. The platform's own resolver: the role devices, swapped for THIS Mac's
#    simulators and only ones Appium can drive on this Xcode (an iOS 18 sim
#    boots fine under Xcode 26 but WebDriverAgent cannot run on it). Booting
#    anything else just burns CPU next to the sims the runs actually use.
picked = []
try:
    sys.path.insert(0, os.getcwd())
    from automation.scenarios.cross_app_config import load_config
    picked = sorted(set(load_config()["devices"].values()))
except Exception:
    picked = []

# 1. This machine's own config, when someone has written one.
if not picked:
  try:
    with open("cross_app_config.json") as f:
        cfg = set(json.load(f).get("devices", {}).values())
    here = {u for _n, u, _s in devices}
    picked = sorted(cfg & here)             # only ones that exist HERE
  except Exception:
    pass

# 2. Resolve the wanted names against what this Mac has.
if not picked:
    for want in (sys.argv[1] if len(sys.argv) > 1 else "").split("|"):
        want = want.strip()
        if want and want in by_name:
            picked.append(by_name[want])

# 3. Nothing matched: boot SOMETHING rather than nothing. Prefer one that is
#    already booted, then the highest iPhone model number -- reverse-alphabetical
#    is not "newest" ("iPhone SE" sorts above "iPhone 16 Pro").
if not picked:
    def rank(entry):
        name, _udid, state = entry
        import re
        m = re.search(r"iPhone (\d+)", name)
        return (state != "Booted", -(int(m.group(1)) if m else 0), name)
    iphones = sorted((d for d in devices if d[0].startswith("iPhone")), key=rank)
    if iphones:
        picked = [iphones[0][1]]

print(" ".join(picked))
PY
)

if [ -z "$SIMS" ]; then
  warn "no iOS simulators found on this Mac — install one in Xcode ▸ Settings ▸ Platforms"
  warn "list what you have: xcrun simctl list devices available"
else
  for u in $SIMS; do
    # Keep stderr: a boot that fails must say why, not vanish.
    berr=$(xcrun simctl boot "$u" 2>&1)
    case "$berr" in
      *"Unable to boot device in current state: Booted"*) : ;;   # already up
      "") : ;;
      *) warn "boot $u: $(printf '%s' "$berr" | head -1)" ;;
    esac
  done
  sleep 3
  for u in $SIMS; do
    nm=$(xcrun simctl list devices available 2>/dev/null | grep "$u" | sed 's/ (.*//;s/^ *//')
    if xcrun simctl list devices booted 2>/dev/null | grep -q "$u"; then
      ok "${nm:-$u} booted"
    else
      warn "${nm:-$u} NOT booted"
    fi
  done
fi

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
