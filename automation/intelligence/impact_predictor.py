"""Test impact prediction — score each test's chance of failing so we run the riskiest first."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("impact_predictor")

# Weights (sum = 1.0) — see predict_failure_risk.
_W_HIST = 0.40   # historical failure rate
_W_FLAKY = 0.30  # flakiness
_W_PROX = 0.20   # code proximity to changed files
_W_RECENCY = 0.10  # how recently it last failed


def _risk_level(score: float) -> str:
    if score >= 60:
        return "HIGH"
    if score >= 30:
        return "MEDIUM"
    return "LOW"


class TestImpactPredictor:
    def predict_failure_risk(self, test_files: List[str], project_id: str,
                             changed_files: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        changed_files = changed_files or []
        from automation.database.config import SessionLocal
        from automation.database.models import TestRun

        results: List[Dict[str, Any]] = []
        with SessionLocal() as db:
            for test in test_files:
                name = os.path.basename(test)
                history = (
                    db.query(TestRun)
                    .filter(TestRun.project_id == project_id, TestRun.test_name == name)
                    .order_by(TestRun.created_at.desc())
                    .limit(20)
                    .all()
                )

                # 1) historical failure rate
                if history:
                    fails = sum(1 for h in history if (h.status or "").lower() in ("failed", "error"))
                    hist_rate = fails / len(history)
                else:
                    hist_rate = 0.3  # unknown → mild default risk

                # 2) flakiness
                flaky = sum(1 for h in history if getattr(h, "is_flaky", False) or getattr(h, "flaky_detected", False))
                flaky_rate = (flaky / len(history)) if history else 0.0

                # 3) code proximity — does a changed file share the test's stem/dir?
                proximity = self._proximity(test, changed_files)

                # 4) recency — did it fail recently?
                recency = self._recency(history)

                score = 100.0 * (
                    _W_HIST * hist_rate
                    + _W_FLAKY * flaky_rate
                    + _W_PROX * proximity
                    + _W_RECENCY * recency
                )
                score = round(min(100.0, max(0.0, score)), 1)
                results.append({
                    "test": test,
                    "test_name": name,
                    "risk_score": score,
                    "risk_level": _risk_level(score),
                    "factors": {
                        "historical_failure_rate": round(hist_rate, 2),
                        "flakiness": round(flaky_rate, 2),
                        "code_proximity": round(proximity, 2),
                        "recency": round(recency, 2),
                        "runs_analyzed": len(history),
                    },
                })

        results.sort(key=lambda r: r["risk_score"], reverse=True)
        return results

    def reorder_tests(self, test_files: List[str], project_id: str,
                      changed_files: Optional[List[str]] = None) -> List[str]:
        """Return test_files ordered highest-risk first."""
        try:
            ranked = self.predict_failure_risk(test_files, project_id, changed_files)
            return [r["test"] for r in ranked]
        except Exception as e:
            logger.warning("reorder_tests failed, keeping original order: %s", e)
            return test_files

    # ── helpers ──────────────────────────────────────────────────────────────
    def _proximity(self, test: str, changed_files: List[str]) -> float:
        if not changed_files:
            return 0.0
        stem = os.path.splitext(os.path.basename(test))[0].lower()
        test_dir = os.path.dirname(test).lower()
        best = 0.0
        for cf in changed_files:
            cf_l = cf.lower()
            cf_stem = os.path.splitext(os.path.basename(cf))[0].lower()
            if cf_l == test.lower():
                return 1.0
            if stem and (stem in cf_l or cf_stem in stem):
                best = max(best, 0.8)
            elif test_dir and test_dir in cf_l:
                best = max(best, 0.5)
        return best

    def _recency(self, history: List[Any]) -> float:
        for h in history:  # history is newest-first
            if (h.status or "").lower() in ("failed", "error"):
                if not h.created_at:
                    return 0.5
                try:
                    age_days = (datetime.utcnow() - h.created_at).days
                except Exception:
                    return 0.5
                if age_days <= 1:
                    return 1.0
                if age_days <= 3:
                    return 0.7
                if age_days <= 7:
                    return 0.4
                return 0.1
        return 0.0


test_impact_predictor = TestImpactPredictor()
