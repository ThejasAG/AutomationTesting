"""Run a project's saved scenarios against ONE Appium session, in-process.

Mirrors _batch_events in automation/api/v1/routers/scenario.py, with one
difference that is the whole point: it runs the code ON DISK RIGHT NOW. The
backend holds Python modules in memory, so after editing scenario_runner.py the
API keeps serving the old code until it is restarted — and restarting it kills
whatever demo is running on the same stack. This bypasses that entirely.

    python -u scripts/run_project_scenarios.py <project_id> [name-prefix ...]

`python -u` matters: a scenario takes minutes and buffered stdout means results
never reach the log file.
"""
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from appium import webdriver  # noqa: E402

from automation.api.v1.routers.scenario import _apply_speed_settings, _appium_options  # noqa: E402
from automation.database.config import DATABASE_URL  # noqa: E402
from automation.intelligence.scenario_runner import ScenarioResult, ScenarioRunner  # noqa: E402

APPIUM = os.getenv("APPIUM_URL", "http://127.0.0.1:4723")

if len(sys.argv) < 2:
    sys.exit(__doc__)
project_id, prefixes = sys.argv[1], sys.argv[2:]

db = DATABASE_URL.removeprefix("sqlite:///")
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
row = c.execute("select name, app_bundle_id from test_projects where id=?", (project_id,)).fetchone()
if not row:
    sys.exit(f"no such project: {project_id}")
project_name, bundle = row
bundle = os.getenv("BUNDLE_ID", bundle)

scenarios = [(n, json.loads(s or "[]")) for n, s in c.execute(
    "select name, steps from saved_scenarios where project_id=? order by name", (project_id,))]
devices = {d for (d,) in c.execute(
    "select distinct device_id from saved_scenarios where project_id=?", (project_id,)) if d}
if prefixes:
    scenarios = [s for s in scenarios if any(s[0].startswith(p) for p in prefixes)]
if not scenarios:
    sys.exit("no scenarios matched")

device = os.getenv("DEVICE_ID") or (devices.pop() if len(devices) == 1 else "")
if not device:
    sys.exit(f"scenarios are pinned to {len(devices)} devices — set DEVICE_ID")

out_path = os.path.join(ROOT, "reports", f"scenarios_{project_id[:8]}.json")
os.makedirs(os.path.dirname(out_path), exist_ok=True)
shot_dir = os.path.join(ROOT, "reports", "scenario")
os.makedirs(shot_dir, exist_ok=True)

print(f"{project_name}: {len(scenarios)} scenarios, bundle={bundle}, device={device[:8]}", flush=True)

driver = webdriver.Remote(APPIUM, options=_appium_options(device, bundle))
_apply_speed_settings(driver)

report = []
try:
    for si, (name, steps) in enumerate(scenarios, 1):
        steps = [s for s in steps if s.strip()]
        print(f"\n=== [{si}/{len(scenarios)}] {name} ({len(steps)} steps)", flush=True)
        try:
            driver.terminate_app(bundle)
        except Exception:
            pass
        driver.activate_app(bundle)
        time.sleep(3)

        runner = ScenarioRunner(driver, bundle, screenshot_dir=shot_dir)
        result = ScenarioResult()
        t0, rows = time.time(), []
        for i, step in enumerate(steps):
            ts = time.time()
            res = runner.run_one(step, i)
            dt = round(time.time() - ts, 1)
            result.results.append(res)
            rows.append({"step": res.step, "ok": res.ok, "secs": dt,
                         "action": res.action, "detail": (res.detail or "")[:300]})
            print(f"  [{'ok ' if res.ok else 'FAIL'}] {dt:6.1f}s  {res.step}  -> {res.action}"
                  + (f"  | {(res.detail or '')[:160]}" if not res.ok else ""), flush=True)
            popup = runner.handle_book_popup()
            if popup is not None:
                result.results.append(popup)
                print(f"       (popup) {popup.action}", flush=True)
        secs = round(time.time() - t0, 1)
        report.append({"name": name, "ok": result.ok, "passed": result.passed,
                       "total_steps": len(result.results), "secs": secs, "steps": rows})
        print(f"=== {name}: {'PASS' if result.ok else 'FAIL'} "
              f"({result.passed}/{len(result.results)}) in {secs}s", flush=True)
        # Written every scenario, so a run killed halfway still leaves its results.
        with open(out_path, "w") as f:
            json.dump(report, f, indent=2)
finally:
    try:
        driver.quit()
    except Exception:
        pass
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)

print("\n===== SUMMARY =====", flush=True)
for r in report:
    print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['secs']:7.1f}s  "
          f"{r['passed']}/{r['total_steps']}  {r['name']}", flush=True)
print(f"{sum(1 for r in report if r['ok'])}/{len(report)} passed, "
      f"{round(sum(r['secs'] for r in report), 1)}s total -> {out_path}", flush=True)
