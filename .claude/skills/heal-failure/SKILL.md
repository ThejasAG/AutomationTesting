---
name: heal-failure
description: Diagnose one failed Vyapy flow run and, when the automation is at fault, fix the automation platform (flow steps, handlers, engine), prove it with a unit test and a passing run on the simulators. App and server bugs are reported with evidence, never worked around.
---

# Heal one failed run

Tools: `.venv/bin/python scripts/agent_tools.py <subcommand>`.

## 1. Evidence first

- `run-info RUN` — segments, the failing step, every note the handler wrote, the
  rule sorter's verdict, and screenshot files: **Read the screenshots**.
- `show-flow F` and `history F` — is this new, or has it failed like this before?
- The handler behind the failing step: `@token` → `FlowRunner._handle_special` in
  `automation/scenarios/cross_app_flows.py` → the method it calls. Plain steps
  (`click x`, `type a in b`) go through `ScenarioRunner` / `_smart_click`.
- The app screen it was on: find it under `repos/` (Grep the ids from the notes) and
  read what the button really does (onPress, conditions, ids). `locator_map.md`
  lists real ids per screen.
- If the screen is unclear and the simulators are free: `screen consumer|waiter`.

## 2. Decide what it is

| It is… | Signs | You do |
|---|---|---|
| **TEST** | an id changed in the app source, a new dialog to dismiss, a step missing or in the wrong order | fix the flow steps (real ids only) |
| **PLATFORM** | the handler mis-reads the screen, picks the wrong element/booking, times out on a slow but healthy app, an idb/Appium call hangs | fix the engine code |
| **APP_BUG** | the app does the wrong thing on a healthy rig: crash, wrong total, a button that does nothing, a status that never changes | no test change; evidence + optional app patch |
| **SERVER_BUG** | staging answers but records the wrong thing (see known_issues.md) | no test change; evidence |
| **INFRA** | WDA/Appium/simulator/Metro trouble, staging 502 | `recover wda` / `preflight --wait 15`, retry once, report |
| **FLAKY** | the retry passed | report only |

Not sure → `needs_human`, with what you found. Never guess.

## 3. Fix (TEST / PLATFORM)

1. Reproduce it in a **unit test** first: a scripted screen like
   `tests/test_accept_appointment.py` (frames of idb elements, a fake tap that
   advances them). Name the test after the behaviour, note the run id and date in
   its docstring ("MEASURED 2026-10-22 (flow5): …").
2. Make the smallest change that fixes the cause, in the style of the surrounding
   code (comments say WHY, with the measured evidence).
3. `unit-tests tests/test_<yours>.py` (and the other test files for that handler).
4. `check-flows`; for flow edits also `check-ids F`.
5. `impacted <function>` for each engine function you changed.
6. `run F --label fix` — must PASS. For engine changes also run one other flow from
   `impacted` (`--label regress`).
7. Failed again? One more idea at most (max fix attempts per failure), then
   **undo your change exactly** and report `fix_reverted` / `needs_human`.

Never: remove or loosen a value check, accept a wrong total/status, retry a
payment or a Confirm until it goes green, invent ids or @tokens, edit `repos/`.

## 4. App / server bug

Write the evidence (run id, screenshot file, the app code that is wrong, what
should happen). If you can see the fix in the app source, write it as a unified diff
in `<session folder>/app_fixes/<app>-<flow>.patch` (paths relative to the app repo
root). The test stays as it is — it is right to fail.
