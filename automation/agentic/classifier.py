"""Rule-based failure sorter: decides what kind of failure a run had, for free.

The patterns are the messages the flow runner actually writes (sampled from the
scenario_results table), so the common cases never need Claude:

  INFRA    the rig or the server failed, not the app (Appium/WDA, staging 5xx,
           backend restart, missing credentials)
  APP_BUG  the app itself crashed / red-boxed
  LOCATOR  the element the step wanted is not on screen
  TIMING   the step waited and the screen never changed (often flaky)
  UNKNOWN  nothing matched — Claude decides
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

_RULES: List[Tuple[str, str]] = [
    ("INFRA", r"could not proxy command|socket hang up|webdriveragent is not initiali[sz]ed|"
              r"session is either terminated|session does not exist|invalidsessionid|"
              r"econnrefused|connection refused|bad gateway|\b50[234]\b|"
              r"backend restarted|no (waiter|kitchen|consumer) password configured|"
              r"device .* is busy|could not sign in|"
              # Metro down / app not installed: the runner labels the first one
              # "APP BUG", but it is the packager, not the app.
              r"no bundle url present|deployment blocked|unable to start webdriveragent|"
              # A Python error in the platform itself is our bug, not the app's.
              r"attributeerror|typeerror|nameerror|keyerror|runner process exited"),
    ("APP_BUG", r"app bug|rctfatal|app crashed|native crash|red ?box|process no longer running"),
    ("LOCATOR", r"no element matches|may need a testid|button not found|is not on screen|"
                r"is not on the dialog|could not be reached|method not found"),
    ("TIMING", r"timed out after|step hung|did not appear within|never opened|did not open|"
               r"polled ~|still disabled|nothing was selected|none shows as selected"),
]


def _failed_rows(rows: List[Any]) -> List[Any]:
    return [r for r in rows if (getattr(r, "status", "") or "").upper() == "FAIL"]


def evidence_text(rows: List[Any], run: Any = None) -> str:
    parts: List[str] = []
    if run is not None and getattr(run, "error_message", None):
        parts.append(run.error_message)
    for r in _failed_rows(rows):
        parts.append(r.error or "")
        parts.extend(str(n) for n in (r.reasons or [])[-6:])
    return "\n".join(p for p in parts if p)


def classify(run: Any, rows: List[Any]) -> Dict[str, Any]:
    """{"category", "matched", "failed_segment", "failed_step", "summary"}"""
    text = evidence_text(rows, run)
    low = text.lower()
    category, matched = "UNKNOWN", ""
    if run is not None and getattr(run, "crash_detected", False):
        category, matched = "APP_BUG", "crash_detected"
    else:
        for cat, pat in _RULES:
            m = re.search(pat, low)
            if m:
                category, matched = cat, m.group(0)
                break

    failed = _failed_rows(rows)
    seg = failed[0] if failed else None
    step = ""
    m = re.search(r"failed at step: '([^']+)'", text)
    if m:
        step = m.group(1)
    summary = ((seg.error if seg else None) or getattr(run, "error_message", "") or "")[:400]
    return {
        "category": category,
        "matched": matched,
        "failed_segment": getattr(seg, "scenario_num", None) if seg else None,
        "failed_segment_name": getattr(seg, "scenario_name", None) if seg else None,
        "failed_step": step,
        "summary": summary,
    }


def first_policy(category: str) -> Optional[str]:
    """What the orchestrator tries before asking Claude: 'retry' (whole flow, after
    the rig recovers), 'resume' (from the failed segment), or None."""
    return {"INFRA": "retry", "TIMING": "resume"}.get(category)
