---
name: analyze-apps
description: Analyze the Vyapy consumer and business apps from their source, map every feature against the regression flows (covered / partial / missing, with risk), and write the feature map. Use for "analyze the application" or before writing new scenarios.
---

# Analyze the apps

Tools: `.venv/bin/python scripts/agent_tools.py <subcommand>`.

1. **The computed map (free):** `coverage` lists every navigator screen of both apps,
   its element ids, and whether any flow drives them. `apps` says which checkout is
   which. `app-changes` lists new upstream commits (for a quick pass, analyze what
   they changed: `app-show` / `app-grep` read the upstream code).
2. **Read the features — in parallel.** Start two `app-analyst` helper agents in
   one message: one for the **consumer** app (diner: discover, book, invite, pre-order,
   wallet, pay), one for the **business** app (waiter: bookings board, new
   appointment, tables, orders, void/comp/split, payment, close table; kitchen: queue,
   ready). Tell each which screens to read (all, or only the changed/uncovered ones),
   and to return per feature: name, role, screens, user actions, the rules the code
   enforces (money, status, permissions), and real element ids.
3. **Compare with the flows.** `flows`; `show-flow` the ones that touch a feature.
   Rate each feature:
   - **covered** — a flow drives it AND checks its outcome;
   - **partial** — a flow passes through it but does not check the outcome, or only
     the happy path;
   - **missing** — no flow touches it.
   Risk: **high** = money, bookings, orders, payments, table/booking status;
   **medium** = lists, search, filters, settings, profile; **low** = cosmetic.
4. **Write `feature_map.json`** in the session folder (format below) and a short
   summary + a table of high-risk gaps in `report.md`.

```json
{"generated": "YYYY-MM-DD", "mode": "delta|full",
 "apps": {"consumer": [{"feature": "", "role": "consumer", "screens": [""],
                         "status": "covered|partial|missing", "risk": "high|medium|low",
                         "covered_by": ["flow ids"], "gap": "what no flow checks",
                         "scenario": "a scenario that would cover the gap, one sentence",
                         "ids": ["real element ids it would use"]}],
          "business": [{"feature": "", "role": "waiter|kitchen", "...": "same fields"}]},
 "totals": {"consumer": {"covered": 0, "partial": 0, "missing": 0},
            "business": {"covered": 0, "partial": 0, "missing": 0}}}
```

Analysis only reads: no flow runs and no code changes in this step.
