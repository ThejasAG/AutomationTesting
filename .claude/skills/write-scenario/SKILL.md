---
name: write-scenario
description: Write a new Vyapy cross-app regression flow (consumer / waiter / kitchen segments) for a feature no flow covers, using only real element ids from the app source, and keep it only if it passes on the simulators.
---

# Write a new scenario

Tools: `.venv/bin/python scripts/agent_tools.py <subcommand>`.
Read `automation/knowledge/scenario_authoring.md` first: steps, the idb API, a handler
template, a unit-test template, and the traps that cost a failed run.

## 1. Understand the feature from the source

- `steps`: every step form, `@token` (with its handler) and shared block, read from
  the engine. Don't study `cross_app_flows.py` to learn them.
- The feature's screens and ids come with the task (from the feature map). Read
  those files under `repos/` (consumer app; business app = waiter + kitchen on the
  iPad). For each control note its `testID` / `accessibilityLabel`, what onPress
  does, and when it is enabled. `locator_map.md` lists known ids.
- `show-flow` the flow that gets closest and reuse its setup.
- Decide what the scenario must **prove**: a status change, a total, a list entry.
  "The screen opened" is not proof. A scenario without a check of the outcome is
  not done; check it with `@see:<text>`.

## 2. Write it

In `automation/scenarios/cross_app_flows.py`, at the end of `FLOWS`, under the
comment `# ── AI Agent drafts ──` (create it once):

```python
"cc_<short_snake_name>": {
    "id": "cc_<short_snake_name>",
    "name": "[Draft] <what it covers, in plain words>",
    "description": "<feature, screens, what it proves>",
    "segments": [
        _seg("1", "C-App: …", "consumer", _C_BOOK_PREFIX + [...steps...]),
        _seg("2", "Waiter: …", "waiter", [..., "@see:<proof>"]),
    ],
},
```

Reuse the shared step blocks (`steps` lists them with the flows using each) the way
flow1–6 do. Never edit a shared block for a draft: every flow using it would change.
A new kind of step needs a handler method, a branch in `_handle_special` and a unit
test (templates in the authoring guide).

Writing several scenarios? Write them all first, then check and run them back to back.

## 3. Check, then run

1. `check-flows` and `check-ids cc_<id>`. Every id must be found in the source;
   template ids like `<User>Card` are fine when the code builds them that way.
2. `unit-tests tests/<your test>.py` for each test you added. Run only those: the
   full suite takes ~7 minutes and the checks after the session run it.
3. `run cc_<id> --label new`.
   - On a test failure: read `run-info` (the `↳ on screen:` ids and the screenshot),
     fix, and run again (max fix attempts). A failure in segment 2 or later re-runs
     with `--resume <failed run>`, which keeps the segments that passed.
   - The same failure twice in a row, after a fix aimed at it → drop the draft: a
     third try costs another 5–10 minutes of simulator time and rarely lands. A draft
     runs at most 5 times in a session; `run` refuses more.
   - On an app failure: keep the evidence and report it as an app bug.
4. Keep the draft only if it **passed after your last edit to what it runs**: its own
   steps, or the handler code its tokens reach. Editing or removing another draft, a
   test or a doc does not undo a pass. A change to a shared block or to the engine
   core does. Otherwise remove the draft completely (no half-written flow left
   behind) and report it as dropped, with why.

The flow keeps its `[Draft]` name until a person approves the session; they can
rename it then.
