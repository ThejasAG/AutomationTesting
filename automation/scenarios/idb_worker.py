"""A long-lived idb client: runs idb CLI commands in-process, one per request.

Run with the interpreter fb-idb is installed in (see idb_fast.py), never imported
by the platform. Every `idb ...` subprocess paid ~0.55-0.9s just to start Python
and import idb before doing anything; inside this process the same commands cost
what the companion takes -- measured on the iPhone 17 Pro sim:

    describe-all     CLI 0.57s   here 0.30s
    describe-point   CLI 0.55s   here 0.04s

Protocol (one JSON object per line):  {"argv": [...]}  ->  {"rc", "out", "err"}
Responses go to a private copy of the original stdout; fd 1 itself is pointed at
/dev/null so a stray C-level write cannot corrupt the stream. Exits on stdin EOF,
so it dies with the backend that spawned it.
"""
import asyncio
import contextlib
import io
import json
import os
import sys


def main() -> None:
    proto = os.fdopen(os.dup(1), "w", buffering=1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)

    from idb.cli.main import gen_main   # the slow import, paid once

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    proto.write(json.dumps({"ready": True}) + "\n")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            argv = json.loads(line)["argv"]
        except Exception as e:
            proto.write(json.dumps({"rc": 2, "out": "", "err": f"bad request: {e}"}) + "\n")
            continue
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = loop.run_until_complete(gen_main(list(argv)))
        except SystemExit as e:          # argparse and friends
            rc = e.code if isinstance(e.code, int) else 1
        except BaseException as e:       # never let one command take the worker down
            rc = 1
            err.write(f"{type(e).__name__}: {e}")
        proto.write(json.dumps({"rc": int(rc or 0), "out": out.getvalue(),
                                "err": err.getvalue()}) + "\n")


if __name__ == "__main__":
    main()
