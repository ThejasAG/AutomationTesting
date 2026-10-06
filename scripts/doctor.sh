#!/usr/bin/env bash
# Check every prerequisite this platform needs, and say how to fix each miss.
#
# Written after a day where the platform failed for four unrelated environment
# reasons in a row — a missing iOS platform, a dead bundler, a full disk, and an
# unset AI key — each of which surfaced as a confusing test failure hours later.
# Run this FIRST on a new machine, and again whenever a run fails oddly.
#
#   bash scripts/doctor.sh
#
# Exit code 0 = ready to run. 1 = something below must be fixed first.

cd "$(dirname "$0")/.." || exit 1
FAIL=0
pass() { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n     → %s\n" "$1" "$2"; }
bad()  { printf "  \033[31m✗\033[0m %s\n     → %s\n" "$1" "$2"; FAIL=1; }

echo "== Host tools =="
for t in node yarn xcrun pod redis-cli; do
  if command -v "$t" >/dev/null 2>&1; then pass "$t"; else
    case $t in
      yarn) bad "yarn missing" "npm i -g yarn  (the app repos are Yarn workspaces; npm corrupts them)";;
      pod)  bad "cocoapods missing" "sudo gem install cocoapods";;
      redis-cli) bad "redis missing" "brew install redis && brew services start redis";;
      *)    bad "$t missing" "install $t";;
    esac
  fi
done
command -v idb >/dev/null 2>&1 || [ -x /usr/local/bin/idb ] \
  && pass "idb" || bad "idb missing" "brew tap facebook/fb && brew install idb-companion && pipx install fb-idb"

echo "== Xcode =="
if xcodebuild -showsdks 2>/dev/null | grep -qi "iphonesimulator"; then
  pass "iOS simulator SDK present"
else
  bad "no iOS simulator SDK" "xcodebuild -downloadPlatform iOS   (deleting a runtime to save disk BREAKS WebDriverAgent)"
fi
# Any iOS runtime in the supported RANGE will do — we do not pin a version.
# ios-support.json declares the range; this only reports what is installed.
MIN_IOS=$(sed -n 's/.*"min_ios"[[:space:]]*:[[:space:]]*"\([0-9.]*\)".*/\1/p' ios-support.json 2>/dev/null)
MAX_IOS=$(sed -n 's/.*"max_ios"[[:space:]]*:[[:space:]]*"\([0-9.]*\)".*/\1/p' ios-support.json 2>/dev/null)
MIN_IOS=${MIN_IOS:-16.0}; MAX_IOS=${MAX_IOS:-26.99}

# Major version is all that matters for the range test.
VERS=$(xcrun simctl runtime list 2>/dev/null \
       | sed -n 's/^iOS \([0-9][0-9.]*\).*/\1/p' | sort -u)
IN_RANGE=$(for v in $VERS; do
    awk -v v="${v%%.*}" -v lo="${MIN_IOS%%.*}" -v hi="${MAX_IOS%%.*}" \
        'BEGIN{ if (v>=lo && v<=hi) print "y" }'
  done | grep -c y)

if [ "${IN_RANGE:-0}" -gt 0 ]; then
  pass "iOS runtime(s) in supported range ${MIN_IOS}-${MAX_IOS}: $(echo $VERS | tr '\n' ' ')"
elif [ -n "$VERS" ]; then
  bad "installed iOS runtime(s) [$(echo $VERS | tr '\n' ' ')] are outside the supported ${MIN_IOS}-${MAX_IOS}" \
      "install any iOS ${MIN_IOS%%.*}-${MAX_IOS%%.*} runtime (Xcode > Settings > Components), or raise max_ios in ios-support.json if a newer iOS is now verified"
else
  bad "no iOS runtime" "Xcode > Settings > Components, or xcodebuild -downloadPlatform iOS"
fi

echo "== Resources =="
FREE=$(df -g / | tail -1 | awk '{print $4}')
[ "${FREE:-0}" -ge 20 ] && pass "disk ${FREE}G free" \
  || warn "only ${FREE}G free" "uv cache prune; xcrun simctl delete unavailable; rm -rf ~/Library/Developer/Xcode/DerivedData/*"
UNUSED=$(top -l 1 -n 0 2>/dev/null | awk '/PhysMem/{print $(NF-1)}' | tr -dc '0-9')
[ -n "$UNUSED" ] && { [ "$UNUSED" -ge 500 ] && pass "memory ${UNUSED}M unused" \
  || warn "only ${UNUSED}M memory unused" "close browsers/Teams and unused Metro bundlers — this is what kills Metro and makes steps take 120s"; }

echo "== Platform config =="
[ -f .env ] && pass ".env present" || bad ".env missing" "cp .env.example .env, then fill it in"
if [ -f .env ]; then
  grep -q "^DATABASE_URL=sqlite:////" .env && pass "DATABASE_URL is an ABSOLUTE path outside the repo" \
    || bad "DATABASE_URL not set outside the repo" "DATABASE_URL=sqlite:////Users/<you>/.vya-platform/platform.db  — .db files are in git history and a checkout WILL overwrite an in-repo database"
  grep -q "^LLM_PROVIDER_TYPE=" .env && grep -q "^OPENAI_API_KEY=" .env \
    && pass "LLM provider configured" \
    || bad "no LLM key" "LLM_PROVIDER_TYPE=groq + OPENAI_API_KEY=<groq key>. WITHOUT THESE the AI silently returns a canned fake analysis instead of erroring"
  grep -q "^GITHUB_TOKEN=" .env && pass "GITHUB_TOKEN set" || warn "no GITHUB_TOKEN" "PR listing and PR-QA comments will not work"
fi
[ -d .venv ] && pass "python venv" || bad "no .venv" "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"

echo "== Services =="
chk() { curl -s --max-time 3 "$2" >/dev/null 2>&1 && pass "$1"; }
lsof -tiTCP:8000 -sTCP:LISTEN >/dev/null 2>&1 && pass "backend :8000" \
  || bad "backend down" "nohup bash scripts/supervise_backend.sh > logs/supervisor.out 2>&1 &"
lsof -tiTCP:5173 -sTCP:LISTEN >/dev/null 2>&1 && pass "dashboard :5173" \
  || warn "dashboard down" "cd automation/dashboard && npm run dev"
curl -s --max-time 3 http://127.0.0.1:4723/status 2>/dev/null | grep -q '"ready":true' \
  && pass "appium :4723" || bad "appium down" "appium &"
redis-cli ping >/dev/null 2>&1 && pass "redis" || warn "redis not responding" "brew services start redis"
for p in 8081 8082 8083 8084; do
  curl -s --max-time 2 "http://127.0.0.1:$p/status" 2>/dev/null | grep -q running \
    && pass "metro :$p" \
    || warn "metro :$p down" "cd repos/<id> && npx react-native start --port $p   (one per app repo)"
done

echo "== Devices =="
BOOTED=$(xcrun simctl list devices booted 2>/dev/null | grep -c Booted)
[ "${BOOTED:-0}" -gt 0 ] && pass "$BOOTED simulator(s) booted" \
  || bad "no simulator booted" "xcrun simctl boot <udid>"
if [ -f cross_app_config.json ]; then
  for u in $(grep -oE '"[A-F0-9-]{36}"' cross_app_config.json | tr -d '"' | sort -u); do
    xcrun simctl list devices booted 2>/dev/null | grep -q "$u" \
      && pass "flow device ${u:0:8} booted" \
      || bad "flow device ${u:0:8} NOT booted" "xcrun simctl boot $u   (flows use PINNED devices, not whatever is booted)"
  done
fi

# == Project preflight ========================================================
# Everything above checks the MACHINE. A project has its own failure modes that
# a machine check cannot see -- unapplied patches, a Podfile.lock that disagrees
# with Pods/Manifest.lock, a dependency importing a framework this SDK dropped.
# `doctor.sh <repo>` runs those too, so one command covers both.
if [ -n "${1:-}" ]; then
  echo
  echo "== Project: $1 =="
  if [ -x .venv/bin/python ]; then
    .venv/bin/python -m automation.projects.macos_environment "$1" || FAIL=1
  else
    warn "cannot run the project preflight" "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  fi
fi

echo
[ "$FAIL" -eq 0 ] && echo "READY — every required check passed." \
  || echo "NOT READY — fix the ✗ items above, then re-run."
[ -z "${1:-}" ] && echo "Tip: ./scripts/doctor.sh repos/<project-id>   also checks that project."
exit $FAIL
