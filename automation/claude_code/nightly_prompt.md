# Nightly regression run — {date} ({mode} analysis)

You are running unattended on the test Mac. Nobody will answer questions: decide,
act, and write down what you did. Read `AGENTS.md` first (sections 4 and 5 are the
platform's hard-won gotchas) and `automation/knowledge/locator_map.md` (the real
element ids). Stop starting new work at **{stop_by}**; the report must be written
before **{deadline}**.

## Your only tools

- Read / Grep / Glob anywhere in the project, including the app source under `repos/`.
- ONE Bash command, always written exactly like this (no `cd`, no pipes, no `&&`):
  `.venv/bin/python scripts/claude_nightly_tools.py <subcommand> ...`
  Run it with `--help` to see the subcommands. The ones you need:
  `preflight --wait 15`, `flows`, `show-flow F`, `run F --label L`, `wait RUN`,
  `run-info RUN`, `history F`, `check-flows`, `coverage`, `app-changes`,
  `app-show APP PATH`, `app-grep APP PATTERN`.
- Edit ONLY `automation/scenarios/cross_app_flows.py` (test fixes and new
  scenarios). Write files ONLY in `{night_dir}`.
- Everything else is blocked. Never touch the app checkouts in `repos/`, `.env`,
  git, or any other file. All your edits are moved to a review branch after you
  finish; the user approves them in the morning.

A flow can take 2–12 minutes. `run` waits up to 9 minutes; if it prints
`STILL RUNNING`, call `wait RUN` until it ends. Only one flow runs at a time.

## 1. Health check

`preflight --wait 15`. If the rig is still not ready after that (staging 502,
Appium down, a simulator not booted), do not run flows. Write the report with
every flow as `not_run` and the reason, and stop.

## 2. Run every flow, one by one

`flows`, then `run <id> --label first` for each one in that order. Do not edit
anything during this phase. Retry a failure once before you investigate it, as follows:
- rule sorter says INFRA: `preflight --wait 15`, then `run <id> --label retry`.
- rule sorter says TIMING: `run <id> --resume <failed run> --label retry`.
A flow that passes on retry is `flaky`: report it, no fix.
If staging goes down in the middle, wait with `preflight --wait 15` and continue;
if it stays down, mark the remaining flows `not_run` (infra).

`flow_kitchen_demo` needs an order already in the kitchen queue. "No in-progress
order" there is an unmet precondition, not a bug.

## 3. Investigate and fix each real failure

For every flow still failing: `run-info <run>` (Read the screenshot files it
prints), `show-flow`, `history`, then the app source behind the failing screen.
Decide:
- **LOCATOR / TEST_ISSUE** (an id changed, a dialog needs dismissing, a step is
  missing): fix the flow's steps in `cross_app_flows.py`. Use only ids you found in
  the app source or `locator_map.md`, or handlers that already exist in
  `STEP_CATALOG`. Never remove or weaken a step that checks a value (a total, VAT,
  a status). If such a check fails, that is an app bug. After editing run
  `check-flows`, then verify with `run <id> --label fix`. **Keep the fix only if
  that run passes**; otherwise undo your edit exactly and try once more with a
  different idea (at most 2 attempts per flow), then leave it as `needs_human`.
- **APP_BUG** (crash, wrong total, a button that does nothing while the rig is
  healthy): do not change the test. Write the evidence, and a suggested minimal
  patch only if you read the code, as a unified diff in
  `{night_dir}/app_fixes/<app>-<flow>.patch` (paths relative to the app repo
  root).
- **INFRA** (simulator, Appium/WDA, Metro, staging): report it, no fix.
- Not sure: `needs_human`, with what you found. Never guess.

## 4. New features → new scenarios ({mode})

{mode_instructions}

To add a scenario, add a flow to the end of the `FLOWS` dict in
`cross_app_flows.py` under the comment `# ── Claude Code nightly drafts ──`
(create the comment once). Use id `cc_<short_snake_name>`, a name starting with
`[Draft] `, a description saying which feature and screens it covers, and
segments built with `_seg(...)` exactly like the existing flows. Reuse the
existing step lists and handlers for setup (login, booking) and add steps only
with real ids. Run `check-flows`, then `run cc_<id> --label new`. Keep a draft
only if it passes; if it fails twice, remove it and describe it in the report as
`dropped`, with the reason. At most {max_new} new scenarios tonight.

## 5. The report (always, even if you stopped early)

Write two files in `{night_dir}`:

`report.md`: for the team, plain and short.
- one-line result: passed / failed / fixed / flaky / not run
- each failure: what broke, the evidence, what you did
- test fixes waiting for approval, as old step → new step
- app bugs, with the patch file names
- new draft scenarios: what they cover; dropped ones and why
- coverage numbers from `coverage`
- anything the team must do by hand

`summary.json`:
```json
{{"headline": "one line",
 "flows": [{{"flow_id": "", "final_status": "passed|failed|flaky|fixed|not_run",
            "category": "", "root_cause": "", "action": "none|retried|test_fixed|fix_reverted|app_bug|infra|needs_human"}}],
 "test_fixes": [{{"flow_id": "", "change": "", "verified_run": ""}}],
 "app_bugs": [{{"flow_id": "", "app": "consumer|business", "summary": "", "patch_file": ""}}],
 "new_scenarios": [{{"flow_id": "", "covers": "", "status": "passed|dropped", "run_id": "", "reason": ""}}],
 "coverage": {{"consumer": "", "business": ""}},
 "needs_human": [""]}}
```
Every flow from `flows` appears in `flows`. Report only what the tool output
shows. A run you did not see pass did not pass.
