"""Turn a point from `idb ui describe-all` into the point `idb ui tap` expects.

On a portrait device they are the same. On a landscape app (the business iPad)
they are not: describe-all reports landscape app coordinates (1210 x 834) while
tap -- and describe-point -- take portrait device coordinates (834 x 1210).
Which way the landscape is turned depends on how the simulator was last
rotated, so no fixed formula is right. Measured on the same iPad:

    homeBtn  describe-all (48, 65)   ->  tap (834 - 65, 48)    [today]
    addNewEvent (725, 723)           ->  tap (723, 1210 - 725) [earlier]

A fixed formula made every iPad idb tap land ~240pt off: 'Save does nothing',
'form never opened', a tap on Table opening History.

So the direction is MEASURED: take an element whose describe-all frame is
known, convert its centre both ways, and ask describe-point which one hits that
same element. The answer is cached per simulator for a short while.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from typing import Dict, List, Optional, Tuple

from automation.scenarios.idb_path import idb_binary

logger = logging.getLogger(__name__)

TTL = 120.0
_CACHE: Dict[str, Tuple[float, str, float, float]] = {}   # udid -> (t, mode, w, h)

# mode -> device point for an app point (x, y) on a w x h landscape app
_MODES = {
    "same": lambda x, y, w, h: (x, y),
    "ccw": lambda x, y, w, h: (h - y, x),
    "cw": lambda x, y, w, h: (y, w - x),
}


def _run_json(args: List[str], timeout: float = 15):
    out = subprocess.run([idb_binary(), *args], capture_output=True, text=True,
                         timeout=timeout).stdout
    return json.loads(out or "null")


def _name(e) -> str:
    if isinstance(e, list):
        e = e[0] if e else {}
    e = e or {}
    return (e.get("AXIdentifier") or e.get("AXLabel") or "").strip()


def _measure(udid: str, elements: Optional[list] = None) -> Tuple[str, float, float]:
    els = elements if elements is not None else _run_json(
        ["ui", "describe-all", "--udid", udid])
    app = next((e for e in els or [] if e.get("type") == "Application"), None)
    f = (app or {}).get("frame") or {}
    w, h = float(f.get("width") or 0), float(f.get("height") or 0)
    if not w or w <= h:
        return "same", w, h
    # Small, named, distinct elements make unambiguous probes.
    probes = [e for e in els if e.get("type") != "Application" and _name(e)
              and 0 < (e.get("frame") or {}).get("width", 0) < w / 2]
    names = [_name(e) for e in probes]
    probes = [e for e in probes if names.count(_name(e)) == 1][:4]
    for e in probes:
        pf = e["frame"]
        cx, cy = pf["x"] + pf["width"] / 2, pf["y"] + pf["height"] / 2
        for mode in ("ccw", "cw"):
            px, py = _MODES[mode](cx, cy, w, h)
            try:
                hit = _run_json(["ui", "describe-point", "--udid", udid,
                                 str(int(px)), str(int(py))])
            except Exception:
                continue
            if _name(hit) == _name(e):
                return mode, w, h
    logger.warning("idb_coords: could not measure rotation on %s; assuming cw", udid[:8])
    return "cw", w, h


def to_device(udid: str, x: float, y: float,
              elements: Optional[list] = None) -> Tuple[int, int]:
    """Device point for idb tap/swipe from a describe-all (app-space) point.

    Never raises: if anything fails, the point is returned unchanged."""
    try:
        now = time.time()
        c = _CACHE.get(udid)
        if not c or now - c[0] > TTL:
            mode, w, h = _measure(udid, elements)
            _CACHE[udid] = c = (now, mode, w, h)
        _, mode, w, h = c
        px, py = _MODES[mode](x, y, w, h)
        return int(px), int(py)
    except Exception as e:
        logger.debug("idb_coords.to_device(%s): %s", udid[:8], e)
        return int(x), int(y)


def mode(udid: str, elements: Optional[list] = None) -> str:
    """Which way the app is turned on the device: 'same', 'ccw' or 'cw'
    (measured, then cached like to_device)."""
    to_device(udid, 1, 1, elements)
    return (_CACHE.get(udid) or (0, "same"))[1]


def upright(image, mode_: str):
    """A PIL image of the device framebuffer (always portrait) turned the way the
    app is, so it lines up with describe-all frames."""
    return image.rotate({"ccw": 90, "cw": 270}.get(mode_, 0), expand=True)


def forget(udid: str = "") -> None:
    """Drop the cached direction (e.g. after the simulator was rotated)."""
    if udid:
        _CACHE.pop(udid, None)
    else:
        _CACHE.clear()
