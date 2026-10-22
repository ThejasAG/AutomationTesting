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
