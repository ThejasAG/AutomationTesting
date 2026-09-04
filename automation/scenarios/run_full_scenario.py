"""Run the Vyapy full cross-app scenario — as far as each engine allows.

    python -m automation.scenarios.run_full_scenario --device <UDID>

WHAT THIS ACTUALLY DOES
  Reads vyapy_full_cross_app.yaml and executes the CONSUMER steps live on the
  iOS simulator via the platform's scenario runner. At each cross-app sync point
  it PAUSES and prints what the Business app must do, because the iOS runner
  cannot drive both apps in sync — that is Tanish's Android bot's job (Option A).

  Business steps are printed, not executed: their labels are still `TBD`.

  This is deliberately honest: it runs the Consumer journey and narrates the
  Business handoffs, rather than pretending a single engine drives both apps.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import yaml

from automation.projects.repository import repository_manager as rm
from automation.scenarios import run_records  # noqa: F401 — backend run bookkeeping
from automation.scenarios.service import scenario_events, ScenarioRequest

SCENARIO_FILE = os.path.join(os.path.dirname(__file__), "vyapy_full_cross_app.yaml")
CONSUMER_PROJECT_ID = "bd34a47c-c099-4d36-ac61-810edfff31ca"


def _clean(step: str) -> str:
    return step.split("#")[0].strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="DA24A392-FF1B-4283-A5CE-CDDE0D000D21",
                    help="iOS simulator UDID for the Consumer app")
    ap.add_argument("--save", action="store_true",
                    help="save the recorded Consumer pytest into e2e/")
    args = ap.parse_args()

    doc = yaml.safe_load(open(SCENARIO_FILE))
    consumer = doc["apps"]["consumer"]
    repo_path = rm.get_repo_path(CONSUMER_PROJECT_ID)

    print(f"\n=== {doc['name']} ===")
    print("Engine: iOS scenario runner (Consumer only). Business steps are")
    print("narrated at each sync point — run Option A (Android bot) for a real")
    print("synchronized cross-app pass.\n")

    # Split the interleaved flow into a runnable Consumer sequence + the Business
    # handoffs to narrate. Stop the Consumer run at the first BLOCKED step so we
    # don't drive into a known app crash and report noise.
    consumer_steps = []
    stopped_reason = None
    for entry in doc["steps"]:
        if entry["role"] != "consumer":
            print(f"  [Business — {entry.get('status','?')}] {entry['step']}"
                  + (f"   (waits for: {entry['waits_for']})" if entry.get("waits_for") else ""))
            continue
        if entry.get("status") == "BLOCKED" and stopped_reason is None:
            stopped_reason = entry["step"]
        if stopped_reason is None:
            consumer_steps.append(_clean(entry["step"]))

    if stopped_reason:
        print(f"\n  Consumer steps stop before '{stopped_reason}' "
              f"(BLOCKED by an app crash — see blockers in the yaml).\n")

    if not consumer_steps:
        print("No runnable Consumer steps.")
        return

    req = ScenarioRequest(
        project_id=CONSUMER_PROJECT_ID,
        steps=consumer_steps,
        device_id=args.device,
        bundle_id=consumer["bundle_id"],
        save=args.save,
        name="full_cross_app_consumer",
    )

    print("--- Running Consumer steps live ---")
    passed = total = 0
    for ev in scenario_events(req, consumer["bundle_id"], consumer_steps, repo_path):
        d = json.loads(ev.strip()[6:])
        t = d.get("type")
        if t == "step":
            mark = " OK " if d["ok"] else "FAIL"
            print(f"  [{mark}] {d['step'][:36]:36}  -> {d['action'][:60]}")
            if d.get("detail"):
                print(f"          {d['detail'][:76]}")
        elif t == "phase":
            print(f"  ...{d['message']}")
        elif t == "error":
            print(f"  ERROR: {d['detail'][:80]}")
        elif t == "done":
            passed, total = d["passed"], d["total"]
            print(f"\n=== Consumer: {passed}/{total} steps "
                  + (f"| saved {d.get('saved_to')}" if d.get("saved_to") else "") + " ===")

    print("\nBusiness half + payment settlement require Option A (Android bot) or")
    print("captured Business labels + the two Consumer crash fixes. See the yaml.")
    return 0 if total and passed == total else 1


if __name__ == "__main__":
    sys.exit(main() or 0)
