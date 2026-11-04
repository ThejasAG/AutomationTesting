**Analyze the two Vyapy apps and map what the tests cover.** ($mode)

$mode_text

Follow `.claude/skills/analyze-apps/SKILL.md`:

1. `coverage` (every navigator screen of both apps, its element ids, and whether
   any flow drives them — computed, free) and `app-changes`.
2. Start two `app-analyst` helper agents in parallel — one for the consumer app,
   one for the business app (waiter and kitchen roles) — each told which screens
   to read ($mode_scope). They return features: name, role, screens, the user
   actions, the money/status rules the code enforces, and the element ids.
3. Compare their features with the flows (`flows`, `show-flow`) and rate each
   feature **covered / partial / missing**, with a **risk** (high: money,
   bookings, orders, payments, table status; medium: lists, filters, settings;
   low: cosmetic).

   **Feature size and names must stay stable between analyses** — the team compares
   one map with the next:
   - one feature = one thing a tester checks: one user action and its outcome
     ("Waiter voids a line item", "Waiter comps a line item", "Waiter splits a line
     item" are three features, not one "assign/split/void/comp");
   - start from the previous analysis's list below: keep each feature and its exact
     name; add a feature only for a screen or capability it does not cover; drop
     one only when its screens are gone from the source; never merge or rename
     features for style. A previous feature that bundles several checks ("X / Y / Z")
     is split into one feature per check, once. Say in `report.md` which features
     were added, split or dropped, and why.

   Previous analysis ($previous_count features):
$previous_features
4. Write `$session_dir/feature_map.json`:
```json
{"generated": "$date", "mode": "$mode",
 "apps": {"consumer": [{"feature": "", "role": "consumer", "screens": [""],
                         "status": "covered|partial|missing", "risk": "high|medium|low",
                         "covered_by": ["flow ids"], "gap": "what no flow checks",
                         "scenario": "a scenario that would cover the gap, in one sentence",
                         "ids": ["real element ids it would use"]}],
          "business": []},
 "totals": {"consumer": {"covered": 0, "partial": 0, "missing": 0},
            "business": {"covered": 0, "partial": 0, "missing": 0}}}
```
5. In `report.md`, a short summary and the table of the high-risk gaps.

This session analyzes only: it does not run flows and does not change code
(`summary.json` lists no fixes). $generate_hint
