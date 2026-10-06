"""The batch: every flow, one at a time, with failures handled end to end.

For each flow:
  1. preflight — Appium, simulators, staging. Staging 502 is waited out (up to
     infra_wait_minutes); if the rig never recovers the item is INFRA, not failed.
  2. run it (start_flow_run) and wait for the verdict.
  3. failed → rule sorter (classifier.py, free):
       INFRA  → wait for the rig, retry the whole flow once
       TIMING → resume from the failed segment once
  4. still failing (and not INFRA) → Claude diagnoses it (triage.py), within the
     batch budget. Then:
       test fix proposed → apply as a flow edit, re-run to verify; keep it if the
                           flow passes, roll it back if not (max_fix_attempts)
       app patch proposed → checked + saved; opened as a draft PR when
                           app_fix_mode == 'pr'
       flaky / retry      → one more resume
  5. record the outcome; after the last flow, send the report.

One batch at a time (the simulators are shared). State lives in agent_batches /
agent_items / agent_actions, so a backend restart continues the batch instead of
losing the night.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from automation.agentic import claude, classifier, fixer, preflight, settings
from automation.database.config import SessionLocal
from automation.database.models import (AgentAction, AgentBatch, AgentItem, ScenarioResult,
                                        TestRun)

logger = logging.getLogger("agentic.orchestrator")

RUN_TIMEOUT_S = 60 * 60          # a single flow attempt
POLL_S = 5
MAX_AUTO_CONTINUES = 3           # restarts a batch may survive before it is abandoned

_lock = threading.Lock()
_state: Dict[str, Any] = {"batch_id": None, "run_id": None, "stop": threading.Event(),
                          "phase": "", "busy": None}


class Busy(Exception):
    pass


# ── small DB helpers ─────────────────────────────────────────────────────────

def _set(model, row_id: str, **kw) -> None:
    with SessionLocal() as db:
        row = db.get(model, row_id)
        if row is None:
            return
        for k, v in kw.items():
            setattr(row, k, v)
        db.commit()


def _action(kind: str, title: str, *, batch_id=None, item_id=None, run_id=None,
            status: str = "done", detail: Optional[Dict[str, Any]] = None,
            cost: float = 0.0) -> str:
    with SessionLocal() as db:
        a = AgentAction(kind=kind, title=title[:500], batch_id=batch_id, item_id=item_id,
                        run_id=run_id, status=status, detail=detail or {}, cost_usd=cost)
        db.add(a)
        db.commit()
        return a.id


def _add_spend(batch_id: Optional[str], cost: float) -> None:
    if not batch_id or not cost:
        return
    with SessionLocal() as db:
        b = db.get(AgentBatch, batch_id)
        if b:
            b.spend_usd = (b.spend_usd or 0.0) + cost
            db.commit()


def _spent(batch_id: Optional[str]) -> float:
    if not batch_id:
        return 0.0
    with SessionLocal() as db:
        b = db.get(AgentBatch, batch_id)
        return (b.spend_usd or 0.0) if b else 0.0


def _phase(text: str) -> None:
    _state["phase"] = text
    logger.info("agent: %s", text)


# ── public status ────────────────────────────────────────────────────────────

def status() -> Dict[str, Any]:
    return {"batch_id": _state["batch_id"], "run_id": _state["run_id"],
            "phase": _state["phase"], "busy": _state["busy"],
            "running": _state["batch_id"] is not None or _state["busy"] is not None}


def flows_for(s: Dict[str, Any]) -> List[Dict[str, Any]]:
    from automation.scenarios.cross_app_flows import list_flows
    every = list_flows()
    if s.get("flows"):
        wanted = set(s["flows"])
        return [f for f in every if f["id"] in wanted]
    return [f for f in every if s.get("include_demos") or f["id"] not in settings.DEMO_FLOWS]


# ── running one flow attempt ─────────────────────────────────────────────────

def _wait_for(run_id: str) -> str:
    from automation.scenarios.cross_app_flows import request_flow_stop
    _state["run_id"] = run_id
    deadline = time.time() + RUN_TIMEOUT_S
    asked_stop = False
    try:
        while time.time() < deadline:
            if _state["stop"].is_set() and not asked_stop:
                request_flow_stop(run_id)
                asked_stop = True
                deadline = min(deadline, time.time() + 180)
            with SessionLocal() as db:
                r = db.get(TestRun, run_id)
                st = (r.status or "") if r else "missing"
            if st not in ("running", "queued", ""):
                return st
            time.sleep(POLL_S)
        request_flow_stop(run_id)
        _set(TestRun, run_id, status="failed",
             error_message="Agent: the run exceeded 60 minutes and was stopped.")
        return "failed"
    finally:
        _state["run_id"] = None


def run_flow(flow_id: str, env: str, resume_from: Optional[str] = None) -> Tuple[str, str]:
    """Start one attempt and wait. Returns (run_id, status). resume_from = a failed
    run to continue from its failed segment (falls back to a full run)."""
    from automation.scenarios.cross_app_flows import list_flows, resume_plan, start_flow_run
    plan = None
    if resume_from:
        flow = next((f for f in list_flows() if f["id"] == flow_id), None)
        with SessionLocal() as db:
            rows = db.query(ScenarioResult).filter(ScenarioResult.run_id == resume_from).all()
            for r in rows:
                db.expunge(r)
        try:
            plan = resume_plan(flow, rows) if flow else None
            if plan is not None:
                plan["from_run"] = resume_from
        except ValueError:
            plan = None
    run_id = start_flow_run(flow_id, env=env, resume=plan)
    return run_id, _wait_for(run_id)


def _load(run_id: str) -> Tuple[Any, List[Any]]:
    with SessionLocal() as db:
        run = db.get(TestRun, run_id)
        rows = db.query(ScenarioResult).filter(ScenarioResult.run_id == run_id).all()
        if run is not None:
            db.expunge(run)
        for r in rows:
            db.expunge(r)
    return run, rows


def _rig_ready(env: str, wait_minutes: float) -> Tuple[bool, List[Dict[str, Any]]]:
    deadline = time.time() + max(0.0, wait_minutes) * 60
    while True:
        checks = preflight.run(env)
        if preflight.all_ok(checks) or time.time() >= deadline or _state["stop"].is_set():
            return preflight.all_ok(checks), checks
        _phase("waiting for the rig: " + "; ".join(
            f"{c['name']} {c['detail']}" for c in checks if not c["ok"]))
        time.sleep(60)


# ── one item ─────────────────────────────────────────────────────────────────

def _process(item: AgentItem, batch: AgentBatch, s: Dict[str, Any]) -> None:
    bid, iid, fid, env = batch.id, item.id, item.flow_id, batch.env
    _set(AgentItem, iid, status="running", started_at=datetime.utcnow())

    _phase(f"{item.flow_name}: preflight")
    ok, checks = _rig_ready(env, s["infra_wait_minutes"])
    if not ok:
        why = "; ".join(f"{c['name']}: {c['detail']}" for c in checks if not c["ok"])
        _action("preflight", f"Rig not ready — {why}", batch_id=bid, item_id=iid,
                status="failed", detail={"checks": checks})
        _set(AgentItem, iid, status="infra", category="INFRA", root_cause=why,
             finished_at=datetime.utcnow())
        return

    _phase(f"{item.flow_name}: running")
    run_id, st = run_flow(fid, env)
    _set(AgentItem, iid, run_id=run_id, final_run_id=run_id, attempts=1)
    if st == "passed":
        _set(AgentItem, iid, status="passed", finished_at=datetime.utcnow())
        return
    if _state["stop"].is_set():
        _set(AgentItem, iid, status="stopped", finished_at=datetime.utcnow())
        return

    attempts = 1
    run, rows = _load(run_id)
    verdict = classifier.classify(run, rows)
    _set(AgentItem, iid, category=verdict["category"], root_cause=verdict["summary"])

    # Cheap first response from the rules.
    policy = classifier.first_policy(verdict["category"])
    if policy:
        if policy == "retry":
            _phase(f"{item.flow_name}: infra failure — waiting for the rig, then retrying")
            ok, checks = _rig_ready(env, s["infra_wait_minutes"])
            if not ok:
                _set(AgentItem, iid, status="infra", finished_at=datetime.utcnow(),
                     root_cause=verdict["summary"] + " | rig still not ready: " + "; ".join(
                         f"{c['name']} {c['detail']}" for c in checks if not c["ok"]))
                return
        _phase(f"{item.flow_name}: {policy} after {verdict['category']}")
        new_id, st = run_flow(fid, env, resume_from=run_id if policy == "resume" else None)
        attempts += 1
        _action(policy, f"{policy.title()} after {verdict['category']}: {verdict['matched']}",
                batch_id=bid, item_id=iid, run_id=new_id,
                status="verified" if st == "passed" else "failed",
                detail={"from_run": run_id, "result": st, "rule": verdict})
        _set(AgentItem, iid, final_run_id=new_id, attempts=attempts)
        if st == "passed":
            _set(AgentItem, iid, status="passed_on_retry", finished_at=datetime.utcnow())
            return
        run_id = new_id
        run, rows = _load(run_id)
        verdict = classifier.classify(run, rows)
        _set(AgentItem, iid, category=verdict["category"], root_cause=verdict["summary"])

    if _state["stop"].is_set():
        _set(AgentItem, iid, status="stopped", finished_at=datetime.utcnow())
        return
    if verdict["category"] == "INFRA":
        _set(AgentItem, iid, status="infra", finished_at=datetime.utcnow())
        return

    final = _investigate(bid, iid, fid, env, run_id, verdict, s, attempts, item.flow_name)
    _set(AgentItem, iid, status=final, finished_at=datetime.utcnow())


def _investigate(bid, iid, fid, env, run_id, verdict, s, attempts, flow_name) -> str:
    """Claude diagnoses; safe fixes are applied and verified. Returns the item status."""
    from automation.agentic import triage
    if not s.get("triage"):
        return "failed"
    if not claude.configured():
        _action("triage", "Skipped Claude diagnosis — ANTHROPIC_API_KEY is not set",
                batch_id=bid, item_id=iid, run_id=run_id, status="failed")
        return "failed"

    tried_fix_failed = False
    retried = False
    for attempt in range(max(1, int(s["max_fix_attempts"]))):
        left = s["budget_usd"] - _spent(bid)
        if bid and left <= 0:
            _action("triage", "Skipped — the batch's Claude budget is used up",
                    batch_id=bid, item_id=iid, run_id=run_id, status="failed")
            return "failed"
        _phase(f"{flow_name}: Claude is diagnosing run {run_id[:8]}")
        lr = triage.diagnose(run_id, fid, verdict, model=s["model"],
                             budget_left_usd=left if bid else s["budget_usd"])
        _add_spend(bid, lr.cost_usd)
        d = lr.submitted or {}
        _action("triage", (f"{d.get('category', 'No verdict')} ({d.get('confidence', '-')}): "
                           f"{d.get('root_cause', lr.error or lr.stop)}"),
                batch_id=bid, item_id=iid, run_id=run_id,
                status="done" if lr.submitted else "failed",
                detail={"verdict": d, "stop": lr.stop, "error": lr.error, "turns": lr.turns,
                        "tools": lr.tool_log, "previous_fix_failed": tried_fix_failed},
                cost=lr.cost_usd)
        if not lr.submitted:
            return "failed"
        _set(AgentItem, iid, verdict=d, category=d.get("category"), root_cause=d.get("root_cause"))

        _propose_app_fix(bid, iid, run_id, d, s, flow_name)

        action = d.get("recommended_action")
        if d.get("category") == "INFRA" or action == "report_infra":
            return "infra"
        if action == "retry" and not retried:
            retried = True
            new_id, st = run_flow(fid, env, resume_from=run_id)
            _action("resume", "Retry recommended by Claude", batch_id=bid, item_id=iid,
                    run_id=new_id, status="verified" if st == "passed" else "failed",
                    detail={"from_run": run_id, "result": st})
            _set(AgentItem, iid, final_run_id=new_id, attempts=attempts + 1)
            attempts += 1
            if st == "passed":
                return "passed_on_retry"
            run_id = new_id
            continue
        if action == "fix_test" and s.get("autofix_tests"):
            fix = d.get("test_fix") or {}
            try:
                snapshot = fixer.apply_test_fix(fid, fix)
            except fixer.FixRefused as e:
                _action("test_fix", f"Fix not applied: {e}", batch_id=bid, item_id=iid,
                        run_id=run_id, status="rejected", detail={"fix": fix})
                return "failed"
            aid = _action("test_fix", f"Applied: segment {fix['segment_num']} "
                                      f"'{fix['old_step']}' → {fix['new_steps']}",
                          batch_id=bid, item_id=iid, run_id=run_id, status="applied",
                          detail={"flow_id": fid, "fix": fix, "snapshot": snapshot})
            _phase(f"{flow_name}: verifying the test fix")
            new_id, st = run_flow(fid, env)
            attempts += 1
            _set(AgentItem, iid, final_run_id=new_id, attempts=attempts)
            if st == "passed":
                _set(AgentAction, aid, status="verified",
                     detail={"flow_id": fid, "fix": fix, "snapshot": snapshot, "verify_run": new_id})
                return "fixed"
            fixer.rollback(fid, snapshot)
            _set(AgentAction, aid, status="rolled_back",
                 detail={"flow_id": fid, "fix": fix, "snapshot": snapshot, "verify_run": new_id})
            tried_fix_failed = True
            run_id = new_id
            run, rows = _load(run_id)
            verdict = classifier.classify(run, rows)
            continue
        break
    final_cat = _get_item_category(iid)
    return "app_bug" if final_cat == "APP_BUG" else "failed"


def _get_item_category(iid: str) -> Optional[str]:
    with SessionLocal() as db:
        it = db.get(AgentItem, iid)
        return it.category if it else None


def _propose_app_fix(bid, iid, run_id, d, s, flow_name) -> None:
    af = d.get("app_fix") or {}
    patch, app = (af.get("patch") or "").strip(), af.get("app")
    if not patch or app in (None, "none") or s.get("app_fix_mode") == "off":
        return
    ok, why = fixer.check_patch(app, patch)
    aid = _action("app_fix", f"App patch ({app}) for {flow_name}: {af.get('explanation', '')[:200]}",
                  batch_id=bid, item_id=iid, run_id=run_id,
                  status="proposed" if ok else "rejected",
                  detail={"app": app, "patch": patch, "explanation": af.get("explanation"),
                          "check": why, "root_cause": d.get("root_cause")})
    if ok:
        path = fixer.save_patch(aid, patch)
        _set(AgentAction, aid, detail={"app": app, "patch": patch, "explanation": af.get("explanation"),
                                       "check": why, "root_cause": d.get("root_cause"),
                                       "patch_file": path})
        if s.get("app_fix_mode") == "pr":
            open_pr_for(aid)


def open_pr_for(action_id: str) -> Dict[str, Any]:
    with SessionLocal() as db:
        a = db.get(AgentAction, action_id)
        if not a or a.kind != "app_fix":
            raise ValueError("not an app fix")
        det = dict(a.detail or {})
    title = f"[test-agent] {det.get('explanation') or 'Fix found by the nightly test agent'}"[:120]
    body = (f"Proposed by the Vya test agent after a failed end-to-end run.\n\n"
            f"**Root cause:** {det.get('root_cause') or '-'}\n\n"
            f"**Change:** {det.get('explanation') or '-'}\n\n"
            f"Run: {a.run_id}\n\nReview before merging — the agent does not merge.")
    try:
        pr = fixer.open_pull_request(det["app"], det["patch"], title, body)
    except fixer.FixRefused as e:
        det["pr_error"] = str(e)
        _set(AgentAction, action_id, detail=det)
        raise
    det.update(pr)
    _set(AgentAction, action_id, status="approved", detail=det)
    return pr


# ── the batch ────────────────────────────────────────────────────────────────

def start_batch(trigger: str = "manual", flow_ids: Optional[List[str]] = None,
                env: Optional[str] = None) -> str:
    s = settings.get()
    with _lock:
        if _state["batch_id"] or _state["busy"]:
            raise Busy("The agent is already working — stop it first.")
        flows = flows_for({**s, "flows": flow_ids or s["flows"]})
        if not flows:
            raise ValueError("No flows selected.")
        with SessionLocal() as db:
            b = AgentBatch(trigger=trigger, env=env or s["env"], status="running",
                           started_at=datetime.utcnow(), spend_usd=0.0, totals={},
                           notes="continues=0")
            db.add(b)
            db.flush()
            for i, f in enumerate(flows):
                db.add(AgentItem(batch_id=b.id, position=i, flow_id=f["id"], flow_name=f["name"],
                                 status="pending", attempts=0))
            db.commit()
            bid = b.id
        _state["batch_id"] = bid
        _state["stop"] = threading.Event()
    threading.Thread(target=_run_batch, args=(bid,), name=f"agent-batch-{bid[:8]}",
                     daemon=True).start()
    return bid


def stop() -> bool:
    if not (_state["batch_id"] or _state["busy"]):
        return False
    _state["stop"].set()
    return True


def _run_batch(bid: str) -> None:
    try:
        s = settings.get()
        while True:
            with SessionLocal() as db:
                batch = db.get(AgentBatch, bid)
                item = (db.query(AgentItem).filter(AgentItem.batch_id == bid,
                                                   AgentItem.status == "pending")
                        .order_by(AgentItem.position).first())
                if batch is not None:
                    db.expunge(batch)
                if item is not None:
                    db.expunge(item)
            if item is None:
                break
            if _state["stop"].is_set():
                with SessionLocal() as db:
                    (db.query(AgentItem).filter(AgentItem.batch_id == bid,
                                                AgentItem.status == "pending")
                     .update({"status": "stopped"}))
                    db.commit()
                break
            try:
                _process(item, batch, s)
            except Exception as e:  # one broken item must not end the night
                logger.exception("agent item %s crashed", item.flow_id)
                _action("error", f"{item.flow_name}: agent error {e}", batch_id=bid, item_id=item.id,
                        status="failed")
                _set(AgentItem, item.id, status="failed", root_cause=f"agent error: {e}",
                     finished_at=datetime.utcnow())
        _finish(bid)
    finally:
        _state["batch_id"] = None
        _state["phase"] = ""


def _finish(bid: str) -> None:
    from automation.agentic import report
    with SessionLocal() as db:
        items = db.query(AgentItem).filter(AgentItem.batch_id == bid).all()
        totals: Dict[str, int] = {}
        for it in items:
            totals[it.status] = totals.get(it.status, 0) + 1
        b = db.get(AgentBatch, bid)
        b.totals = totals
        b.status = "stopped" if _state["stop"].is_set() else "completed"
        b.finished_at = datetime.utcnow()
        db.commit()
    _phase("sending the report")
    try:
        report.send(bid)
    except Exception as e:
        logger.warning("agent report failed: %s", e)


def continue_after_restart() -> None:
    """Called at backend startup: pick an interrupted batch back up. The flow that
    was running died with the process (and was reaped), so it runs again."""
    with SessionLocal() as db:
        b = (db.query(AgentBatch).filter(AgentBatch.status == "running")
             .order_by(AgentBatch.started_at.desc()).first())
        if b is None:
            return
        n = int((b.notes or "continues=0").split("=")[-1] or 0)
        if n >= MAX_AUTO_CONTINUES:
            b.status = "interrupted"
            b.finished_at = datetime.utcnow()
            db.commit()
            return
        b.notes = f"continues={n + 1}"
        (db.query(AgentItem).filter(AgentItem.batch_id == b.id, AgentItem.status == "running")
         .update({"status": "pending"}))
        for other in db.query(AgentBatch).filter(AgentBatch.status == "running",
                                                 AgentBatch.id != b.id):
            other.status = "interrupted"
        db.commit()
        bid = b.id
    with _lock:
        _state["batch_id"] = bid
        _state["stop"] = threading.Event()
    logger.info("agent: continuing batch %s after a restart", bid)
    threading.Thread(target=_run_batch, args=(bid,), name=f"agent-batch-{bid[:8]}",
                     daemon=True).start()


# ── on-demand work (dashboard buttons) ───────────────────────────────────────

def _flow_id_for_run(run: Any) -> Optional[str]:
    import re as _re
    from automation.scenarios.cross_app_flows import list_flows
    name = _re.sub(r"^\[[^\]]*\]\s*", "", run.test_name or "").strip()
    f = next((f for f in list_flows() if f["name"] == name), None)
    return f["id"] if f else None


def _background(label: str, fn) -> None:
    with _lock:
        if _state["batch_id"] or _state["busy"]:
            raise Busy("The agent is already working — stop it first.")
        _state["busy"] = label
        _state["stop"] = threading.Event()

    def _go():
        try:
            fn()
        except Exception as e:
            logger.exception("agent %s failed", label)
            _action("error", f"{label} failed: {e}", status="failed")
        finally:
            _state["busy"] = None
            _state["phase"] = ""

    threading.Thread(target=_go, name=f"agent-{label}", daemon=True).start()


def analyze_run(run_id: str) -> None:
    """Diagnose (and, if allowed, fix) one failed run picked on the dashboard."""
    run, rows = _load(run_id)
    if run is None:
        raise ValueError(f"No run {run_id}")
    if run.bot_type != "ios-crossapp-flow":
        raise ValueError("The agent analyses cross-app flow runs.")
    fid = _flow_id_for_run(run)
    if not fid:
        raise ValueError("Could not tell which flow this run executed.")
    env = "staging" if "staging" in (run.test_name or "").lower() else "prod"

    def _work():
        s = settings.get()
        verdict = classifier.classify(run, rows)
        with SessionLocal() as db:
            it = AgentItem(batch_id=_adhoc_batch(env), position=0, flow_id=fid,
                           flow_name=run.test_name, status="running", run_id=run_id,
                           final_run_id=run_id, attempts=1, category=verdict["category"],
                           root_cause=verdict["summary"], started_at=datetime.utcnow())
            db.add(it)
            db.commit()
            iid, bid = it.id, it.batch_id
        final = _investigate(bid, iid, fid, env, run_id, verdict, s, 1, run.test_name)
        _set(AgentItem, iid, status=final, finished_at=datetime.utcnow())
        _finish_adhoc(bid)

    _background(f"analyze {run_id[:8]}", _work)


def _adhoc_batch(env: str) -> str:
    with SessionLocal() as db:
        b = AgentBatch(trigger="analyze", env=env, status="running", spend_usd=0.0,
                       started_at=datetime.utcnow(), totals={}, notes="continues=99")
        db.add(b)
        db.commit()
        return b.id


def _finish_adhoc(bid: str) -> None:
    with SessionLocal() as db:
        b = db.get(AgentBatch, bid)
        items = db.query(AgentItem).filter(AgentItem.batch_id == bid).all()
        b.totals = {it.status: 1 for it in items}
        b.status, b.finished_at = "completed", datetime.utcnow()
        db.commit()


def inventory_summary(inv: Dict[str, Any]) -> Dict[str, Any]:
    """The coverage map, trimmed for storage and the dashboard."""
    out: Dict[str, Any] = {}
    for app, a in inv["apps"].items():
        out[app] = {k: a[k] for k in ("screens", "screens_covered", "ids", "ids_covered")}
        out[app]["routes"] = [{"route": r["route"], "file": r["file"], "covered": r["covered"],
                               "ids": len(r["ids"]), "covered_ids": r["covered_ids"][:10]}
                              for r in a["routes"]]
    return out


def generate_scenarios(focus: str = "") -> None:
    """Analyze the whole application and propose scenarios for what is not tested."""
    from automation.agentic import generator, inventory
    from automation.scenarios.cross_app_flows import list_flows

    def _work():
        s = settings.get()
        _phase("Mapping every screen of both apps and what the tests cover")
        inv = inventory.build()
        with SessionLocal() as db:
            pending = [a.title for a in db.query(AgentAction).filter(
                AgentAction.kind == "scenario_proposal", AgentAction.status == "proposed")]
        _phase("Claude is analysing the application and writing scenarios for the gaps")
        lr = generator.analyze(model=s["model"], budget_left_usd=s["budget_usd"], focus=focus,
                               inv=inv, pending=pending)
        out = lr.submitted or {}
        _action("app_analysis",
                f"Application analysis: {len(out.get('features') or [])} features, "
                f"{len(out.get('scenarios') or [])} new scenarios",
                status="done" if lr.submitted else "failed",
                detail={"summary": out.get("app_summary", ""), "features": out.get("features") or [],
                        "coverage": inventory_summary(inv), "focus": focus, "stop": lr.stop,
                        "error": lr.error, "turns": lr.turns, "tools": lr.tool_log},
                cost=lr.cost_usd)
        taken = {f["id"] for f in list_flows()} | {generator.slug(t) for t in pending}
        for sc in out.get("scenarios") or []:
            fid = generator.slug(sc.get("name", ""))
            problems = generator.validate(sc)
            if fid in taken:
                problems.append("a flow or proposal with this name already exists")
            taken.add(fid)
            _action("scenario_proposal", sc.get("name", "Untitled scenario"),
                    status="rejected" if problems else "proposed",
                    detail={"flow": sc, "flow_id": fid, "feature": sc.get("feature", ""),
                            "problems": problems})

    _background("analyze app", _work)


def approve_and_test(action_id: str) -> Dict[str, str]:
    """Approve a proposal, then run it once now as a one-flow batch."""
    fid = approve_proposal(action_id)
    bid = start_batch("trial", [fid])
    return {"flow_id": fid, "batch_id": bid}


def approve_proposal(action_id: str) -> str:
    from automation.agentic import generator
    from automation.database.models import CrossAppFlowEdit
    with SessionLocal() as db:
        a = db.get(AgentAction, action_id)
        if not a or a.kind != "scenario_proposal":
            raise ValueError("not a scenario proposal")
        flow = (a.detail or {}).get("flow") or {}
        problems = generator.validate(flow)
        if problems:
            raise ValueError("; ".join(problems))
        fid = (a.detail or {}).get("flow_id") or generator.slug(flow.get("name", ""))
        segs = [{"num": str(i + 1), "name": sg["name"], "role": sg["role"],
                 "steps": [x.strip() for x in sg["steps"] if x.strip()]}
                for i, sg in enumerate(flow["segments"])]
        row = db.get(CrossAppFlowEdit, fid) or CrossAppFlowEdit(id=fid)
        row.name, row.description, row.segments = flow["name"], flow.get("description", ""), segs
        db.merge(row)
        a.status = "approved"
        db.commit()
    return fid


def rollback_fix(action_id: str) -> None:
    with SessionLocal() as db:
        a = db.get(AgentAction, action_id)
        if not a or a.kind != "test_fix" or a.status not in ("applied", "verified"):
            raise ValueError("only an applied test fix can be rolled back")
        det = dict(a.detail or {})
    fixer.rollback(det["flow_id"], det["snapshot"])
    _set(AgentAction, action_id, status="rolled_back")
