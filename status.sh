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
for p in 8081 8082 8083 8084; do chk "Metro          :$p" "curl -s --max-time 3 http://127.0.0.1:$p/status | grep -q running"; done
BOOTED=$(xcrun simctl list devices booted 2>/dev/null | grep -icE "iphone|ipad")
printf "  \033[1;36m● %s\033[0m simulators booted\n" "$BOOTED"
echo "--- staging backend (live-run dependency) ---"
chk "vya.xorstack.com  " "[ \"\$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 https://vya.xorstack.com/)\" = 200 ]"
