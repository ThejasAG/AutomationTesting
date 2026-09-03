"""Live UI inspector — the accessibility tree of a running app, plus what is wrong with it.

Answers the question that cost the most time on 2026-09-02: "the step says it tapped
it, so why did nothing happen?" Three times the answer was that something was drawn
over the control, and finding that meant dumping frames and comparing rectangles by
hand. This does it in one request.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from automation.api.v1.routers.auth import get_current_user
from automation.inspector.overlap import label_of, rect_of, report

router = APIRouter(prefix="/inspector", tags=["Inspector"])

_IDB = "/usr/local/bin/idb"


def _tree(udid: str) -> List[Dict[str, Any]]:
    try:
        raw = subprocess.run([_IDB, "ui", "describe-all", "--udid", udid],
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
