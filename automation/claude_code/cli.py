"""The AI Agent from a terminal (and from the nightly launchd job).

  python -m automation.claude_code.cli start heal|fix_run|analyze|generate [--run-id ID]
                                       [--focus TEXT] [--mode delta|full] [--wait]
  python -m automation.claude_code.cli nightly [--force]   the scheduled run (honours the setting)
  python -m automation.claude_code.cli status              the last sessions
  python -m automation.claude_code.cli stop ID
  python -m automation.claude_code.cli continue ID   an interrupted session, where it stopped
  python -m automation.claude_code.cli approve ID | reject ID
  python -m automation.claude_code.cli recheck ID    the size check again (when only it failed)
  python -m automation.claude_code.cli schedule            install/remove the launchd job per settings

The same sessions as the AI Agent page: they show up there, and are approved there
or here.
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import List, Optional


def _wait(sid: str) -> int:
    from automation.claude_code import session
    last = ""
    while True:
        s = session.get(sid) or {}
        line = f"{s.get('status')}: {s.get('phase') or ''}"
        if line != last:
            print(line, flush=True)
            last = line
        if s.get("status") not in session.ACTIVE:
            return 0 if s.get("status") in ("review", "no_changes", "approved") else 1
        time.sleep(15)


def cmd_start(a) -> int:
    from automation.claude_code import session
    opts = {}
    if a.run_id:
        opts["run_ids"] = a.run_id
    if a.focus:
        opts["focus"] = a.focus
    if a.mode:
        opts["mode"] = a.mode
    try:
        sid = session.create(a.kind, trigger=a.trigger, options=opts, created_by="cli")
    except (session.Busy, session.Refused, ValueError) as e:
        print(f"not started: {e}")
        return 1
    print(f"session {sid} started ({a.kind}) — follow it on the AI Agent page")
    return _wait(sid) if a.wait else 0


def cmd_nightly(a) -> int:
    from automation.agentic import settings
    from automation.claude_code import session
    s = settings.get()
    if not s.get("nightly_enabled") and not a.force:
        print("nightly run is switched off in the AI Agent settings")
        return 0
    deadline = time.time() + 2 * 3600        # a session already running: wait for it
    while True:
        try:
            sid = session.create("nightly", trigger="nightly",
                                 options={"mode": s.get("nightly_mode") or "auto"}, created_by="schedule")
            break
        except session.Busy as e:
            if time.time() > deadline:
                print(f"nightly skipped: {e}")
                return 1
            print(f"waiting: {e}", flush=True)
            time.sleep(300)
        except (session.Refused, ValueError) as e:
            print(f"nightly not started: {e}")
            return 1
    print(f"nightly session {sid}", flush=True)
    return _wait(sid)


def cmd_status(_a) -> int:
    from automation.claude_code import session
    for s in session.list_sessions(15):
        print(f"{(s['created_at'] or '')[:16]}  {s['id'][:8]}  {s['kind']:8} {s['trigger']:8} "
              f"{s['status']:12} {s['branch'] or '':34} {(s['phase'] or '')[:60]}")
    return 0


def cmd_stop(a) -> int:
    from automation.claude_code import session
    print("stopping" if session.stop(a.id) else "not running")
    return 0


def cmd_continue(a) -> int:
    from automation.claude_code import session
    try:
        print(f"continuing session {session.continue_session(a.id, 'cli')}")
    except (session.Refused, session.Busy, ValueError) as e:
        print(f"refused: {e}")
        return 1
    return 0


def cmd_decide(a) -> int:
    from automation.claude_code import session
    try:
        out = session.approve(a.id, "cli") if a.cmd == "approve" else session.reject(a.id, "cli")
    except (session.Refused, ValueError) as e:
        print(f"refused: {e}")
        return 1
    print(out)
    if out.get("restart_needed"):
        print("Restart the backend to load it: ./stop.sh && ./start.sh (when nothing is running)")
    return 0


def cmd_recheck(a) -> int:
    from automation.claude_code import session
    try:
        out = session.recheck_size(a.id, "cli")
    except (session.Refused, ValueError) as e:
        print(f"refused: {e}")
        return 1
    print(f"size: {'ok' if out['ok'] else 'still too big'} — {out['detail']}")
    print("now waiting for approval" if out["ok"] else "raise 'Largest change it may propose' "
          "in the AI Agent settings, or reject it")
    return 0


def cmd_schedule(_a) -> int:
    from automation.agentic import settings
    from automation.claude_code import schedule
    print(schedule.sync(settings.get()))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    from automation.config import load_env
    load_env()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("kind", choices=["heal", "fix_run", "analyze", "generate", "nightly"])
    s.add_argument("--run-id", action="append", default=[])
    s.add_argument("--focus", default="")
    s.add_argument("--mode", choices=["delta", "full"])
    s.add_argument("--trigger", default="manual")
    s.add_argument("--wait", action="store_true")
    s.set_defaults(fn=cmd_start)
    s = sub.add_parser("nightly")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_nightly)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    s = sub.add_parser("stop")
    s.add_argument("id")
    s.set_defaults(fn=cmd_stop)
    s = sub.add_parser("continue")
    s.add_argument("id")
    s.set_defaults(fn=cmd_continue)
    for name in ("approve", "reject"):
        s = sub.add_parser(name)
        s.add_argument("id")
        s.set_defaults(fn=cmd_decide)
    s = sub.add_parser("recheck")
    s.add_argument("id")
    s.set_defaults(fn=cmd_recheck)
    sub.add_parser("schedule").set_defaults(fn=cmd_schedule)
    a = p.parse_args(argv)
    if getattr(a, "id", None) and len(a.id) < 36:          # accept a short id
        from automation.claude_code import session
        hit = [x["id"] for x in session.list_sessions(200) if x["id"].startswith(a.id)]
        if len(hit) == 1:
            a.id = hit[0]
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
