"""Which iOS simulators on THIS Mac can actually be driven by Appium.

A simulator can exist, boot and run the app and still be untestable: Xcode 26's
XCTest links _LocationEssentials, which only iOS 26 runtimes ship, so
WebDriverAgent built by Xcode 26.5 dies on an iOS 18.1 simulator ("built for
iOS-sim 26.5 which is newer than running OS" / "Failed to load the test
bundle") and every run fails at session start with ECONNREFUSED :8100.

The rule used everywhere a simulator is picked: with Xcode 26 or later, prefer
simulators on iOS 26 or later. Older Xcodes drive older runtimes, so there the
rule does nothing. When none exist the full
list is returned unchanged -- this must never turn a working pick into none.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def _major(version: str) -> int:
    try:
        return int(str(version).split(".")[0])
    except (ValueError, IndexError):
        return 0


_SDK_MAJOR: Optional[int] = None


def sdk_major() -> int:
    """Major version of the newest iOS Simulator SDK in the selected Xcode (0 if unknown).

    Cached once known; "unknown" is NOT cached, because machine_setup may install
    the iOS platform later in the same process."""
    global _SDK_MAJOR
    if _SDK_MAJOR:
        return _SDK_MAJOR
    try:
        out = subprocess.run(["xcodebuild", "-showsdks"], capture_output=True,
                             text=True, timeout=60).stdout
    except Exception:
        return 0
    found = max((_major(v) for v in re.findall(r"iphonesimulator([\d.]+)", out)), default=0)
    if found:
        _SDK_MAJOR = found
    return found


def list_sims() -> List[Dict[str, str]]:
    """Available iOS simulators: udid, name, state, ios (e.g. "26.5")."""
    try:
        out = subprocess.run(["xcrun", "simctl", "list", "devices", "available", "-j"],
                             capture_output=True, text=True, timeout=15).stdout
        data = json.loads(out)
    except Exception:
        return []
    sims = []
    for runtime, devs in data.get("devices", {}).items():
        if "iOS" not in runtime:
            continue
        ios = runtime.split("iOS-")[-1].replace("-", ".") if "iOS-" in runtime else ""
        for d in devs:
            if d.get("isAvailable"):
                sims.append({"udid": d["udid"], "name": d.get("name", ""),
                             "state": d.get("state", "Shutdown"), "ios": ios})
    return sims


# The first Xcode whose XCTest cannot load on older simulator runtimes (it links
# _LocationEssentials, which only iOS 26+ runtimes ship). Earlier Xcodes drive
# older runtimes fine, so the rule must not fire there.
XCTEST_NEEDS_SAME_MAJOR_FROM = 26


def is_testable(sim: Dict[str, str], sdk: Optional[int] = None) -> bool:
    sdk = sdk_major() if sdk is None else sdk
    if sdk < XCTEST_NEEDS_SAME_MAJOR_FROM:
        return True
    return _major(sim.get("ios", "")) >= XCTEST_NEEDS_SAME_MAJOR_FROM


def testable(sims: List[Dict[str, str]], sdk: Optional[int] = None) -> List[Dict[str, str]]:
    """The simulators Appium can drive; all of them when none qualify."""
    good = [s for s in sims if is_testable(s, sdk)]
    return good or sims


def create_missing(step=lambda m: logger.info(m)) -> List[str]:
    """Create an iPhone and an iPad on the newest iOS runtime when none exist.

    Installing a runtime normally creates a default set, but not always (and a
    user may have deleted them). Returns the udids created.
    """
    try:
        rt = json.loads(subprocess.run(["xcrun", "simctl", "list", "runtimes", "-j"],
                                       capture_output=True, text=True, timeout=30).stdout)
    except Exception:
        return []
    runtimes = [r for r in rt.get("runtimes", [])
                if r.get("platform") == "iOS" and r.get("isAvailable")]
    if not runtimes:
        return []
    newest = max(runtimes, key=lambda r: [int(x) for x in r["version"].split(".")])
    sims = [s for s in list_sims() if _major(s["ios"]) >= _major(newest["version"])]
    created = []
    for kind in ("iPhone", "iPad"):
        if any(s["name"].startswith(kind) for s in sims):
            continue
        types = [t for t in newest.get("supportedDeviceTypes", [])
                 if t.get("name", "").startswith(kind)]
        # A Pro model: what the flows and screenshots were written against.
        pick = next((t for t in reversed(types) if "Pro" in t["name"]), None) \
            or (types[-1] if types else None)
        if not pick:
            continue
        r = subprocess.run(["xcrun", "simctl", "create", pick["name"],
                            pick["identifier"], newest["identifier"]],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            created.append(r.stdout.strip())
            step(f"Created simulator {pick['name']} (iOS {newest['version']}) — "
                 f"Appium needs one on the same iOS as Xcode")
    return created


# Apple's on-device photo/media indexers. On a fresh iOS 26 simulator each one
# runs flat out for a long time (~400% CPU per simulator was measured on an Intel
# Mac), which starved the build and made `simctl install` time out at 300s. The
# apps under test never need them. `launchctl disable` persists in the
# simulator's data, so a reboot keeps them off; bootout stops the running copy.
QUIET_DAEMONS = ("com.apple.mediaanalysisd", "com.apple.mediaanalysisd.service",
                 "com.apple.photoanalysisd")
_quieted: set = set()


def quiet_background_daemons(udid: str) -> bool:
    """Stop and disable the media indexers inside a booted simulator.

    Idempotent and best-effort: done once per simulator per process, never raises.
    Returns True when the simulator was quieted (now or earlier)."""
    if not udid or udid in _quieted:
        return bool(udid)
    ok = True
    for label in QUIET_DAEMONS:
        for verb in ("disable", "bootout"):
            try:
                subprocess.run(["xcrun", "simctl", "spawn", udid, "launchctl", verb,
                                f"system/{label}"], capture_output=True, text=True,
                               timeout=60)
            except Exception as e:
                logger.debug("quiet %s %s on %s: %s", verb, label, udid[:8], e)
                ok = False
    if ok:
        _quieted.add(udid)
        logger.info("Simulator %s: media indexers disabled", udid[:8])
    return ok


def quiet_booted() -> List[str]:
    """quiet_background_daemons for every booted simulator. Returns their udids."""
    done = []
    for s in list_sims():
        if s["state"] == "Booted" and quiet_background_daemons(s["udid"]):
            done.append(s["udid"])
    return done
