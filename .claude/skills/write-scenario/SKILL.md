---
name: write-scenario
description: Write a new Vyapy cross-app regression flow (consumer / waiter / kitchen segments) for a feature no flow covers, using only real element ids from the app source, and keep it only if it passes on the simulators.
---

# Write a new scenario

Tools: `.venv/bin/python scripts/agent_tools.py <subcommand>`.

## 1. Understand the feature from the source

- Read its screens under `repos/` (consumer app; business app = waiter + kitchen on
  the iPad). Note every control's real `testID` / `accessibilityLabel`, what onPress
  does, and when the control is enabled. `locator_map.md` lists known ids.
- `flows` and `show-flow` the flows that get closest: reuse their setup.
- Decide what the scenario must **prove** — a status change, a total, a list entry —
  not just "the screen opened". A scenario without a check of the outcome is not done.

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
        _seg("2", "Waiter: …", "waiter", [...steps...]),
    ],
},
```

Reuse the shared step blocks near the top of the file (`_C_BOOK_PREFIX`,
`_C_ORDER_LATER`, `_C_ACCEPT_APPT`, `_C_PAY_FINAL`, `_W_CREATE_APPT`,
`_W_ASSIGN_SEND`, `_K_READY`, `_W_SERVE_NOTIFY`, `_W_PAY_CLOSE`, …) the way flow1–6
do — never edit a shared block for a draft (every flow using it would change). Steps:
`click <id>`, `?click <id>` (optional), `type <text> in <id>`, `select <text>`,
`scroll down`, and `@token`s that `_handle_special` dispatches. A new kind of step
needs a handler method + a branch in `_handle_special` + a unit test.

## 3. Check, then run

1. `check-flows` and `check-ids cc_<id>` (every id must be found in the source;
   template ids like `<User>Card` are fine when the code builds them that way).
2. `unit-tests` for any test you added.
3. `run cc_<id> --label new`. On a test failure: fix and run again (max fix
   attempts). On an app failure: keep the evidence, report it as an app bug.
4. Keep the draft only if it **passed after your last edit**. Otherwise remove it
   completely (no half-written flow left behind) and report it as dropped, with why.

The flow keeps its `[Draft]` name until a person approves the session; they can
rename it then.
