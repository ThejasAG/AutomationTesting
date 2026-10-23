**Find out why one run failed, and fix the automation if the automation is at fault.**

The run: $run_list

A person pressed "Fix with Claude Code" on this run. Follow
`.claude/skills/heal-failure/SKILL.md`:

1. `run-info` the run; Read the screenshot files it prints; `show-flow` and
   `history` the flow; read the handler code behind the failing step and the app
   screen it was on.
2. Decide: TEST / PLATFORM (fix + unit test + verify on the simulators), APP_BUG /
   SERVER_BUG (evidence and an app patch file, no test change), INFRA (recover,
   retry once), FLAKY (retry passed: report only).
3. To verify, run the flow again — `run F --label fix` — and, for engine changes,
   one flow from `impacted` too. If the rig is busy with a person's runs, wait
   with `busy`.

Stay on this failure: do not run or change unrelated flows.
