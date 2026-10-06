"""Can the rig run a flow right now? Checked before every item of a batch.

A failure here is an environment problem, so the item is held (staging) or
skipped as INFRA — it never counts as the app failing.
"""
from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, List

import httpx

APPIUM_STATUS = "http://127.0.0.1:4723/status"
STAGING_URL = "https://vya.xorstack.com"


def _appium() -> Dict[str, Any]:
    try:
        r = httpx.get(APPIUM_STATUS, timeout=5)
        ok = r.status_code == 200
        return {"name": "Appium", "ok": ok, "detail": "ready" if ok else f"HTTP {r.status_code}"}
    except Exception as e:
        return {"name": "Appium", "ok": False, "detail": f"not reachable ({type(e).__name__})"}


def _simulators() -> Dict[str, Any]:
    from automation.scenarios import cross_app_config as cfgmod
    want = {u for u in (cfgmod.load_config().get("devices") or {}).values() if u}
    try:
        out = subprocess.run(["xcrun", "simctl", "list", "devices", "booted", "-j"],
                             capture_output=True, text=True, timeout=20).stdout
        booted = {d["udid"] for devs in json.loads(out).get("devices", {}).values() for d in devs}
    except Exception as e:
        return {"name": "Simulators", "ok": False, "detail": f"simctl failed: {e}"}
    missing = sorted(want - booted)
    if missing:
        return {"name": "Simulators", "ok": False,
                "detail": "not booted: " + ", ".join(m[:8] for m in missing)}
    return {"name": "Simulators", "ok": True, "detail": f"{len(want)} booted"}


def staging() -> Dict[str, Any]:
    """vya.xorstack.com flaps 502; while it does, bookings cannot complete."""
    try:
        r = httpx.get(STAGING_URL, timeout=10, follow_redirects=True)
        ok = r.status_code < 500
        return {"name": "Staging backend", "ok": ok, "detail": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"name": "Staging backend", "ok": False, "detail": f"unreachable ({type(e).__name__})"}


def run(env: str = "staging") -> List[Dict[str, Any]]:
    checks = [_appium(), _simulators()]
    if env == "staging":
        checks.append(staging())
    return checks


def all_ok(checks: List[Dict[str, Any]]) -> bool:
    return all(c["ok"] for c in checks)
