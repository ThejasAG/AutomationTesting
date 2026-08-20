"""Auto-generated, PM-friendly test summaries (Ollama-backed, DB-cached)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger("test_summary")


def _ollama(system: str, user: str, max_tokens: int = 256) -> Optional[str]:
    """Generate via the configured LLM provider (Groq/OpenAI/Ollama). Named
    `_ollama` for back-compat; it now uses whatever LLM_PROVIDER_TYPE selects."""
    try:
        from automation.ai.provider import create_provider, default_config
        resp = create_provider(default_config).generate(
            system, user, max_tokens=max_tokens, temperature=0.2)
        return (resp.content or "").strip() or None
    except Exception as e:
        logger.warning("LLM summary unavailable: %s", e)
        return None


class TestSummaryGenerator:
    def generate_run_summary(self, run_id: str, db) -> Dict[str, Any]:
        """3–4 sentence, plain-English summary of a single run. Cached on the run."""
        from automation.database.models import TestRun, RCAReport

        run = db.query(TestRun).filter(TestRun.id == run_id).first()
        if not run:
            return {"summary": "Run not found.", "cached": False}
        if run.report_summary:
            return {"summary": run.report_summary, "cached": True,
                    "generated_at": run.report_generated_at.isoformat() if run.report_generated_at else None}

        rca = db.query(RCAReport).filter(RCAReport.run_id == run_id).first()
        facts = [
            f"Test: {run.test_name or run.test_suite or 'unnamed'}",
            f"Status: {run.status}",
            f"Device: {run.device_name or 'n/a'} ({run.os_version or 'n/a'})",
            f"Duration: {round((run.duration_ms or 0) / 1000, 1)}s",
            f"Attempts: {getattr(run, 'attempts', 1)}"
            + (" (flaky — passed on retry)" if getattr(run, "flaky_detected", False) else ""),
        ]
        if run.error_message:
            facts.append(f"Error: {run.error_message[:300]}")
        if rca and getattr(rca, "root_cause", None):
            facts.append(f"Root cause: {rca.root_cause[:300]}")

        system = (
            "You are a QA lead writing for product managers. In 3-4 short sentences, plainly "
            "explain what this test run did and what its result means for the release. No jargon, "
            "no code, no bullet points. Be honest: if it failed, say what broke in business terms."
        )
        text = _ollama(system, "\n".join(facts)) or self._fallback_run_summary(run)

        run.report_summary = text
        run.report_generated_at = datetime.utcnow()
        try:
            db.commit()
        except Exception:
            db.rollback()
        return {"summary": text, "cached": False,
                "generated_at": run.report_generated_at.isoformat()}

    def generate_trend_summary(self, project_id: str, days: int = 7) -> Dict[str, Any]:
        """Weekly-trend summary across a project's recent runs."""
        from automation.database.config import SessionLocal
        from automation.database.models import TestRun

        since = datetime.utcnow() - timedelta(days=days)
        with SessionLocal() as db:
            runs: List[TestRun] = (
                db.query(TestRun)
                .filter(TestRun.project_id == project_id, TestRun.created_at >= since)
                .all()
            )
        total = len(runs)
        if not total:
            return {"summary": f"No test runs in the last {days} days.", "total": 0}

        passed = sum(1 for r in runs if (r.status or "").lower() == "passed")
        failed = sum(1 for r in runs if (r.status or "").lower() in ("failed", "error"))
        flaky = sum(1 for r in runs if getattr(r, "is_flaky", False) or getattr(r, "flaky_detected", False))
        pass_rate = round(100 * passed / total, 1)

        facts = (
            f"Window: last {days} days\nTotal runs: {total}\nPassed: {passed}\n"
            f"Failed: {failed}\nFlaky: {flaky}\nPass rate: {pass_rate}%"
        )
        system = (
            "You are a QA lead. In 3-4 sentences for a product manager, summarize this week's test "
            "health trend: is quality improving or slipping, what stands out (failures, flakiness), "
            "and what to watch. Plain English, no bullet points."
        )
        text = _ollama(system, facts) or (
            f"Over the last {days} days there were {total} test runs with a {pass_rate}% pass rate "
            f"({failed} failed, {flaky} flaky). "
            + ("Quality looks healthy." if pass_rate >= 90 else
               "Failures are elevated — review the failing flows before release."
               if pass_rate < 70 else "Quality is acceptable but watch the flaky tests.")
        )
        return {
            "summary": text, "total": total, "passed": passed, "failed": failed,
            "flaky": flaky, "pass_rate": pass_rate, "days": days,
        }

    def _fallback_run_summary(self, run) -> str:
        status = (run.status or "unknown").lower()
        name = run.test_name or run.test_suite or "The test"
        if status == "passed":
            base = f"{name} passed on {run.device_name or 'the device'} in {round((run.duration_ms or 0)/1000,1)}s."
            if getattr(run, "flaky_detected", False):
                base += " It was flaky — it only passed after a retry, so keep an eye on it."
            return base
        if status in ("failed", "error"):
            return (f"{name} failed on {run.device_name or 'the device'}. "
                    + (f"The issue was: {run.error_message[:200]}. " if run.error_message else "")
                    + "This flow is not safe to release until fixed.")
        return f"{name} finished with status '{run.status}'."


test_summary_generator = TestSummaryGenerator()
