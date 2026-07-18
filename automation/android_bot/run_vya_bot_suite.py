"""Launch the Vya-agentic-BOT's scenarios and stream results into the platform.

    python -m automation.android_bot.run_vya_bot_suite [all|CW1,PAY8|1-10]

What it does:
  1. Creates a run row on the platform (bot_type="android") and prints its
     dashboard URL / run_id.
  2. Sets PLATFORM_SCENARIO_CALLBACK so the bot's reporter POSTs every scenario
     result to /api/v1/runs/<run_id>/scenario-result (already patched in the
     bot's shared/scenario_reporter.py).
  3. Launches the bot's iOS manager with the requested selection.

The bot itself still needs its own runtime (idb, the iOS devices/simulators its
manager is configured for, the apps installed). This script only connects it to
the platform and starts it — it does not replace the bot's device setup.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime

from automation.database.config import SessionLocal
from automation.database import database

# The separate Vya-agentic-BOT checkout on this machine.
BOT_DIR = os.getenv(
    "VYA_BOT_DIR",
    "/Users/roops/Desktop/Vya/Vya-agentic-Bot/vya-agent-testing-main",
)
IOS_MANAGER = os.path.join(BOT_DIR, "ios", "ios_agent_manager.py")
PLATFORM_BASE = os.getenv("PLATFORM_BASE_URL", "http://localhost:8000")
CONSUMER_PROJECT_ID = "bd34a47c-c099-4d36-ac61-810edfff31ca"


def main():
    selection = sys.argv[1] if len(sys.argv) > 1 else "all"

    if not os.path.exists(IOS_MANAGER):
        print(f"Bot iOS manager not found at {IOS_MANAGER}\n"
              f"Set VYA_BOT_DIR to the Vya-agentic-BOT checkout.")
        return 1

    run_id = str(uuid.uuid4())
    with SessionLocal() as db:
        database.insert_test_run(db, {
            "id": run_id, "project_id": CONSUMER_PROJECT_ID,
            "test_suite": "Vya-agentic-BOT (iOS cross-app)",
            "test_name": f"Scenarios: {selection}",
            "status": "running", "job_state": "running",
            "started_at": datetime.utcnow(), "created_at": datetime.utcnow(),
            "device_name": "vya-bot", "platform": "iOS", "bot_type": "android",
        })

    callback = f"{PLATFORM_BASE}/api/v1/runs/{run_id}/scenario-result"
    print("=" * 66)
    print(f"  Platform run : {run_id}")
    print(f"  Dashboard    : http://localhost:5173/runs/{run_id}  (Scenarios tab)")
    print(f"  Callback     : {callback}")
    print(f"  Selection    : {selection}")
    print("=" * 66)

    env = dict(os.environ, PLATFORM_SCENARIO_CALLBACK=callback)
    # BOT_SECRET (if the platform requires it) is passed straight through.

    try:
        proc = subprocess.run(
            [sys.executable, IOS_MANAGER, selection], cwd=os.path.join(BOT_DIR, "ios"),
            env=env,
        )
        rc = proc.returncode
    except KeyboardInterrupt:
        rc = 130

    # Finalise the run status from whatever scenarios reported.
    from automation.database.models import ScenarioResult, TestRun
    with SessionLocal() as db:
        run = db.query(TestRun).filter_by(id=run_id).first()
        rows = db.query(ScenarioResult).filter_by(run_id=run_id).all()
        if run is not None:
            run.status = "failed" if any(r.status == "FAIL" for r in rows) else \
                         ("passed" if rows else "failed")
            run.job_state = run.status
            run.completed_at = datetime.utcnow()
            db.commit()
        print(f"\nRun {run_id[:8]}: {len(rows)} scenario(s) reported → "
              f"{run.status if run else '?'}")
    return rc


if __name__ == "__main__":
    sys.exit(main() or 0)
