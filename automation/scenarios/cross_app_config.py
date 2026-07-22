"""Cross-app run configuration: which simulator plays each role, and the logins.

Roles:
  consumer — the diner (Consumer app, iPhone)
  waiter   — Business app, waiter account
  kitchen  — Business app, kitchen account

Waiter and kitchen are the SAME app with two accounts. They may run on the same
device (log in as waiter, then switch to kitchen) or on two different devices
(two Business-app instances). The device map expresses whichever you choose.

Credentials are stored in a gitignored JSON file next to the DB, seeded once from
the VYA_* environment variables. Passwords are never returned by the API in the
clear — only a has_password flag. This is a local QA tool; treat the file as
secret and do not commit it (it is in .gitignore).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List

_CONFIG_PATH = Path(os.getenv(
    "CROSS_APP_CONFIG_PATH",
    Path(__file__).resolve().parents[2] / "cross_app_config.json",
))

ROLES = ("consumer", "waiter", "kitchen")

# Env vars each role's credentials seed from, when the config file is first made.
_ENV_SEED = {
    "consumer": ("VYA_CONSUMER_USER", "VYA_CONSUMER_PASSWORD"),
    "waiter": ("VYA_BUSINESS_WAITER_USER", "VYA_BUSINESS_WAITER_PASSWORD"),
    "kitchen": ("VYA_BUSINESS_KITCHEN_USER", "VYA_BUSINESS_KITCHEN_PASSWORD"),
}

# Sensible defaults when nothing is configured yet.
_DEFAULT_DEVICES = {
    "consumer": "DA24A392-FF1B-4283-A5CE-CDDE0D000D21",   # iPhone 16 Pro
    "waiter":   "D19D3EC7-5494-4B69-AC7B-3AB8AE0B4D1B",   # iPad Pro 11"
    "kitchen":  "D19D3EC7-5494-4B69-AC7B-3AB8AE0B4D1B",   # same iPad (switch accounts)
}


def list_ios_simulators() -> List[Dict[str, str]]:
    """Available iOS simulators, Booted first, for the role pickers."""
    try:
        out = subprocess.run(
            ["xcrun", "simctl", "list", "devices", "available", "-j"],
            capture_output=True, text=True, timeout=15,
        ).stdout
        data = json.loads(out)
    except Exception:
        return []
    sims = []
    for runtime, devs in data.get("devices", {}).items():
        if "iOS" not in runtime:
            continue
        ios = runtime.split("iOS-")[-1].replace("-", ".") if "iOS-" in runtime else ""
        for dev in devs:
            if dev.get("isAvailable"):
                sims.append({
                    "udid": dev["udid"], "name": dev["name"],
                    "state": dev.get("state", "Shutdown"), "ios": ios,
                })
    sims.sort(key=lambda s: (s["state"] != "Booted", s["name"]))
    return sims


def _seed_from_env() -> Dict[str, Any]:
    creds = {}
    for role, (u, p) in _ENV_SEED.items():
        creds[role] = {"email": os.getenv(u, ""), "password": os.getenv(p, "")}
    return {"devices": dict(_DEFAULT_DEVICES), "credentials": creds}


def load_config() -> Dict[str, Any]:
    if _CONFIG_PATH.exists():
        try:
            cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = _seed_from_env()
    else:
        cfg = _seed_from_env()
    cfg.setdefault("devices", dict(_DEFAULT_DEVICES))
    cfg.setdefault("credentials", {})
    for role in ROLES:
        cfg["devices"].setdefault(role, _DEFAULT_DEVICES[role])
        cfg["credentials"].setdefault(role, {"email": "", "password": ""})
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def public_config() -> Dict[str, Any]:
    """Config for the UI: devices + emails, passwords masked to has_password."""
    cfg = load_config()
    creds = {
        role: {
            "email": cfg["credentials"].get(role, {}).get("email", ""),
            "has_password": bool(cfg["credentials"].get(role, {}).get("password")),
        }
        for role in ROLES
    }
    return {
        "simulators": list_ios_simulators(),
        "devices": cfg["devices"],
        "credentials": creds,
    }


def apply_update(devices: Dict[str, str] | None,
                 credentials: Dict[str, Dict[str, str]] | None) -> Dict[str, Any]:
    """Merge a UI update into the stored config and save it.

    A blank password in the update means 'keep the existing one' — so the UI can
    show masked fields without wiping stored secrets.
    """
    cfg = load_config()
    if devices:
        for role in ROLES:
            if devices.get(role):
                cfg["devices"][role] = devices[role]
    if credentials:
        for role in ROLES:
            upd = credentials.get(role) or {}
            if "email" in upd:
                cfg["credentials"][role]["email"] = upd["email"]
            if upd.get("password"):        # blank = keep existing
                cfg["credentials"][role]["password"] = upd["password"]
    save_config(cfg)
    return cfg
