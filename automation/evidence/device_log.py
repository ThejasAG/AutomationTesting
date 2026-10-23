"""The device's own log for the app, around the moment a step failed.

`xcrun simctl spawn <udid> log show --last <window>` filtered to the app's process.
A WINDOW, not the whole run: an unfiltered iOS log is tens of thousands of lines a
minute and burying the one useful line in it is the same as not collecting it.

Split deliberately into a pure filter (`interesting_lines`) and a thin shell wrapper
(`capture`), so the filtering — the part with the judgement in it — is testable
without a simulator.
"""

from __future__ import annotations

import re
import subprocess
from typing import List, Optional

#: Levels iOS marks on each line: Df debug, I info, Er error, Fa fault.
#: Errors and faults are the ones worth a human's attention.
_LEVEL = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+\s+(Er|Fa)\s")

#: Crash/abort language that can appear at any level.
_SEVERE = re.compile(
    r"Terminating app|uncaught exception|SIGABRT|SIGSEGV|EXC_BAD_ACCESS"
    r"|Fatal error|assertion failed|RCTFatal",
    re.I,
)

#: Present on every launch of every simulator build and never the cause.
_NOISE = re.compile(r"aps-environment|BackgroundTask|Snapshotting a view")


def interesting_lines(text: str, max_lines: int = 50) -> List[str]:
    """Error/fault lines plus anything that reads like a crash. Tail-biased."""
    keep = [
        ln.rstrip() for ln in (text or "").splitlines()
        if (_LEVEL.search(ln) or _SEVERE.search(ln)) and not _NOISE.search(ln)
    ]
    return keep[-max_lines:] if max_lines and len(keep) > max_lines else keep


def capture(udid: str, process_match: str, window: str = "60s",
            max_lines: int = 50, timeout: int = 25) -> List[str]:
    """Device log for *process_match* over the last *window*.

    Returns [] on any failure — a run must never fail because its evidence could
    not be gathered.
    """
    if not udid or not process_match:
        return []
    try:
        out = subprocess.run(
            ["xcrun", "simctl", "spawn", udid, "log", "show",
             "--last", window, "--style", "compact",
             "--predicate", f'processImagePath CONTAINS "{process_match}"'],
            capture_output=True, text=True, timeout=timeout,
        ).stdout
    except Exception:
        return []
    return interesting_lines(out, max_lines=max_lines)
