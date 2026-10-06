"""
scenario_reporter.py  —  results collector for the Vya-agentic-BOT.

Drop this file next to multi_agent_manager.py in the bot checkout. It is the
single source of truth for scenario results and does three jobs:

  1. Accumulates per-scenario PASS/FAIL (merging the Consumer and Business
     halves of a cross-app scenario with the "both must pass" rule).
  2. Persists everything to scenario_history.json in the exact schema the
     vya_*_report.py generators read  ({"current": ..., "history": [...]}).
  3. Streams every result LIVE to the Vyapy platform when the environment
     variable PLATFORM_SCENARIO_CALLBACK is set (so the dashboard's Scenarios
     tab updates in real time). The local HTML reports keep working unchanged.

The bot calls exactly three things:
    scenario_reporter.add_result(scenario_num=, scenario_name=, role=, status=,
                                 reason=, launch_time=, screenshot_path=,
                                 screen_load_time=)   # screen_load_time optional
    scenario_reporter.print_summary()
    scenario_reporter.save_run_to_history()

Environment:
    PLATFORM_SCENARIO_CALLBACK   full URL of /api/v1/runs/<run_id>/scenario-result
                                 (set automatically by run_vya_bot_suite.py).
    BOT_SECRET                   optional; sent as X-Bot-Secret if the platform
                                 requires it.
    SCENARIO_HISTORY_PATH        optional override for scenario_history.json.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

try:
    import requests
except Exception:  # requests is optional — local reporting still works without it
    requests = None

# ── Paths ────────────────────────────────────────────────────────────────────
# Default: scenario_history.json lives next to this file (i.e. in the bot dir).
HISTORY_PATH = Path(os.getenv(
    "SCENARIO_HISTORY_PATH",
    str(Path(__file__).resolve().parent / "scenario_history.json"),
))

# ── Platform callback (live streaming into the dashboard) ────────────────────
CALLBACK_URL = os.getenv("PLATFORM_SCENARIO_CALLBACK", "").strip()
BOT_SECRET = os.getenv("BOT_SECRET", "").strip()

TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"

# ── In-memory state for the current run ──────────────────────────────────────
_lock = threading.RLock()
_results: "dict[str, dict]" = {}   # scenario_num -> merged record
_order: "list[str]" = []           # preserve first-seen order


def _now() -> str:
    return datetime.now().strftime(TIMESTAMP_FMT)


def _norm_role(role: str) -> str:
    r = (role or "").strip().lower()
    if r.startswith("consumer"):
        return "consumer"
    if r.startswith("business"):
        return "business"
    return "consumer"  # default the phone side to consumer


def _blank_record(num: str, name: str) -> dict:
    return {
        "num": str(num),
        "name": name or "Unknown",
        "status": "PASS",
        "consumer_status": "N/A",
        "business_status": "N/A",
        "reasons": [],
        "consumer_reasons": [],
        "business_reasons": [],
        "error": "",
        "launch_time": None,
        "screenshot_path": None,
        "timestamp": _now(),
    }


def _apply_side_status(current: str, incoming: str) -> str:
    """FAIL is sticky; otherwise the latest non-N/A status wins."""
    inc = (incoming or "").upper()
    if current == "FAIL" or inc == "FAIL":
        return "FAIL"
    if inc in ("PASS", "SKIP", "SKIPPED"):
        return "PASS" if inc == "PASS" else current if current != "N/A" else "SKIP"
    return current


def _recompute(record: dict) -> None:
    """Both-must-pass gate + rebuild the merged error string."""
    cs, bs = record["consumer_status"], record["business_status"]
    record["status"] = "FAIL" if (cs == "FAIL" or bs == "FAIL") else "PASS"
    fail_reasons = [
        r for r in (record["consumer_reasons"] + record["business_reasons"] + record["reasons"])
        if r and r.strip().lower() != "completed"
    ]
    # de-dupe while preserving order
    record["error"] = "; ".join(dict.fromkeys(fail_reasons)) if record["status"] == "FAIL" else ""


def add_result(scenario_num, scenario_name, role, status, reason=None,
               launch_time=None, screenshot_path=None, screen_load_time=None):
    """Record one (scenario, role) result. Called many times per scenario.

    Merges into a single record per scenario_num and streams it to the platform.
    """
    num = str(scenario_num)
    side = _norm_role(role)
    status = (status or "").upper()

    with _lock:
        if num not in _results:
            _results[num] = _blank_record(num, scenario_name)
            _order.append(num)
        rec = _results[num]

        if scenario_name and rec["name"] in ("Unknown", ""):
            rec["name"] = scenario_name

        # Per-side status (FAIL sticky) + reasons
        key_status = f"{side}_status"
        key_reasons = f"{side}_reasons"
        rec[key_status] = _apply_side_status(rec[key_status], status)
        if reason:
            for chunk in (reason if isinstance(reason, (list, tuple)) else [reason]):
                if chunk and chunk not in rec[key_reasons]:
                    rec[key_reasons].append(chunk)

        # Metadata
        lt = launch_time if launch_time is not None else screen_load_time
        if lt is not None:
            rec["launch_time"] = lt
        # Prefer a failure screenshot; otherwise keep the first one seen.
        if screenshot_path and (rec["screenshot_path"] is None or status == "FAIL"):
            rec["screenshot_path"] = screenshot_path
        rec["timestamp"] = _now()

        _recompute(rec)
        snapshot = dict(rec)

    # Live stream to the platform (outside the lock — network can be slow).
    _post_to_platform(snapshot, side, status, reason, launch_time)


def _post_to_platform(rec: dict, side: str, incoming_status: str,
                      reason, launch_time) -> None:
    if not CALLBACK_URL or requests is None:
        return
    headers = {"Content-Type": "application/json"}
    if BOT_SECRET:
        headers["X-Bot-Secret"] = BOT_SECRET
    reasons = (rec["consumer_reasons"] if side == "consumer" else rec["business_reasons"])
    body = {
        "scenario_num": rec["num"],
        "scenario_name": rec["name"],
        "status": incoming_status or rec["status"],
        "role": "Consumer" if side == "consumer" else "Business",
        "consumer_status": rec["consumer_status"],
        "business_status": rec["business_status"],
        "error": rec["error"] or (reason if incoming_status == "FAIL" else None),
        "reasons": list(reasons),
        "launch_time": launch_time,
    }
    try:
        requests.post(CALLBACK_URL, json=body, headers=headers, timeout=10)
    except Exception as e:  # never let a reporting failure break the run
        print(f"[Report] Could not POST result to platform: {e}")


def print_summary() -> None:
    """Print a compact PASS/FAIL table for the current run."""
    with _lock:
        records = [_results[n] for n in _order]

    total = len(records)
    passed = sum(1 for r in records if r["status"] == "PASS")
    failed = total - passed

    print("\n" + "=" * 60)
    print(f"  SCENARIO SUMMARY  —  {passed}/{total} passed, {failed} failed")
    print("=" * 60)
    for r in records:
        mark = "PASS" if r["status"] == "PASS" else "FAIL"
        sides = []
        if r["consumer_status"] != "N/A":
            sides.append(f"C:{r['consumer_status']}")
        if r["business_status"] != "N/A":
            sides.append(f"B:{r['business_status']}")
        side_str = f" ({', '.join(sides)})" if sides else ""
        print(f"  [{mark}] {r['num']:>6}  {r['name']}{side_str}")
        if r["status"] == "FAIL" and r["error"]:
            print(f"          -> {r['error'][:140]}")
    print("=" * 60 + "\n")

    if CALLBACK_URL:
        print(f"[Report] Streamed live to platform: {CALLBACK_URL}")


def _load_history() -> dict:
    if not HISTORY_PATH.exists():
        return {"current": None, "history": []}
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"current": None, "history": []}


def save_run_to_history() -> None:
    """Flush the current run's records into scenario_history.json.

    The vya_*_report.py generators read this file. We append every record of
    this run to `history` and clear `current` so the reports pick them up.
    """
    with _lock:
        records = [dict(_results[n]) for n in _order]

    if not records:
        return

    data = _load_history()
    history = data.get("history") or []
    # Move any lingering `current` into history first.
    if data.get("current"):
        history.insert(0, data["current"])
    # Newest first.
    history[:0] = records

    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(
        json.dumps({"current": None, "history": history}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[Report] Saved {len(records)} scenario(s) -> {HISTORY_PATH}")


def reset() -> None:
    """Clear in-memory state (start of a fresh run)."""
    with _lock:
        _results.clear()
        _order.clear()
