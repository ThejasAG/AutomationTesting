"""Live scenario recorder — mirror the simulator, turn clicks into steps.

The dashboard shows the real simulator's screen (served as a frame here); when the
user clicks the mirror we (1) hit-test the element at that point from the live UI
tree, (2) actually tap it through Appium so the app advances, and (3) record a
plain-language step the ScenarioRunner can replay. Stop → save as a SavedScenario.

One Appium session is held open per recording session (sequential user actions, so
no concurrency concern). This is an in-browser Appium Inspector with record mode.
"""

from __future__ import annotations

import base64
import io
import logging
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from automation.auth.security import get_current_user
from automation.database.config import SessionLocal
from automation.database.models import TestProject, SavedScenario

logger = logging.getLogger("recorder")
router = APIRouter(prefix="/recorder", tags=["Scenario Recorder"])

# session_id -> {driver, bundle_id, steps, size(points), project_id, device_id}
_SESSIONS: Dict[str, Dict[str, Any]] = {}


class StartBody(BaseModel):
    project_id: str
    device_id: str
    bundle_id: Optional[str] = None
    appium_url: str = "http://127.0.0.1:4723"


class TapBody(BaseModel):
    # Normalised click position on the mirror (0..1), origin top-left.
    x: float
    y: float


class SaveBody(BaseModel):
    name: str
    description: Optional[str] = None


def _encode_frame(png: bytes) -> str:
    """Downscale the retina screenshot to a small JPEG data URL. A raw device PNG
    is ~240 KB; this makes it ~20-30 KB so the mirror transfers and decodes fast."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(png)).convert("RGB")
        w, h = im.size
        target_w = 430
        if w > target_w:
            im = im.resize((target_w, round(h * target_w / w)), Image.BILINEAR)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=68)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return "data:image/png;base64," + base64.b64encode(png).decode()


def _sess_or_404(sid: str) -> Dict[str, Any]:
    s = _SESSIONS.get(sid)
    if not s:
        raise HTTPException(status_code=404, detail="Recording session not found (start a new one).")
    return s


def _element_at(source: str, px: float, py: float) -> Optional[Dict[str, str]]:
    """Deepest visible element whose rect contains the point (px,py in points).

    Returns {label, testid, type, locator} — 'locator' is the most reliable thing
    to replay by: a UNIQUE accessibility id if the element has one (replays exactly),
    otherwise the visible text. That is what makes a recording actually re-runnable."""
    try:
        root = ET.fromstring(source)
    except Exception:
        return None

    # Count accessibility-id occurrences so we only trust an id that is unique
    # (a shared wrapper id like "card-container-outer-layer" is not a locator).
    name_counts: Dict[str, int] = {}
    for el in root.iter():
        n = (el.attrib.get("name") or "").strip()
        if n:
            name_counts[n] = name_counts.get(n, 0) + 1

    best = None
    best_area = None
    for el in root.iter():
        a = el.attrib
        try:
            x, y = float(a.get("x", "nan")), float(a.get("y", "nan"))
            w, h = float(a.get("width", "0")), float(a.get("height", "0"))
        except ValueError:
            continue
        if w <= 0 or h <= 0:
            continue
        if a.get("visible", "true") == "false":
            continue
        if not (x <= px <= x + w and y <= py <= y + h):
            continue
        area = w * h
        # Smallest containing element = the most specific thing under the finger.
        if best_area is None or area < best_area:
            name = (a.get("name") or "").strip()
            label = (a.get("label") or "").strip()
            value = (a.get("value") or "").strip()
            # Prefer a unique id (exact replay); else the visible text; else the id.
            if name and name_counts.get(name, 0) == 1:
                locator = name
            elif label:
                locator = label
            elif value:
                locator = value
            else:
                locator = name
            best = {
                "label": (label or name or value or "").strip(),
                "testid": name,
                "type": (a.get("type") or "").replace("XCUIElementType", ""),
                "locator": locator,
            }
            best_area = area
    return best


def _step_for(el: Optional[Dict[str, str]]) -> str:
    """A replayable step for the tapped element — uses the reliable locator so the
    replay engine can find exactly what was tapped."""
    if not el:
        return "tap here"
    loc = el.get("locator") or el.get("label") or el.get("testid") or ""
    if loc:
        return f'tap {loc}'
    t = el.get("type") or "element"
    return f"tap the {t.lower()}"


@router.post("/start")
def start(body: StartBody, current_user=Depends(get_current_user)):
    """Boot the sim, open an Appium session on the app, and begin recording."""
    from appium import webdriver
    from appium.options.ios import XCUITestOptions
    from automation.projects.builder import app_builder
    from automation.projects.repository import repository_manager

    with SessionLocal() as db:
        project = db.query(TestProject).filter(TestProject.id == body.project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        bundle_id = body.bundle_id or project.app_bundle_id
    if not bundle_id:
        raise HTTPException(status_code=400, detail="No app bundle id — set one on the project.")
    repo_path = repository_manager.get_repo_path(body.project_id)

    # Use exactly the chosen simulator and boot it.
    resolved, _ = app_builder.resolve_ios_device(body.device_id, prefer_requested=True)
    device_id = resolved or body.device_id
    ok, msg = app_builder.ensure_ios_booted(device_id)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)

    # The recorder attaches to an already-installed app (it doesn't build). If it
    # isn't on this simulator, say so clearly instead of mirroring the home screen.
    import subprocess as _sp
    installed = _sp.run(["xcrun", "simctl", "get_app_container", device_id, bundle_id],
                        capture_output=True, text=True, timeout=15).returncode == 0
    if not installed:
        raise HTTPException(
            status_code=400,
            detail=f"The app ({bundle_id}) is not installed on this simulator. "
                   f"Prepare/run it once on this device (Projects → Prepare), then record.",
        )

    # A Debug React-Native build fetches its JS bundle from Metro — without it the
    # app opens on the red "No bundle URL present" screen and nothing is tappable.
    if repo_path:
        # Pass the device + bundle so an app with its own Metro (e.g. the Business
        # app on :8082) starts the right packager and is pointed at it.
        metro_ok, metro_msg = app_builder.ensure_metro(repo_path, udid=device_id, bundle_id=bundle_id)
        if not metro_ok:
            raise HTTPException(status_code=400, detail=metro_msg)

    opts = XCUITestOptions()
    opts.platform_name = "iOS"
    opts.automation_name = "XCUITest"
    opts.udid = device_id
    opts.bundle_id = bundle_id
    opts.no_reset = True
    # Central WDA resolver — was usePrebuiltWDA=True with no derivedDataPath.
    from automation.appium_service import wda as _wda
    _wda.apply(opts, udid=device_id)
    opts.set_capability("waitForQuiescence", False)   # don't wait for app-idle each command
    opts.set_capability("shouldUseCompactResponses", True)
    # Self-heal a wedged Appium/WDA before connecting (what makes "device stop working").
    appium_ok, appium_msg = app_builder.ensure_appium(body.appium_url)
    if not appium_ok:
        raise HTTPException(status_code=503, detail=appium_msg)
    try:
        driver = webdriver.Remote(body.appium_url, options=opts)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not start Appium session: {e}")
    try:
        driver.update_settings({"waitForIdleTimeout": 0, "shouldWaitForQuiescence": False,
                                "shouldUseCompactResponses": True, "snapshotMaxDepth": 40,
                                "useFirstMatch": True})
    except Exception:
        pass

    # Fresh launch so the app loads its JS bundle from Metro now — activate alone
    # can bring a stale "No bundle URL present" screen back to the front.
    try:
        driver.terminate_app(bundle_id)
    except Exception:
        pass
    try:
        driver.activate_app(bundle_id)
        import time as _t
        _t.sleep(3)
    except Exception:
        pass

    size = driver.get_window_size()   # points
    sid = str(uuid.uuid4())
    _SESSIONS[sid] = {
        "driver": driver, "bundle_id": bundle_id, "steps": [],
        "size": size, "project_id": body.project_id, "device_id": device_id,
        "tree": None, "tree_ts": 0.0,
    }
    return {"session_id": sid, "width": size["width"], "height": size["height"]}


@router.get("/{sid}/frame")
def frame(sid: str, current_user=Depends(get_current_user)):
    """Current screenshot of the mirrored simulator as a data URL + point size."""
    s = _sess_or_404(sid)
    try:
        png = s["driver"].get_screenshot_as_png()
    except Exception:
        # The Appium session died (server restarted, session ended). Drop it and
        # tell the client to stop polling instead of erroring forever.
        _SESSIONS.pop(sid, None)
        raise HTTPException(status_code=410, detail="The recording session ended (Appium stopped). Start a new recording.")
    # Cache the UI tree alongside the frame so a tap can hit-test instantly
    # against the very screen the user is looking at — no per-tap page_source.
    try:
        s["tree"] = s["driver"].page_source
        s["tree_ts"] = time.time()
    except Exception:
        pass
    return {
        "image": _encode_frame(png),
        "width": s["size"]["width"], "height": s["size"]["height"],
        "steps": s["steps"],
    }


@router.post("/{sid}/tap")
def tap(sid: str, body: TapBody, current_user=Depends(get_current_user)):
    """Hit-test the click, tap it through Appium, and record the step."""
    s = _sess_or_404(sid)
    driver = s["driver"]
    px = max(0.0, min(1.0, body.x)) * s["size"]["width"]
    py = max(0.0, min(1.0, body.y)) * s["size"]["height"]

    # Prefer the tree cached by the last frame poll (which is the screen the user
    # is looking at) — that makes the tap instant. Only fetch page_source if the
    # cache is missing or stale (e.g. rapid taps before the next poll).
    source = s.get("tree") or ""
    if not source or (time.time() - s.get("tree_ts", 0)) > 4:
        try:
            source = driver.page_source
        except Exception:
            source = ""
    el = _element_at(source, px, py)
    step = _step_for(el)

    try:
        driver.tap([(int(px), int(py))], 1)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Tap failed: {e}")

    s["steps"].append(step)
    s["tree"] = None   # the screen just changed — force a fresh tree next poll

    # Return the post-tap frame in the SAME response so the client doesn't need a
    # second round trip to refresh the mirror. Brief settle so the new screen shows.
    time.sleep(0.35)
    image = None
    try:
        image = _encode_frame(driver.get_screenshot_as_png())
    except Exception:
        pass
    return {"step": step, "element": el, "steps": s["steps"], "image": image}


class StepsBody(BaseModel):
    steps: List[str]


@router.put("/{sid}/steps")
def set_steps(sid: str, body: StepsBody, current_user=Depends(get_current_user)):
    """Replace the recorded steps (the UI lets the user edit before saving)."""
    s = _sess_or_404(sid)
    s["steps"] = [x for x in body.steps if x.strip()]
    return {"steps": s["steps"]}


@router.post("/{sid}/save")
def save(sid: str, body: SaveBody, current_user=Depends(get_current_user)):
    """Persist the recording as a SavedScenario."""
    s = _sess_or_404(sid)
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="Name is required.")
    with SessionLocal() as db:
        row = SavedScenario(
            name=body.name.strip(), description=body.description,
            project_id=s["project_id"], device_id=s["device_id"],
            steps=list(s["steps"]),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.to_dict()


@router.post("/{sid}/stop")
def stop(sid: str, current_user=Depends(get_current_user)):
    """End the recording session and release the Appium session."""
    s = _SESSIONS.pop(sid, None)
    if s:
        try:
            s["driver"].quit()
        except Exception:
            pass
    return {"stopped": True}
