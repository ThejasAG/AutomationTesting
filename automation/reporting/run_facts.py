"""What a run really did, from its segments' own notes, for the reports.

The user, 2026-11-05, on the Reports page's exported report of the END TO END run: "most
of the areas is not filled". Its Detail column showed only errors (empty for a pass), and
the AI summary called the run suspect: "took 0.0 seconds and the device information shows
n/a". The run row's duration is that of the LAST resume only (segments carried over from
earlier runs on the same booking took their time there), and the row's device string is
'C:D03927 B:6767DB'. The segments know better: each one's launch_time, the device its
session ran on ('session ready on D03927A2'), the steps that passed, the calculations
checked and the sums that did not match.
"""
from __future__ import annotations

import json
import re
import subprocess
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional

from automation.scenarios.invoice_check import MISMATCH_PREFIX, is_printed_payments_gap

_SESSION = re.compile(r"session ready on ([0-9A-F]{8})")
_STEP = re.compile(r"^\[ok\] (?P<step>.+?) — ")
_BOOKKEEPING = ("session ready", "carried over", "logged in", "already logged in")


def _reasons(s: Any) -> List[str]:
    rs = getattr(s, "reasons", None) if not isinstance(s, dict) else s.get("reasons")
    if isinstance(rs, str):
        try:
            rs = json.loads(rs)
        except ValueError:
            rs = [rs]
    return [str(r) for r in (rs or [])]


def _get(s: Any, key: str):
    return s.get(key) if isinstance(s, dict) else getattr(s, key, None)


def segment_seconds(scenarios: Iterable[Any]) -> float:
    """Σ of the segments' own run times (launch_time, seconds)."""
    return round(sum(float(_get(s, "launch_time") or 0) for s in scenarios), 1)


def effective_duration_ms(run_ms: Optional[int], scenarios: Iterable[Any]) -> int:
    """The longer of the run row's duration and its segments' total: a resumed run's row
    counts only its last resume."""
    return int(max(run_ms or 0, segment_seconds(scenarios) * 1000))


def carried_over(scenarios: Iterable[Any]) -> int:
    """Segments that passed in an earlier run of the same booking and were not re-run."""
    return sum(1 for s in scenarios
               if any(r.startswith("[ok] carried over") for r in _reasons(s)))


def _role(name: str) -> str:
    who = (name or "").split(":")[0].lower()
    for key, role in (("participant", "participant"), ("host", "host"), ("kitchen", "kitchen"),
                      ("waiter", "waiter"), ("consumer", "consumer")):
        if key in who:
            return role
    return "device"


@lru_cache(maxsize=1)
def _sim_names() -> Dict[str, str]:
    """udid prefix (8) -> simulator name, from simctl (empty when it cannot be asked)."""
    try:
        out = subprocess.run(["xcrun", "simctl", "list", "devices", "-j"], capture_output=True,
                             text=True, timeout=15).stdout
        devs = [d for ds in json.loads(out or "{}").get("devices", {}).values() for d in ds]
    except Exception:
        return {}
    return {d["udid"][:8]: d["name"] for d in devs if d.get("udid") and d.get("name")}


def devices(scenarios: Iterable[Any], fallback: str = "") -> str:
    """'host: iPhone 17 Pro (D03927A2) · participant: iPhone 17 (BB34587D) · waiter +
    kitchen: iPad Pro 11-inch (M5) (6767DB23)' -- the device each role's segments ran on."""
    by_udid: Dict[str, List[str]] = {}
    for s in scenarios:
        for r in _reasons(s):
            m = _SESSION.search(r)
            if m:
                roles = by_udid.setdefault(m.group(1), [])
                role = _role(_get(s, "scenario_name") or _get(s, "name") or "")
                if role not in roles:
                    roles.append(role)
                break
    if not by_udid:
        return fallback or "—"
    names = _sim_names()
    return " · ".join(f"{' + '.join(roles)}: {names.get(u, 'simulator')} ({u})"
                      for u, roles in by_udid.items())


def segment_detail(s: Any) -> str:
    """One line on what a segment did: '9 steps passed · 3 calculations checked · 1 sum
    did not match: …', or its error when it failed."""
    from automation.reporting.step_tables import tables_of
    from automation.scenarios.run_issues import issues_of
    reasons = _reasons(s)
    status = (_get(s, "status") or "").upper()
    if any(r.startswith("[ok] carried over") for r in reasons) and not any(
            _STEP.match(r) and not r.startswith("[ok] carried over") for r in reasons):
        return "passed in an earlier run of this booking (carried over)"
    steps = [m.group("step") for r in reasons for m in [_STEP.match(r)]
             if m and not m.group("step").startswith(_BOOKKEEPING)]
    bits = [f"{len(steps)} step{'s' if len(steps) != 1 else ''} passed"]
    try:
        tables = tables_of(reasons)
    except Exception:
        tables = []
    if tables:
        bits.append(f"{len(tables)} calculation{'s' if len(tables) != 1 else ''} checked")
    red = list(dict.fromkeys(r[len(MISMATCH_PREFIX):] for r in reasons
                             if r.startswith(MISMATCH_PREFIX) and not is_printed_payments_gap(r)))
    if red:
        bits.append(f"{len(red)} sum{'s' if len(red) != 1 else ''} did not match: {red[0][:160]}")
    yellow = issues_of(reasons)
    if yellow:
        bits.append(f"{len(yellow)} app issue{'s' if len(yellow) != 1 else ''} noted (yellow)")
    if status not in ("PASS", "PASSED") and _get(s, "error"):
        return f"{str(_get(s, 'error'))[:240]} ({', '.join(bits)})"
    return " · ".join(bits)


def segment_steps(s: Any, limit: int = 10) -> List[str]:
    """The steps a segment passed, by name, for the AI summary."""
    reasons = _reasons(s)
    return [m.group("step") for r in reasons for m in [_STEP.match(r)]
            if m and not m.group("step").startswith(_BOOKKEEPING)][:limit]
