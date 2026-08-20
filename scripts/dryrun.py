"""Trigger a cross-app flow and wait (keeping the daemon thread alive) until it ends.
Usage: PYTHONPATH=<root> python scripts/dryrun.py <flow_id> <env>"""
import sys, time
from automation.scenarios.cross_app_flows import start_flow_run
from automation.database.config import SessionLocal
from automation.database.models import TestRun

flow = sys.argv[1] if len(sys.argv) > 1 else "flow_book_demo"
env = sys.argv[2] if len(sys.argv) > 2 else "staging"
rid = start_flow_run(flow, env=env)
print(f"RUN_ID {rid}", flush=True)

TERMINAL = {"passed", "failed", "stopped", "cancelled", "error"}
t0 = time.time()
while True:
    time.sleep(8)
    with SessionLocal() as db:
        r = db.query(TestRun).filter_by(id=rid).first()
        st = r.status if r else "?"
    print(f"[{int(time.time()-t0)}s] status={st}", flush=True)
    if st in TERMINAL or time.time() - t0 > 420:
        print(f"DONE status={st} after {int(time.time()-t0)}s", flush=True)
        break
