"""The app's own JS console, reduced to the lines that explain a failure.

On 2026-09-02 a run failed for hours with "element not found". The actual cause was
sitting in the Metro log the whole time:

    Invariant Violation: Module AppRegistry is not a registered callable module

Nothing surfaced it, because nothing looked. This reads the Metro log an app is
served from and keeps only the lines that diagnose something — a bounded tail, not
a dump: 300k of log attached to a run is the same "app crashed" problem with more
scrolling.

Pure function over text, so it is testable without a device or a running bundler.
"""

from __future__ import annotations

import os
import re
from typing import List, Optional

#: Lines worth showing a human. Ordered most- to least-specific; the first three are
#: the ones that actually ended investigations, the rest are supporting context.
_SIGNAL = re.compile(
    r"""(
        Invariant\ Violation            # AppRegistry not registered, getEnforcing, …
      | Unable\ to\ resolve\ module     # a missing/undeclared dependency
      | (?<![A-Za-z])ERROR(?![A-Za-z])  # RN's own console.error marker
      | Failed\ to\ (?:load|fetch)
      | (?:Unhandled|Possible\ unhandled)\ promise
      | \bTypeError\b | \bReferenceError\b | \bSyntaxError\b
    )""",
    re.VERBOSE,
)

#: Noise that matches the above but never explains anything. Kept explicit rather
#: than tightening _SIGNAL, so it is obvious WHAT was suppressed and why.
_NOISE = re.compile(
    r"aps-environment"          # push entitlement, absent in every simulator build
    r"|Each child in a list"    # React key warning, present on every screen
    r"|Failed prop type"        # noisy in this app and never the cause
    r"|Require cycle:"
)

# ANSI colour from Metro's pretty printer — unreadable once stored as JSON.
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def extract_js_errors(text: str, max_lines: int = 50) -> List[str]:
    """The last *max_lines* diagnostic lines in a Metro log, newest last.

    Tail rather than head: a failure is explained by what happened just before it,
    and these logs run to hundreds of thousands of lines over a session.
    """
    keep: List[str] = []
    for raw in (text or "").splitlines():
        line = _ANSI.sub("", raw).rstrip()
        if not line.strip():
            continue
        if _SIGNAL.search(line) and not _NOISE.search(line):
            keep.append(line.strip())
    return keep[-max_lines:] if max_lines and len(keep) > max_lines else keep


def read_js_console(log_path: Optional[str], max_lines: int = 50,
                    tail_bytes: int = 2_000_000) -> List[str]:
    """`extract_js_errors` over a log file, reading only its tail.

    A long-running bundler log is far too big to load whole; 2 MB covers well beyond
    any single run. Returns [] rather than raising — missing evidence must never be
    the thing that fails a run.
    """
    if not log_path or not os.path.isfile(log_path):
        return []
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()          # drop the partial first line
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    return extract_js_errors(text, max_lines=max_lines)
