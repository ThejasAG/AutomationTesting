"""How much of a scenario actually passed.

A run that fails at step 7 of 8 is a very different thing from one that fails at
step 1, but the report said only "failed" for both. The step outcomes are already
recorded: FlowRunner writes one line per step into ScenarioResult.reasons, tagged
`[ok]` or `[FAIL]`. This reads them back — no schema change, no new capture.

Pure function over a list of strings, so it is testable without a device or a DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

# A step line starts with its outcome tag. Everything else in `reasons` — the
# "[where] failed at step:" marker, "[SLOW LOAD]" notes, indented "    ·" detail —
# is commentary ABOUT steps, not a step, and must not be counted as one.
_OK = "[ok]"
_FAIL = "[FAIL]"


@dataclass(frozen=True)
class StepStats:
    passed: int
    failed: int

    @property
    def total(self) -> int:
        return self.passed + self.failed

    @property
    def pass_pct(self) -> Optional[float]:
        """Percent of steps that passed, or None when nothing countable ran.

        None rather than 0.0: "no steps were recorded" and "every step failed" are
        different failures, and showing 0% for the first one is a lie.
        """
        if not self.total:
            return None
        return round(self.passed * 100.0 / self.total, 1)

    @property
    def fail_pct(self) -> Optional[float]:
        if self.pass_pct is None:
            return None
        return round(100.0 - self.pass_pct, 1)

    def summary(self) -> str:
        """One line for the report cell, e.g. '6/8 steps · 75% passed'."""
        if not self.total:
            return "no steps recorded"
        return f"{self.passed}/{self.total} steps · {self.pass_pct:g}% passed"


def step_stats(reasons: Optional[Sequence[str]]) -> StepStats:
    passed = failed = 0
    for line in reasons or []:
        text = (line or "").strip()
        if text.startswith(_OK):
            passed += 1
        elif text.startswith(_FAIL):
            failed += 1
    return StepStats(passed=passed, failed=failed)
