**Tonight's full pass: run and heal every flow, analyze the apps, write new scenarios.**

Do the three parts in this order and keep an eye on the time (stop starting new
work at $stop_by):

### 1. Run and heal ($run_scope)
Follow `.claude/skills/run-all-heal/SKILL.md` and, for each real failure,
`.claude/skills/heal-failure/SKILL.md` (fix the automation when it is at fault,
verify on the simulators, report app/server bugs with evidence).

### 2. Analyze ($mode)
$mode_text
Follow `.claude/skills/analyze-apps/SKILL.md` and write
`$session_dir/feature_map.json` (the format is in that skill). Keep the feature list stable
(same names and size; one feature = one thing a tester checks; split a bundled one
once; add only for new screens). Previous analysis ($previous_count features):
$previous_features

### 3. New scenarios
For the highest-risk **missing** features from part 2 (payments, bookings, orders
first), write at most **$max_new** new scenarios following
`.claude/skills/write-scenario/SKILL.md`. Keep only scenarios that pass on the
simulators.

`flow_kitchen_demo` needs an order already waiting in the kitchen; "no order"
there is an unmet precondition, not a bug.
