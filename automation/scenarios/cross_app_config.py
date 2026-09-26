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
from typing import Any, Dict, Iterable, List, Optional

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
    """Available iOS simulators for the role pickers: ones Appium can drive on
    this Xcode first (see projects.simulators), then Booted, then by name."""
    from automation.projects import simulators
    sims = simulators.list_sims()
    sdk = simulators.sdk_major()
    for s in sims:
        s["testable"] = simulators.is_testable(s, sdk)
    sims.sort(key=lambda s: (not s["testable"], s["state"] != "Booted", s["name"]))
    return sims


# Which kind of simulator each role needs.
ROLE_KIND = {"consumer": "iPhone", "waiter": "iPad", "kitchen": "iPad"}


def local_udid_for(kind: str, preferred: Optional[str] = None,
                   exclude: Iterable[str] = (),
                   sims: Optional[List[Dict[str, str]]] = None) -> Optional[str]:
    """A simulator of *kind* ("iPhone"/"iPad") that exists on THIS Mac.

    A UDID only exists on the Mac that created it, so every id written into
    config or code (DA24A392… is one developer's iPhone 16 Pro) fails on every
    other machine with "Invalid device or device pair". *preferred* is kept
    when it exists here and Appium can drive it on this Xcode; otherwise the
    nearest local match is chosen -- booted first, then the same model as
    *preferred* was, then a Pro model, then any of that kind.
    """
    from automation.projects.simulators import testable
    sims = list_ios_simulators() if sims is None else sims
    # Only simulators Appium can drive with this Xcode: an iOS 18 sim exists and
    # boots under Xcode 26, but WebDriverAgent cannot load on it.
    sims = testable(sims)
    if preferred and any(s["udid"] == preferred for s in sims):
        return preferred
    pool = [s for s in sims if s["name"].startswith(kind) and s["udid"] not in set(exclude)]
    if not pool:
        return None
    model = _DEFAULT_MODEL.get(preferred or "", "")
    pool.sort(key=lambda s: (s["state"] != "Booted",
                             not s["name"].startswith(model or "\0"),
                             "Pro" not in s["name"], s["name"]))
    return pool[0]["udid"]


# The models the hardcoded defaults were, so a replacement matches them.
_DEFAULT_MODEL = {
    "DA24A392-FF1B-4283-A5CE-CDDE0D000D21": "iPhone 16 Pro",
    "D19D3EC7-5494-4B69-AC7B-3AB8AE0B4D1B": "iPad Pro 11",
    "B1093E61-C510-4E6E-8A60-C2D05D150F64": "iPhone 16",
}


def localize_devices(devices: Dict[str, str],
                     sims: Optional[List[Dict[str, str]]] = None) -> Dict[str, str]:
    """Replace every role device that does not exist on this Mac with a local one.

    Waiter and kitchen stay on ONE iPad when they were configured that way (the
    shared-device account switch); consumer never shares with them.
    """
    sims = list_ios_simulators() if sims is None else sims
    if not sims:
        return devices          # simctl unavailable: cannot judge, change nothing
    out = dict(devices)
    shared = devices.get("waiter") == devices.get("kitchen")
    out["consumer"] = local_udid_for("iPhone", devices.get("consumer"), sims=sims) \
        or devices.get("consumer")
    out["waiter"] = local_udid_for("iPad", devices.get("waiter"),
                                   exclude=[out["consumer"]], sims=sims) or devices.get("waiter")
    out["kitchen"] = out["waiter"] if shared else (
        local_udid_for("iPad", devices.get("kitchen"), exclude=[out["consumer"]], sims=sims)
        or devices.get("kitchen"))
    return out


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
    # A device id from another Mac (the defaults, or a copied config file) is
    # swapped for this Mac's own simulator of the same kind.
    cfg["devices"] = localize_devices(cfg["devices"])
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
