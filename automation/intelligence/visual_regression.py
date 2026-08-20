"""Visual regression analysis — pixel-diff current screenshots vs a stored baseline."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("visual_regression")

# Project root → baselines/ and reports/ live here.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASELINE_ROOT = os.path.join(_ROOT, "baselines")
REPORTS_ROOT = os.path.join(_ROOT, "reports")

_HIGH = 10.0   # > 10% diff
_MED = 5.0     # > 5% diff  (also the pass/fail threshold)
_LOW = 2.0     # > 2% diff


def _screen_name(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _severity(pct: float) -> str:
    if pct > _HIGH:
        return "high"
    if pct > _MED:
        return "medium"
    if pct > _LOW:
        return "low"
    return "none"


def _list_pngs(directory: str) -> List[str]:
    if not directory or not os.path.isdir(directory):
        return []
    return sorted(
        os.path.join(directory, f) for f in os.listdir(directory)
        if f.lower().endswith((".png", ".jpg", ".jpeg"))
    )


class VisualRegressionAnalyzer:
    def capture_baseline(self, run_id: str, screenshots_dir: str) -> Dict[str, Any]:
        """Store this run's screenshots as the baseline for its project."""
        import shutil
        project_id = self._project_of(run_id)
        dest = os.path.join(BASELINE_ROOT, project_id or run_id)
        os.makedirs(dest, exist_ok=True)
        saved = 0
        for src in _list_pngs(screenshots_dir):
            try:
                shutil.copyfile(src, os.path.join(dest, f"{_screen_name(src)}.png"))
                saved += 1
            except Exception as e:
                logger.warning("baseline copy failed for %s: %s", src, e)
        return {"baseline_count": saved, "saved": saved > 0, "baseline_dir": dest}

    def compare_with_baseline(self, run_id: str, project_id: str,
                              current_screenshots: List[str]) -> Dict[str, Any]:
        """Pixel-compare current screenshots to the project baseline."""
        import cv2
        import numpy as np

        baseline_dir = os.path.join(BASELINE_ROOT, project_id or "")
        diff_dir = os.path.join(REPORTS_ROOT, run_id, "visual_diff")
        os.makedirs(diff_dir, exist_ok=True)

        # Accept either a directory or an explicit list of paths.
        if len(current_screenshots) == 1 and os.path.isdir(current_screenshots[0]):
            current_screenshots = _list_pngs(current_screenshots[0])

        regressions: List[Dict[str, Any]] = []
        total = 0
        max_diff = 0.0

        for current_path in current_screenshots:
            screen = _screen_name(current_path)
            baseline_path = os.path.join(baseline_dir, f"{screen}.png")
            if not os.path.exists(baseline_path):
                continue  # new screen — no baseline to compare against
            total += 1

            baseline = cv2.imread(baseline_path)
            current = cv2.imread(current_path)
            if baseline is None or current is None:
                continue
            if baseline.shape != current.shape:
                current = cv2.resize(current, (baseline.shape[1], baseline.shape[0]))

            diff = cv2.absdiff(baseline, current)
            gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
            _, thresh = cv2.threshold(gray, 25, 255, cv2.THRESH_BINARY)
            nonzero = int(np.count_nonzero(thresh))
            pct = (nonzero / thresh.size) * 100.0 if thresh.size else 0.0
            max_diff = max(max_diff, pct)

            severity = _severity(pct)
            diff_path = os.path.join(diff_dir, f"{screen}_diff.png")
            highlight = current.copy()
            highlight[thresh > 0] = [0, 0, 255]  # red overlay on changed pixels
            blended = cv2.addWeighted(current, 0.6, highlight, 0.4, 0)
            try:
                cv2.imwrite(diff_path, blended)
            except Exception:
                diff_path = ""

            if pct > _MED:
                regressions.append({
                    "screen": screen,
                    "diff_percentage": round(pct, 2),
                    "baseline_path": baseline_path,
                    "current_path": current_path,
                    "diff_path": diff_path,
                    "severity": severity,
                })

        return {
            "regressions": regressions,
            "total_screens": total,
            "screens_changed": len(regressions),
            "max_diff": round(max_diff, 2),
            "passed": len(regressions) == 0,
        }

    def update_baseline(self, project_id: str, run_id: str) -> Dict[str, Any]:
        """Update the baseline with a passing run's screenshots."""
        screenshots_dir = self._screenshots_dir(run_id)
        result = self.capture_baseline(run_id, screenshots_dir)
        return {"updated": result.get("baseline_count", 0)}

    # ── helpers ──────────────────────────────────────────────────────────────
    def _project_of(self, run_id: str) -> Optional[str]:
        try:
            from automation.database.config import SessionLocal
            from automation.database.models import TestRun
            with SessionLocal() as db:
                r = db.query(TestRun).filter(TestRun.id == run_id).first()
                return r.project_id if r else None
        except Exception:
            return None

    def _screenshots_dir(self, run_id: str) -> str:
        """Where a run's screenshots land (scenario runner + agent evidence)."""
        from automation.projects.repository import repository_manager
        project_id = self._project_of(run_id)
        repo = repository_manager.get_repo_path(project_id) if project_id else None
        for cand in (
            os.path.join(repo, "reports", run_id) if repo else "",
            os.path.join(repo, "reports", "scenario") if repo else "",
            os.path.join(REPORTS_ROOT, run_id),
        ):
            if cand and os.path.isdir(cand) and _list_pngs(cand):
                return cand
        return os.path.join(REPORTS_ROOT, run_id)


visual_regression_analyzer = VisualRegressionAnalyzer()
