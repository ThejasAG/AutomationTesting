#!/usr/bin/env bash
# Free storage by deleting failure screenshots from the DB. Safe: this ONLY removes
# the images — every run's pass/fail, failing step, and reason stay intact.
#   ./clear-screenshots.sh              → delete ALL stored screenshots
#   ./clear-screenshots.sh --keep 20    → keep screenshots for the 20 most recent runs, drop older
#   ./clear-screenshots.sh --days 7     → drop screenshots from runs older than 7 days
cd "$(dirname "$0")"

MODE="all"; N=0
case "${1:-}" in
  --keep) MODE="keep"; N="${2:-20}" ;;
  --days) MODE="days"; N="${2:-7}"  ;;
  "" )    MODE="all" ;;
  * ) echo "usage: $0 [--keep N | --days N]"; exit 1 ;;
esac

.venv/bin/python - "$MODE" "$N" <<'PY'
import sqlite3, sys
# Use the CONFIGURED database. Hardcoding the repo-root file meant this cleared
# screenshots from a database the platform may not be using, while the real one grew
# unbounded.
sys.path.insert(0, ".")
from automation.config import database_url
_url = database_url()
if not _url.startswith("sqlite:"):
    sys.exit(f"clear-screenshots only supports sqlite; DATABASE_URL is {_url}")
mode, n = sys.argv[1], int(sys.argv[2] or 0)
c = sqlite3.connect(_url.removeprefix("sqlite:///"))
before = c.execute("select coalesce(sum(length(screenshot)),0) from scenario_results "
                   "where screenshot is not null").fetchone()[0]
if mode == "all":
    c.execute("update scenario_results set screenshot=NULL where screenshot is not null")
elif mode == "keep":
    # keep screenshots only for the N most-recent runs (by created_at); clear the rest
    c.execute("""update scenario_results set screenshot=NULL where run_id not in (
                   select id from test_runs order by created_at desc limit ?)""", (n,))
elif mode == "days":
    c.execute("""update scenario_results set screenshot=NULL where run_id in (
                   select id from test_runs where created_at < datetime('now', ?))""",
              (f'-{n} days',))
c.commit()
after = c.execute("select coalesce(sum(length(screenshot)),0) from scenario_results "
                  "where screenshot is not null").fetchone()[0]
c.execute("VACUUM")   # actually shrink the DB file on disk
print(f"freed {round((before-after)/1024/1024,2)} MB of screenshots "
      f"(mode={mode}{', '+str(n) if mode!='all' else ''}); "
      f"{round(after/1024/1024,2)} MB of screenshots remain")
PY
