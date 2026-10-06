"""Test Reports — a proper, stored report for every run.

Aggregates a run's metadata, per-scenario results, and root-cause analysis into
one report, and generates a plain-English narrative ("what happened, why, what to
do") via the local Ollama model (the same no-key provider the RCA/Chat features
use). Reports are stored on the run and can be regenerated or exported.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
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


def _fmt_duration(ms: Optional[int]) -> str:
    """Human duration like the sample report ('32m', '1h 04m', '45s')."""
    if not ms or ms <= 0:
        return "—"
    secs = int(ms / 1000)
    if secs < 60:
        return f"{secs}s"
    mins, s = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m" if s < 10 else f"{mins}m {s:02d}s"
    hrs, m = divmod(mins, 60)
    return f"{hrs}h {m:02d}m"


@router.get("/{run_id}/rich", response_class=HTMLResponse)
def rich_report(run_id: str, screenshots: int = 1, db: Session = Depends(get_db),
                current_user=Depends(get_current_user)):
    """Standalone, styled HTML report for a run — summary cards, per-scenario
    rows, and the pre/post-payment VAT tables parsed from each scenario's
    validation reasons. Print-to-PDF from the browser (matches the sample).

    screenshots=1 (default) embeds failure screenshots for the on-screen view;
    pass screenshots=0 for an image-free copy (used when downloading/exporting)."""
    from automation.reporting.scenario_report import records_from_rows, render_run_report
    r = db.query(TestRun).filter(TestRun.id == run_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    rows = _scenarios(db, run_id)
    records = records_from_rows(rows)

    device = r.device_name or "—"
    platform = r.platform or "—"
    duration = _fmt_duration(r.duration_ms)
    subtitle_bits = [f"Device: {device}", f"Platform: {platform}", f"Duration: {duration}"]
    if r.branch:
        subtitle_bits.append(f"Branch: {r.branch}")
    title = f"Vya Mobile Automation — {r.test_name or 'Test Report'}"
    html = render_run_report(
        records, title=title, subtitle="  ·  ".join(subtitle_bits),
        duration=duration, validation_header="Bill &amp; VAT Validation",
        include_screenshots=bool(screenshots))
    return HTMLResponse(content=html)


def _verdict(status: str, scenarios_total: int) -> str:
    """Honest verdict. A run that executed ZERO scenarios verified nothing — it is
    NOT a pass, no matter what the run row says. That distinction stops a build-only
    run from masquerading as a validated pass."""
    s = (status or "").lower()
    if s in ("failed", "error"):
        return "failed"
    if scenarios_total == 0:
        return "no-tests"          # built/ran but nothing was actually tested
    if s in ("passed", "completed"):
        return "passed"
    return s or "unknown"


def _run_summary(db: Session, r: TestRun) -> Dict[str, Any]:
    scs = _scenarios(db, r.id)
    passed = sum(1 for s in scs if (s.status or "").upper() in ("PASS", "PASSED"))
    return {
        "id": r.id,
        "test_name": r.test_name,
        "test_suite": r.test_suite,
        "status": r.status,
        "verdict": _verdict(r.status, len(scs)),
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


@router.get("/config")
def report_config(current_user=Depends(get_current_user)):
    """Which integrations are configured (so the UI shows the right buttons)."""
    from automation.notifications import dispatch
    return dispatch.status()


@router.get("/golden-run")
def golden_run(db: Session = Depends(get_db),
               current_user=Depends(get_current_user)):
    """Demo mode: the pinned known-GREEN run to fall back to if a live run blips.
    Reads logs/golden_run.txt (written after a clean run); if that's missing/stale it
    falls back to the most recent passed run. Public so a demo screen can hit it."""
    import os
    rid = None
    try:
        p = os.path.join("logs", "golden_run.txt")
        if os.path.isfile(p):
            rid = (open(p, encoding="utf-8").read().strip() or None)
    except Exception:
        pass
    if rid and not db.query(TestRun).filter(TestRun.id == rid).first():
        rid = None                     # stale pin — the run was deleted
    if not rid:
        r = (db.query(TestRun).filter(TestRun.status == "passed")
             .order_by(TestRun.created_at.desc()).first())
        rid = r.id if r else None
    return {"run_id": rid,
            "report_url": (f"/api/v1/reports/{rid}/rich" if rid else None)}


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


def _prose(system: str, user: str) -> str:
    """Generate a prose narrative via the configured provider (Groq by default).

    Routes through the same fast/free provider the rest of the platform uses,
    so cross-app and single-app reports no longer depend on a local Ollama.
    """
    from automation.ai.provider import create_provider, default_config
    resp = create_provider(default_config).generate(
        system, user, max_tokens=1024, temperature=0.3)
    return (resp.content or "").strip()


_SYSTEM = (
    "You are a senior QA engineer writing a concise, professional test report. "
    "Write in clear plain English for a mixed technical/non-technical audience. "
    "Use short paragraphs and, where useful, bullet points. Do not invent facts — "
    "use only the data given. CRITICAL HONESTY RULE: if ZERO scenarios ran, the run "
    "verified NOTHING — you must state plainly that this is NOT a validated pass and "
    "that no functionality was actually tested (the app may have only been built). "
    "Never imply a fix works, or a feature is fine, when no scenarios ran. "
    "Structure: 1) Outcome summary, 2) What was tested, 3) What passed/failed and why, "
    "4) Recommended next steps."
)


@router.post("/{run_id}/jira")
def file_jira(run_id: str, db: Session = Depends(get_db),
              current_user=Depends(get_current_user)):
    """File a Jira bug for a failed run, with the RCA in the description."""
    from automation.notifications import dispatch
    if not dispatch.jira_enabled():
        raise HTTPException(status_code=400, detail="Jira is not configured (set JIRA_URL/EMAIL/TOKEN/PROJECT_KEY).")
    r = db.query(TestRun).filter(TestRun.id == run_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    rca = db.query(RCAReport).filter(RCAReport.run_id == run_id).first()
    desc = [f"Run: {r.test_name}", f"Suite: {r.test_suite}", f"Status: {r.status}",
            f"Device: {r.device_name} ({r.platform})"]
    if r.error_message:
        desc.append(f"Error: {r.error_message}")
    if rca and rca.root_cause:
        desc.append(f"Root cause: {rca.root_cause}")
        if rca.suggested_fix:
            desc.append(f"Suggested fix: {rca.suggested_fix}")
    if r.report_summary:
        desc.append(f"\n{r.report_summary}")
    key = dispatch.create_jira_ticket(
        summary=f"[QA] {r.test_name} — {r.status}"[:200], description="\n".join(desc))
    if not key:
        raise HTTPException(status_code=502, detail="Could not create the Jira ticket (check Jira config/credentials).")
    return {"key": key, "url": dispatch.jira_browse_url(key)}


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
        (f"Scenarios executed: {passed}/{len(scs)} passed" if scs else
         "Scenarios executed: 0 — NOTHING WAS ACTUALLY TESTED. The run may have only "
         "built the app. This is NOT a validated pass; report it as such."),
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
        narrative = _prose(_SYSTEM, "Write the test report from this data:\n\n" + "\n".join(lines))
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Could not generate the report narrative ({e}). "
                   f"Check the AI provider config (LLM_API_BASE / OPENAI_API_KEY).",
        )
    if not narrative:
        raise HTTPException(status_code=502, detail="The model returned an empty report.")

    r.report_summary = narrative
    r.report_generated_at = datetime.utcnow()
    db.commit()
    return {"report_summary": narrative, "report_generated_at": r.report_generated_at.isoformat()}
