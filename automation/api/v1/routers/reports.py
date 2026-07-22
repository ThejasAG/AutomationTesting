"""Test Reports — a proper, stored report for every run.

Aggregates a run's metadata, per-scenario results, and root-cause analysis into
one report, and generates a plain-English narrative ("what happened, why, what to
do") via the local Ollama model (the same no-key provider the RCA/Chat features
use). Reports are stored on the run and can be regenerated or exported.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from automation.auth.security import get_current_user
from automation.database.config import get_db
from automation.database.models import TestRun, ScenarioResult, RCAReport

logger = logging.getLogger("reports")
router = APIRouter(prefix="/reports", tags=["Test Reports"])


def _scenarios(db: Session, run_id: str) -> List[ScenarioResult]:
    return (db.query(ScenarioResult)
            .filter(ScenarioResult.run_id == run_id)
            .order_by(ScenarioResult.scenario_num).all())


def _run_summary(db: Session, r: TestRun) -> Dict[str, Any]:
    scs = _scenarios(db, r.id)
    passed = sum(1 for s in scs if (s.status or "").upper() in ("PASS", "PASSED"))
    return {
        "id": r.id,
        "test_name": r.test_name,
        "test_suite": r.test_suite,
        "status": r.status,
        "device_name": r.device_name,
        "platform": r.platform,
        "branch": r.branch,
        "commit_sha": r.commit_sha,
        "triggered_by": r.triggered_by,
        "duration_ms": r.duration_ms,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "scenarios_total": len(scs),
        "scenarios_passed": passed,
        "has_report": bool(r.report_summary),
    }


@router.get("")
def list_reports(limit: int = 200, db: Session = Depends(get_db),
                 current_user=Depends(get_current_user)):
    """All runs as report rows, newest first."""
    runs = db.query(TestRun).order_by(TestRun.created_at.desc()).limit(limit).all()
    return [_run_summary(db, r) for r in runs]


def _is_pass(status: str) -> bool:
    return (status or "").lower() in ("passed", "completed")


def _is_fail(status: str) -> bool:
    return (status or "").lower() in ("failed", "error")


@router.get("/trends")
def trends(days: int = 30, db: Session = Depends(get_db),
           current_user=Depends(get_current_user)):
    """Pass-rate over time, per-environment (project) breakdown, and flaky tests."""
    from collections import defaultdict
    since = datetime.utcnow() - timedelta(days=days)
    runs = db.query(TestRun).filter(TestRun.created_at >= since).all()

    daily: Dict[str, Dict[str, int]] = defaultdict(lambda: {"passed": 0, "failed": 0})
    per_env: Dict[str, Dict[str, int]] = defaultdict(lambda: {"passed": 0, "failed": 0})
    outcomes: Dict[str, List[bool]] = defaultdict(list)   # test_name -> [pass?, …]
    total_pass = total_fail = 0

    # Map project_id -> name for the environment breakdown.
    from automation.database.models import TestProject
    names = {p.id: p.name for p in db.query(TestProject).all()}

    for r in runs:
        if _is_pass(r.status):
            ok = True
        elif _is_fail(r.status):
            ok = False
        else:
            continue  # skip queued/running/cancelled
        day = (r.created_at or since).strftime("%Y-%m-%d")
        daily[day]["passed" if ok else "failed"] += 1
        env = names.get(r.project_id, r.test_suite or "—")
        per_env[env]["passed" if ok else "failed"] += 1
        outcomes[r.test_name or "—"].append(ok)
        total_pass += ok
        total_fail += (not ok)

    total = total_pass + total_fail
    flaky = sorted(
        [{"test_name": n, "runs": len(o), "passed": sum(o), "failed": len(o) - sum(o)}
         for n, o in outcomes.items() if any(o) and not all(o)],
        key=lambda x: x["failed"], reverse=True,
    )[:15]

    return {
        "days": days,
        "total_runs": total,
        "pass_rate": round(100 * total_pass / total, 1) if total else 0.0,
        "passed": total_pass, "failed": total_fail,
        "daily": [{"date": d, **daily[d]} for d in sorted(daily)],
        "by_environment": sorted(
            [{"environment": e, **per_env[e],
              "pass_rate": round(100 * per_env[e]["passed"] / max(1, per_env[e]["passed"] + per_env[e]["failed"]), 1)}
             for e in per_env],
            key=lambda x: x["passed"] + x["failed"], reverse=True,
        ),
        "flaky": flaky,
    }


@router.get("/{run_id}")
def get_report(run_id: str, db: Session = Depends(get_db),
               current_user=Depends(get_current_user)):
    """The full report for a run: metadata, scenarios, RCA, and the narrative."""
    r = db.query(TestRun).filter(TestRun.id == run_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    scs = _scenarios(db, run_id)
    rca = db.query(RCAReport).filter(RCAReport.run_id == run_id).first()
    return {
        **_run_summary(db, r),
        "error_message": r.error_message,
        "report_summary": r.report_summary,
        "report_generated_at": r.report_generated_at.isoformat() if r.report_generated_at else None,
        "scenarios": [{
            "scenario_num": s.scenario_num, "scenario_name": s.scenario_name,
            "status": s.status, "consumer_status": getattr(s, "consumer_status", None),
            "business_status": getattr(s, "business_status", None),
            "error": getattr(s, "error", None), "role": getattr(s, "role", None),
        } for s in scs],
        "rca": None if not rca else {
            "root_cause": rca.root_cause, "suggested_fix": rca.suggested_fix,
            "summary": getattr(rca, "summary", None), "llm_provider": rca.llm_provider,
        },
    }


def _ollama_prose(system: str, user: str) -> str:
    """Local Ollama call for a prose narrative (no JSON formatting)."""
    base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    model = os.getenv("LLM_MODEL_NAME", "llama3.2")
    resp = httpx.post(
        f"{base}/api/generate",
        json={"model": model, "system": system, "prompt": user, "stream": False,
              "options": {"temperature": 0.3, "num_predict": 1024}},
        timeout=300,
    )
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


_SYSTEM = (
    "You are a senior QA engineer writing a concise, professional test report. "
    "Write in clear plain English for a mixed technical/non-technical audience. "
    "Use short paragraphs and, where useful, bullet points. Do not invent facts — "
    "use only the data given. Structure: 1) Outcome summary, 2) What was tested, "
    "3) What passed/failed and why, 4) Recommended next steps."
)


@router.post("/{run_id}/generate")
def generate_report(run_id: str, db: Session = Depends(get_db),
                    current_user=Depends(get_current_user)):
    """Generate (or regenerate) the narrative report for a run and store it."""
    r = db.query(TestRun).filter(TestRun.id == run_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    scs = _scenarios(db, run_id)
    rca = db.query(RCAReport).filter(RCAReport.run_id == run_id).first()

    passed = sum(1 for s in scs if (s.status or "").upper() in ("PASS", "PASSED"))
    lines = [
        f"Test: {r.test_name}", f"Suite: {r.test_suite}", f"Overall status: {r.status}",
        f"Device: {r.device_name} ({r.platform})",
        f"Branch: {r.branch or '-'}  Commit: {(r.commit_sha or '-')[:10]}",
        f"Duration: {round((r.duration_ms or 0)/1000, 1)}s",
        f"Scenarios: {passed}/{len(scs)} passed" if scs else "Scenarios: (none recorded)",
    ]
    if r.error_message:
        lines.append(f"Error: {r.error_message[:500]}")
    if scs:
        lines.append("Per-scenario results:")
        for s in scs[:60]:
            det = f" — {s.error[:160]}" if getattr(s, "error", None) else ""
            lines.append(f"  [{s.status}] {s.scenario_num or ''} {s.scenario_name or ''}{det}")
    if rca and (rca.root_cause or getattr(rca, "summary", None)):
        lines.append(f"Root cause analysis: {rca.root_cause or ''} {getattr(rca, 'summary', '') or ''}")
        if rca.suggested_fix:
            lines.append(f"Suggested fix: {rca.suggested_fix}")

    try:
        narrative = _ollama_prose(_SYSTEM, "Write the test report from this data:\n\n" + "\n".join(lines))
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach the local Ollama model ({e}). Start it with 'ollama serve' "
                   f"and pull a model (e.g. 'ollama pull llama3.2').",
        )
    if not narrative:
        raise HTTPException(status_code=502, detail="The model returned an empty report.")

    r.report_summary = narrative
    r.report_generated_at = datetime.utcnow()
    db.commit()
    return {"report_summary": narrative, "report_generated_at": r.report_generated_at.isoformat()}
