#!/usr/bin/env bash
# One-glance health of the whole platform.
cd "$(dirname "$0")"
chk(){ # name  test-cmd
  if eval "$2" >/dev/null 2>&1; then printf "  \033[1;32m● up  \033[0m %s\n" "$1"
  else printf "  \033[1;31m● down\033[0m %s\n" "$1"; fi
}
echo "Platform status:"
chk "Backend        :8000" "[ \"\$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 http://127.0.0.1:8000/docs)\" = 200 ]"
chk "Dashboard      :5173" "curl -s --max-time 4 -o /dev/null http://localhost:5173"
chk "Appium         :4723" "curl -s --max-time 4 http://127.0.0.1:4723/status | grep -q ready"
chk "Supervisor         " "[ -f logs/supervisor.pid ] && kill -0 \$(cat logs/supervisor.pid)"
# Only the Metros something is actually responsible for starting: a port declared in
# project-environments.json AND owned by a project row (the watchdog keys off
# app_bundle_id). A declared-but-unowned port is listed as unclaimed, not "down" --
# nothing starts it, so reporting it red every boot trains you to ignore the line.
eval "$(.venv/bin/python scripts/metro_ports.py)"
for p in $METRO_OWNED;     do chk "Metro          :$p" "curl -s --max-time 3 http://127.0.0.1:$p/status | grep -q running"; done
for p in $METRO_UNCLAIMED; do printf "  \033[1;33m● n/a \033[0m Metro          :%s (declared, no project)\n" "$p"; done
BOOTED=$(xcrun simctl list devices booted 2>/dev/null | grep -icE "iphone|ipad")
printf "  \033[1;36m● %s\033[0m simulators booted\n" "$BOOTED"
echo "--- staging backend (live-run dependency) ---"
chk "vya.xorstack.com  " "[ \"\$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 https://vya.xorstack.com/)\" = 200 ]"
