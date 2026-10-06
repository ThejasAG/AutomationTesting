"""AI Agent page — the autonomous test agent (automation/agentic/).

Reading is for any signed-in user. Anything that runs, spends Claude budget,
changes flows or opens PRs is admin-only.
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from automation.agentic import orchestrator, preflight, report, scheduler, settings, sources
from automation.agentic import claude as claude_mod
from automation.agentic.fixer import FixRefused
from automation.auth.security import get_current_user, require_role
from automation.database.config import SessionLocal
from automation.database.models import AgentAction, AgentBatch, AgentItem

router = APIRouter(prefix="/agent", tags=["AI Agent"], dependencies=[Depends(get_current_user)])
_admin = Depends(require_role(["admin"]))


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _batch(b: AgentBatch) -> Dict[str, Any]:
    return {"id": b.id, "trigger": b.trigger, "env": b.env, "status": b.status,
            "started_at": _iso(b.started_at), "finished_at": _iso(b.finished_at),
            "totals": b.totals or {}, "spend_usd": round(b.spend_usd or 0.0, 4),
            "report_sent": bool(b.report_sent)}


def _item(i: AgentItem) -> Dict[str, Any]:
    return {"id": i.id, "position": i.position, "flow_id": i.flow_id, "flow_name": i.flow_name,
            "status": i.status, "run_id": i.run_id, "final_run_id": i.final_run_id,
            "attempts": i.attempts, "category": i.category, "root_cause": i.root_cause,
            "verdict": i.verdict, "started_at": _iso(i.started_at), "finished_at": _iso(i.finished_at)}


def _action(a: AgentAction, full: bool = False) -> Dict[str, Any]:
    det = dict(a.detail or {})
    if not full:
        det.pop("tools", None)
        det.pop("snapshot", None)
    return {"id": a.id, "batch_id": a.batch_id, "item_id": a.item_id, "run_id": a.run_id,
            "kind": a.kind, "status": a.status, "title": a.title, "detail": det,
            "cost_usd": round(a.cost_usd or 0.0, 4), "created_at": _iso(a.created_at)}


@router.get("/status")
def get_status():
    return {**orchestrator.status(), "next_run": scheduler.next_run(),
            "settings": settings.get(), "secrets": settings.secrets_status(),
            "apps": sources.apps_available(), "claude_ready": claude_mod.configured()}


class SettingsIn(BaseModel):
    changes: Dict[str, Any]


@router.put("/settings", dependencies=[_admin])
def put_settings(body: SettingsIn):
    ch = body.changes
    if "app_fix_mode" in ch and ch["app_fix_mode"] not in ("off", "propose", "pr"):
        raise HTTPException(400, "app_fix_mode must be off, propose or pr")
    if "nightly_time" in ch:
        try:
            h, m = (int(x) for x in str(ch["nightly_time"]).split(":"))
            assert 0 <= h < 24 and 0 <= m < 60
        except Exception:
            raise HTTPException(400, "nightly_time must be HH:MM")
    return settings.update(ch)


@router.get("/flows")
def get_flows():
    from automation.scenarios.cross_app_flows import list_flows
    return {"flows": [{"id": f["id"], "name": f["name"], "segments": len(f["segments"]),
                       "demo": f["id"] in settings.DEMO_FLOWS, "agent": f["id"].startswith("agent_")}
                      for f in list_flows()]}


@router.get("/preflight")
def get_preflight(env: str = "staging"):
    checks = preflight.run(env)
    return {"ok": preflight.all_ok(checks), "checks": checks}


class RunIn(BaseModel):
    flow_ids: Optional[List[str]] = None
    env: Optional[str] = None


@router.post("/run", dependencies=[_admin])
def run_now(body: RunIn):
    try:
        bid = orchestrator.start_batch("manual", body.flow_ids, body.env)
    except orchestrator.Busy as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"started": True, "batch_id": bid}


@router.post("/stop", dependencies=[_admin])
def stop():
    return {"stopping": orchestrator.stop()}


@router.get("/batches")
def list_batches(limit: int = 30):
    with SessionLocal() as db:
        rows = db.query(AgentBatch).order_by(AgentBatch.started_at.desc()).limit(limit).all()
        return {"batches": [_batch(b) for b in rows]}


@router.get("/batches/{batch_id}")
def get_batch(batch_id: str):
    with SessionLocal() as db:
        b = db.get(AgentBatch, batch_id)
        if not b:
            raise HTTPException(404, "No such batch")
        items = db.query(AgentItem).filter(AgentItem.batch_id == batch_id).order_by(AgentItem.position).all()
        acts = (db.query(AgentAction).filter(AgentAction.batch_id == batch_id)
                .order_by(AgentAction.created_at).all())
        return {"batch": _batch(b), "items": [_item(i) for i in items],
                "actions": [_action(a) for a in acts]}


@router.get("/actions")
def list_actions(kind: Optional[str] = None, status: Optional[str] = None, limit: int = 100):
    with SessionLocal() as db:
        q = db.query(AgentAction)
        if kind:
            q = q.filter(AgentAction.kind.in_(kind.split(",")))
        if status:
            q = q.filter(AgentAction.status.in_(status.split(",")))
        rows = q.order_by(AgentAction.created_at.desc()).limit(limit).all()
        return {"actions": [_action(a) for a in rows]}


@router.get("/actions/{action_id}")
def get_action(action_id: str):
    with SessionLocal() as db:
        a = db.get(AgentAction, action_id)
        if not a:
            raise HTTPException(404, "No such action")
        return _action(a, full=True)


@router.post("/analyze/{run_id}", dependencies=[_admin])
def analyze(run_id: str):
    try:
        orchestrator.analyze_run(run_id)
    except orchestrator.Busy as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"started": True}


class GenerateIn(BaseModel):
    focus: str = ""


@router.post("/generate", dependencies=[_admin])
def generate(body: GenerateIn):
    if not claude_mod.configured():
        raise HTTPException(400, "Set ANTHROPIC_API_KEY in .env first.")
    try:
        orchestrator.generate_scenarios(body.focus)
    except orchestrator.Busy as e:
        raise HTTPException(409, str(e))
    return {"started": True}


@router.post("/actions/{action_id}/approve", dependencies=[_admin])
def approve(action_id: str):
    with SessionLocal() as db:
        a = db.get(AgentAction, action_id)
        kind = a.kind if a else None
    if kind == "scenario_proposal":
        try:
            return {"approved": True, "flow_id": orchestrator.approve_proposal(action_id)}
        except ValueError as e:
            raise HTTPException(400, str(e))
    if kind == "app_fix":
        try:
            return {"approved": True, **orchestrator.open_pr_for(action_id)}
        except (FixRefused, ValueError) as e:
            raise HTTPException(400, str(e))
    raise HTTPException(400, "Only scenario proposals and app patches can be approved.")


@router.get("/inventory")
def get_inventory():
    """The computed coverage map (no AI): every screen of both apps, its ids, and
    which the automation drives. A few seconds; recomputed on each call."""
    from automation.agentic import inventory
    return orchestrator.inventory_summary(inventory.build())


@router.get("/analysis")
def latest_analysis():
    """The most recent Claude analysis of the application (features + coverage)."""
    with SessionLocal() as db:
        a = (db.query(AgentAction).filter(AgentAction.kind == "app_analysis")
             .order_by(AgentAction.created_at.desc()).first())
        return {"analysis": _action(a) if a else None}


@router.post("/actions/{action_id}/approve-test", dependencies=[_admin])
def approve_and_test(action_id: str):
    try:
        return orchestrator.approve_and_test(action_id)
    except orchestrator.Busy as e:
        raise HTTPException(409, f"Approved, but not run now: {e}")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/actions/{action_id}/reject", dependencies=[_admin])
def reject(action_id: str):
    with SessionLocal() as db:
        a = db.get(AgentAction, action_id)
        if not a or a.status not in ("proposed",):
            raise HTTPException(400, "Only a pending proposal can be rejected.")
        a.status = "rejected"
        db.commit()
    return {"rejected": True}


@router.post("/actions/{action_id}/rollback", dependencies=[_admin])
def rollback(action_id: str):
    try:
        orchestrator.rollback_fix(action_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"rolled_back": True}


@router.post("/batches/{batch_id}/report", dependencies=[_admin])
def resend_report(batch_id: str):
    return report.send(batch_id)


@router.get("/batches/{batch_id}/report", response_model=None)
def preview_report(batch_id: str):
    from fastapi.responses import HTMLResponse
    return HTMLResponse(report.build(batch_id)["html"])
