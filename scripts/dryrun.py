"""Trigger a cross-app flow and wait (keeping the daemon thread alive) until it ends.
Usage: PYTHONPATH=<root> python scripts/dryrun.py <flow_id> <env>

The flow runs on a daemon thread IN THIS PROCESS, so exiting early kills it
mid-step and leaves its run stuck at 'running'. This used to give up after 420s
-- shorter than a slow flow -- and did exactly that. It now waits for the flow
thread itself; on the (long) safety cap or Ctrl-C it asks the flow to stop and
waits for it to unwind, so the run always ends with a real status.
"""
import sys, threading, time
from automation.scenarios.cross_app_flows import start_flow_run, request_flow_stop
from automation.database.config import SessionLocal
from automation.database.models import TestRun

flow = sys.argv[1] if len(sys.argv) > 1 else "flow_book_demo"
env = sys.argv[2] if len(sys.argv) > 2 else "staging"
MAX_WAIT = 2 * 60 * 60
rid = start_flow_run(flow, env=env)
print(f"RUN_ID {rid}", flush=True)

worker = next((t for t in threading.enumerate() if rid[:8] in t.name), None)
t0 = time.time()


def status() -> str:
    with SessionLocal() as db:
        r = db.query(TestRun).filter_by(id=rid).first()
        return r.status if r else "?"


try:
    while worker and worker.is_alive():
        worker.join(8)
        print(f"[{int(time.time()-t0)}s] status={status()}", flush=True)
        if time.time() - t0 > MAX_WAIT:
            print("safety cap reached — asking the flow to stop", flush=True)
            break
except KeyboardInterrupt:
    print("interrupted — asking the flow to stop", flush=True)
if worker and worker.is_alive():
    request_flow_stop(rid)
    worker.join(600)
print(f"DONE status={status()} after {int(time.time()-t0)}s", flush=True)
