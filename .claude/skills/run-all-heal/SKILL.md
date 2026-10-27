---
name: run-all-heal
description: Run the Vyapy cross-app regression flows one by one on the simulators, sort failures, retry what is infra/timing, and hand each real failure to heal-failure. Use for "run all and fix what fails".
---

# Run the flows and heal what fails

All commands: `.venv/bin/python scripts/agent_tools.py <subcommand>` (one per Bash
call). Flows run with the code of the checkout you are in.

1. **Rig.** `preflight --wait 15`. Not ready after that (staging 502, Appium down, a
   simulator not booted) → `recover wda` once if it is WDA/Appium, `preflight` again;
   still not ready → report every flow `not_run` (infra) and stop.
2. **Is a person testing?** `busy`. If a run is on the simulators, wait (call `busy`
   again every minute or two). Never interrupt a person's run.
3. **Run.** `flows`, then `run <id> --label first` for each, in that order, unless
   you were given a list of failed runs to look at (then skip to 5 with those).
   Do not edit anything while this pass runs.
4. **Retry once, by the rule sorter's verdict** (printed by `run`):
   - INFRA → `preflight --wait 15`, then `run <id> --label retry`
   - TIMING → `run <id> --resume <failed run> --label retry`
   - anything else → no blind retry; investigate.
   A flow that passes on its retry is **flaky**: report it, change nothing.
5. **Investigate each real failure** with `.claude/skills/heal-failure/SKILL.md`.
   Use the `flow-debugger` helper agent to read a long run so your own context stays
   free for decisions.
6. **Final check.** After all fixes, run each flow you changed once more
   (`--label final`). Only a pass after your last edit counts.
7. **Report** as the task says (report.md + summary.json in the session folder).

Remember `known_issues.md`: the Guests flow pays 1 cent over the bill on purpose
(staging ignores an exact pay-for-all payment), the kitchen demo creates its own
order, and a "Waiting: …" stop is the 30-minute booking window, not a failure.
