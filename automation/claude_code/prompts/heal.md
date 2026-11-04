**Run the regression flows and heal what fails.**

$run_scope

Follow `.claude/skills/run-all-heal/SKILL.md` for the order of work (run, sort,
retry once, investigate) and `.claude/skills/heal-failure/SKILL.md` for each real
failure. In short:

1. `preflight --wait 15`. If the rig never gets ready, report every flow as
   not_run (infra) and stop.
2. Get each failure's evidence (`run-info`, the screenshot files it prints,
   `history`, `show-flow`) and the code behind the failing step.
3. Decide what it is:
   - **TEST** (a step, an id, a missing dismiss) or **PLATFORM** (a handler, the
     runner, a timeout, the idb/Appium layer): fix the automation code, add a unit
     test that reproduces it, verify on the simulators (rule 4).
   - **APP_BUG / SERVER_BUG**: never change the test. Record the evidence; if you
     read the app code and see the fix, write it as a unified diff in
     `$session_dir/app_fixes/<app>-<flow>.patch` (paths relative to the app repo).
   - **INFRA** (simulator, WDA, Metro, staging 502): recover if a tool can
     (`recover wda`, `preflight --wait 15`), retry once, otherwise report it.
   - **FLAKY**: passed on the retry — report it, no change.
4. When every failure is handled, re-run once each flow you changed to be sure the
   final code passes, then write the report.

