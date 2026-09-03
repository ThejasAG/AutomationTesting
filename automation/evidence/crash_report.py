"""Crash reports for the app under test, from the last few minutes.

TestRun already carries a `crash_detected` flag and nothing ever filled it. macOS
writes simulator app crashes to ~/Library/Logs/DiagnosticReports as .ips files: a
JSON header line, then a JSON body. We want three things a human reads first —
what crashed, why it was terminated, and the top of the faulting stack — not the
whole file, which runs to hundreds of KB of every thread's registers.

`summarise` is a pure function over file text so it is testable without crashing
anything; `recent_for_app` is the thin filesystem wrapper.
"""

from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional

_DIR = os.path.expanduser("~/Library/Logs/DiagnosticReports")


def summarise(text: str, max_frames: int = 8) -> Optional[Dict]:
    """Pull the readable summary out of one .ips file, or None if it isn't one."""
    if not text or not text.strip().startswith("{"):
        return None
    lines = text.splitlines()
    try:
        head = json.loads(lines[0])
    except Exception:
        return None

    body: Dict = {}
    rest = "\n".join(lines[1:]).strip()
    if rest.startswith("{"):
        try:
            body = json.loads(rest)
        except Exception:
            body = {}

    # The faulting thread is the one flagged `triggered`; fall back to the first.
    threads = body.get("threads") or []
    faulting = next((t for t in threads if t.get("triggered")), threads[0] if threads else {})
    images = body.get("usedImages") or []
    frames: List[str] = []
    for f in (faulting.get("frames") or [])[:max_frames]:
        idx = f.get("imageIndex")
        name = ""
        if isinstance(idx, int) and 0 <= idx < len(images):
            name = images[idx].get("name") or ""
        frames.append(f"{name} {f.get('symbol') or hex(f.get('imageOffset', 0))}".strip())

    return {
        "app": head.get("app_name") or body.get("procName"),
        "version": head.get("app_version"),
        "timestamp": head.get("timestamp"),
        # termination.indicator is the human-readable "why", e.g. an uncaught
        # NSException's reason — usually the single most useful field in the file.
        "reason": ((body.get("termination") or {}).get("indicator")
                   or (body.get("exception") or {}).get("type")),
        "signal": (body.get("exception") or {}).get("signal"),
        "frames": frames,
    }


def recent_for_app(app_match: str, within_seconds: int = 600,
                   directory: str = _DIR, limit: int = 3) -> List[Dict]:
    """Summaries of crashes matching *app_match* written in the last *within_seconds*.

    Time-bounded so a run is never blamed for a crash from last week. Returns []
    on any error — missing evidence must not fail a run.
    """
    if not app_match or not os.path.isdir(directory):
        return []
    cutoff = time.time() - within_seconds
    out: List[Dict] = []
    try:
        names = sorted(os.listdir(directory), reverse=True)
    except OSError:
        return []
    for name in names:
        if not name.endswith(".ips") or app_match.lower() not in name.lower():
            continue
        path = os.path.join(directory, name)
        try:
            if os.path.getmtime(path) < cutoff:
                continue
            with open(path, "r", errors="replace") as f:
                info = summarise(f.read())
        except OSError:
            continue
        if info:
            info["file"] = name
            out.append(info)
        if len(out) >= limit:
            break
    return out
