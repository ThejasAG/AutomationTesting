"""Live UI inspector — the accessibility tree of a running app, plus what is wrong with it.

Answers the question that cost the most time on 2026-09-02: "the step says it tapped
it, so why did nothing happen?" Three times the answer was that something was drawn
over the control, and finding that meant dumping frames and comparing rectangles by
hand. This does it in one request.
"""

from __future__ import annotations

import json
import shutil
import sys
import os
import subprocess
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from automation.api.v1.routers.auth import get_current_user
from automation.inspector.overlap import label_of, rect_of, report

router = APIRouter(prefix="/inspector", tags=["Inspector"])

def _idb_path() -> str:
    """The idb executable on THIS machine, or a message saying how to get it.

    Resolution is shared with the scenario runner (scenarios/idb_path.py), which
    already honours IDB_BINARY and falls back to PATH. This module previously
    hardcoded /usr/local/bin/idb, which exists under Intel Homebrew and not under
    Apple Silicon's /opt/homebrew or inside a virtualenv -- so the Inspector
    worked on the machine it was written on and returned a raw ENOENT naming a
    path the operator has no reason to recognise on any other.
    """
    from automation.scenarios.idb_path import find_idb
    # find_idb() returns None when idb is genuinely absent (idb_binary() would
    # hand back the bare name "idb", which is not evidence the tool exists).
    found = find_idb()
    if found:
        return found
    raise HTTPException(
        status_code=503,
        detail="The UI Inspector needs Facebook's idb, which was not found on "
               "this machine. Install it with:\n"
               "  brew tap facebook/fb && brew install idb-companion\n"
               "  pip install fb-idb\n"
               "Then restart the backend. Set IDB_BINARY to override the path. "
               "Scenario execution does not use the Inspector and is unaffected.")


def _tree(udid: str) -> List[Dict[str, Any]]:
    idb = _idb_path()
    try:
        raw = subprocess.run([idb, "ui", "describe-all", "--udid", udid],
                             capture_output=True, text=True, timeout=25).stdout
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not read the UI tree: {e}")
    if not (raw or "").strip().startswith("["):
        raise HTTPException(
            status_code=502,
            detail="idb returned no tree — is the device booted and an app in the "
                   "foreground?")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"Malformed UI tree: {e}")


@router.get("/{udid}/screenshot", dependencies=[Depends(get_current_user)])
def screenshot(udid: str) -> Dict[str, Any]:
    """The device screen as a data-URI, to draw element frames over.

    The rotation bug was ONLY visible by comparing this against the reported frames:
    the screenshot came back portrait 834x1210 while the app reported landscape
    1210x834, so `size` is returned alongside for the caller to scale by.
    """
    import base64, tempfile, os as _os
    path = _os.path.join(tempfile.gettempdir(), f"inspect_{udid[:8]}.png")
    try:
        subprocess.run(["xcrun", "simctl", "io", udid, "screenshot", path],
                       capture_output=True, timeout=30)
        with open(path, "rb") as f:
            data = f.read()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not capture the screen: {e}")
    finally:
        try: _os.remove(path)
        except OSError: pass
    try:
        from PIL import Image
        import io as _io
        w, h = Image.open(_io.BytesIO(data)).size
    except Exception:
        w = h = 0
    return {"udid": udid, "width": w, "height": h,
            "image": "data:image/png;base64," + base64.b64encode(data).decode()}


@router.get("/{udid}/tree", dependencies=[Depends(get_current_user)])
def inspect(udid: str,
            safe_margin: float = Query(
                0, ge=0, le=400,
                description="Exclude a band top and bottom. An element under the "
                            "header is on screen but still untappable — a card at "
                            "y=29 had its tap land on the status bar."),
            only_labelled: bool = Query(True)) -> Dict[str, Any]:
    """The tree, the screen size, and every element that cannot be tapped as drawn."""
    els = _tree(udid)
    app = next((e for e in els if (e.get("type") or "") == "Application"), {})
    frame = app.get("frame") or {}
    w, h = float(frame.get("width", 0)), float(frame.get("height", 0))

    problems = report(els, w, h, safe_margin=safe_margin, only_labelled=only_labelled)

    elements = []
    for e in els:
        r = rect_of(e)
        if not r:
            continue
        elements.append({
            "label": label_of(e),
            "type": e.get("type"),
            "frame": {"x": int(r.x), "y": int(r.y), "w": int(r.w), "h": int(r.h)},
            "centre": {"x": int(r.cx), "y": int(r.cy)},
        })

    return {
        "udid": udid,
        # Landscape here means idb's coordinates are rotated relative to where a tap
        # lands — the bug that made every iPad coordinate tap miss by ~240pt.
        "screen": {"width": int(w), "height": int(h),
                   "orientation": "landscape" if w > h else "portrait"},
        "element_count": len(elements),
        "elements": elements,
        "problems": problems,
        "problem_count": len(problems),
    }


class TapBody(BaseModel):
    x: float
    y: float


@router.post("/{udid}/tap", dependencies=[Depends(get_current_user)])
def tap(udid: str, body: TapBody) -> Dict[str, Any]:
    """Tap the device at an app-space point (the same POINTS /tree reports).

    Without this the Inspector was look-only: clicking an element selected it and
    nothing reached the device, which read as "nothing can be tapped".

    idb reports landscape frames but taps in the device's portrait space, so the
    point is rotated exactly as FlowRunner._rotate_for_device does — on the iPad
    an unrotated tap lands ~240pt from its target. Portrait phones are unaffected.
    """
    idb = _idb_path()
    # The same MEASURED rotation the flows use (idb_coords): which way a landscape
    # iPad's app is turned depends on how the simulator was last rotated, so the
    # fixed formula (x, y) -> (y, w - x) was right one way only -- turned the other
    # way every tap landed ~240pt off.
    from automation.scenarios.idb_coords import to_device
    x, y = to_device(udid, body.x, body.y)
    try:
        done = subprocess.run([idb, "ui", "tap", "--udid", udid, str(int(x)), str(int(y))],
                              capture_output=True, text=True, timeout=10)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Tap failed: {e}")
    if done.returncode != 0:
        raise HTTPException(status_code=502,
                            detail=f"Tap failed: {(done.stderr or done.stdout).strip()[:300]}")
    return {"udid": udid, "tapped": {"x": int(x), "y": int(y)}}
