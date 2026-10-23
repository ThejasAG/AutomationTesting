# Failure evidence & reporting — plan

Goal: when a run fails, the report says WHERE it failed, HOW MUCH passed, and WHY —
without anyone opening a terminal. Every item below came from a real dead end on
2026-09-02, noted against it.

Status: `[ ]` not started · `[~]` in progress · `[x]` done & tested

## Phase 1 — Pass/fail percentage        (small, no schema change)
- [x] 1.1 Pure function: parse `ScenarioResult.reasons` -> (passed, failed, total, pct)
      `reasons` already carries one `[ok]` / `[FAIL]` line per step.
- [x] 1.2 Serve it on the scenario-result API
- [x] 1.3 Show it in the run report ("6/8 steps · 75%")
- [x] 1.4 Tests: all-pass, all-fail, partial, empty, non-step notes ignored

## Phase 2 — Failure evidence            (the "app crashed" -> real diagnosis gap)
- [x] 2.1 JS console capture from the Metro log, windowed to the failing step
      2026-09-02: `AppRegistry is not a registered callable module` sat in
      /tmp/metro8084.log for hours; the report only said "element not found".
- [x] 2.2 Device log window: `simctl spawn <udid> log stream`, filtered to the
      bundle id, +/-30s around the failure. NOT the whole run.
- [x] 2.3 Crash reports from ~/Library/Logs/DiagnosticReports
      `TestRun.crash_detected` already exists and nothing fills it.
- [x] 2.4 Screenshot at failure (column exists; only set on final capture today)
      The 90-degree rotation bug was ONLY visible in a screenshot.
- [x] 2.5 Attach the ~50 relevant lines to the failing step, not a raw dump
- [x] 2.6 Tests for each collector (pure parsing over captured fixtures)

## Phase 3 — Smart UI inspector          (locator_catalog.py already does the parsing)
- [x] 3.1 Endpoint: live tree -> catalog + gap report
- [x] 3.2 Page: screenshot left, tree right, click to highlight a frame
- [x] 3.3 **Overlap detector** — flag any element whose frame is covered by another.
      Would have caught 3 of today's 4 bugs (saveBtn/toast, slot/toast, card off-screen).
- [x] 3.4 Tests

## Explicitly out of scope for now
- Network logs — needs a proxy on the simulator; much larger job, least payoff.
