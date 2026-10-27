"""AI Agent page — Claude Code sessions (automation/claude_code/).

Reading is for any signed-in user. Starting, stopping, approving and changing
settings are admin-only: they drive the simulators and change the platform's code.
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from automation.agentic import preflight, settings, sources
from automation.auth.security import get_current_user, require_role
from automation.claude_code import schedule, session

router = APIRouter(prefix="/agent", tags=["AI Agent"], dependencies=[Depends(get_current_user)])
_admin = Depends(require_role(["admin"]))


def _who(user) -> str:
    return getattr(user, "email", "") or getattr(user, "username", "") or ""


@router.get("/status")
def get_status():
    s = settings.get()
    try:
        sched = schedule.installed()
    except Exception as e:  # noqa: BLE001 — launchctl missing on a non-Mac
        sched = {"installed": False, "error": str(e)}
    return {"claude": session.claude_status(), "active": session.active(), "settings": s,
            "schedule": sched, "secrets": settings.secrets_status(), "apps": sources.apps_available(),
            "hooks": session.recent_hooks(5)}


class SettingsIn(BaseModel):
    changes: Dict[str, Any]


@router.put("/settings", dependencies=[_admin])
def put_settings(body: SettingsIn):
    ch = dict(body.changes)
    if "nightly_time" in ch:
        try:
            h, m = (int(x) for x in str(ch["nightly_time"]).split(":"))
            assert 0 <= h < 24 and 0 <= m < 60
            ch["nightly_time"] = f"{h:02d}:{m:02d}"
        except Exception:
            raise HTTPException(400, "nightly_time must be HH:MM")
    if "nightly_mode" in ch and ch["nightly_mode"] not in ("auto", "delta", "full"):
        raise HTTPException(400, "nightly_mode must be auto, delta or full")
    for k in ("max_new_scenarios", "max_fix_attempts", "max_changed_lines"):
        if k in ch:
            try:
                ch[k] = max(1, int(ch[k]))
            except (TypeError, ValueError):
                raise HTTPException(400, f"{k} must be a number")
    s = settings.update(ch)
    out: Dict[str, Any] = {"settings": s}
    if {"nightly_enabled", "nightly_time"} & set(ch):
        try:
            out["schedule"] = schedule.sync(s)
        except Exception as e:  # noqa: BLE001
            out["schedule"] = {"error": str(e)}
    return out


@router.get("/flows")
def get_flows():
    from automation.scenarios.cross_app_flows import list_flows
    return {"flows": [{"id": f["id"], "name": f["name"], "segments": len(f["segments"]),
                       "demo": f["id"] in settings.DEMO_FLOWS, "draft": f["id"].startswith("cc_")}
                      for f in list_flows()]}


@router.get("/preflight")
def get_preflight(env: str = "staging"):
    checks = preflight.run(env)
    return {"ok": preflight.all_ok(checks), "checks": checks}


@router.get("/inventory")
def get_inventory():
    """The computed coverage map (no AI): every screen of both apps, its ids, and
    which the automation drives. A few seconds; recomputed on each call."""
    from automation.agentic import inventory, runinfo
    return runinfo.inventory_summary(inventory.build())


@router.get("/feature-map")
def get_feature_map():
    """The latest Claude analysis of both apps (features rated covered/partial/missing)."""
    return {"feature_map": session.latest_feature_map()}


# ── sessions ─────────────────────────────────────────────────────────────────

class SessionIn(BaseModel):
    kind: str                                   # heal | fix_run | analyze | generate
    run_ids: Optional[List[str]] = None
    flow_ids: Optional[List[str]] = None
    focus: str = ""
    features: Optional[List[str]] = None
    mode: Optional[str] = None
    max_new: Optional[int] = None


@router.post("/sessions", dependencies=[_admin])
def start_session(body: SessionIn, current_user=Depends(get_current_user)):
    opts: Dict[str, Any] = {}
    for k in ("run_ids", "flow_ids", "features", "mode", "max_new"):
        v = getattr(body, k)
        if v:
            opts[k] = v
    if body.focus.strip():
        opts["focus"] = body.focus.strip()
    try:
        sid = session.create(body.kind, trigger="manual", options=opts, created_by=_who(current_user))
    except session.Busy as e:
        raise HTTPException(409, str(e))
    except (session.Refused, ValueError) as e:
        raise HTTPException(400, str(e))
    return {"started": True, "id": sid}


@router.get("/sessions")
def list_sessions(limit: int = 30):
    session.active()                            # closes rows whose runner died
    return {"sessions": session.list_sessions(limit)}


@router.get("/sessions/{sid}")
def get_session(sid: str):
    s = session.get(sid)
    if s is None:
        raise HTTPException(404, "No such session")
    return {**s, "report": session.read_file(sid, "report.md"), "runs": session.runs(sid),
            "app_patches": session.app_patches(sid)}


@router.get("/sessions/{sid}/events")
def get_events(sid: str, after: int = 0):
    try:
        return session.session_events(sid, after)
    except ValueError:
        raise HTTPException(404, "No such session")


@router.get("/sessions/{sid}/diff")
def get_diff(sid: str):
    if session.get(sid) is None:
        raise HTTPException(404, "No such session")
    return {"diff": session.read_file(sid, "changes.patch") or session.read_file(sid, "stopped.patch")}


@router.post("/sessions/{sid}/stop", dependencies=[_admin])
def stop_session(sid: str):
    return {"stopping": session.stop(sid)}


@router.post("/sessions/{sid}/continue", dependencies=[_admin])
def continue_session(sid: str, current_user=Depends(get_current_user)):
    try:
        return {"id": session.continue_session(sid, _who(current_user))}
    except ValueError as e:
        raise HTTPException(404, str(e))
    except session.Busy as e:
        raise HTTPException(409, str(e))
    except session.Refused as e:
        raise HTTPException(400, str(e))


@router.post("/sessions/{sid}/approve", dependencies=[_admin])
def approve_session(sid: str, current_user=Depends(get_current_user)):
    try:
        return session.approve(sid, _who(current_user))
    except ValueError as e:
        raise HTTPException(404, str(e))
    except session.Refused as e:
        raise HTTPException(400, str(e))


@router.post("/sessions/{sid}/reject", dependencies=[_admin])
def reject_session(sid: str, current_user=Depends(get_current_user)):
    try:
        return session.reject(sid, _who(current_user))
    except ValueError as e:
        raise HTTPException(404, str(e))
    except session.Refused as e:
        raise HTTPException(400, str(e))


@router.post("/restart-backend", dependencies=[_admin])
def restart_backend():
    """Load approved changes: restart the backend, only when nothing is running."""
    try:
        return session.restart_backend_if_idle()
    except session.Busy as e:
        raise HTTPException(409, str(e))
    except session.Refused as e:
        raise HTTPException(400, str(e))
