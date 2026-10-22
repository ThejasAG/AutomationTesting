**Write new regression scenarios for features the flows do not cover yet.**

What to cover: $focus

At most **$max_new** new scenarios. Follow `.claude/skills/write-scenario/SKILL.md`:

1. For each chosen feature, read its screens in the app source (`app-analyst` can
   help) and the existing flows that get closest. Reuse their setup blocks
   (login, booking, assign table…) — copy how `FLOWS` builds segments with `_seg`.
2. Add each scenario at the end of `FLOWS` in
   `automation/scenarios/cross_app_flows.py`, under the comment
   `# ── AI Agent drafts ──` (create it once): id `cc_<short_snake_name>`, a name
   starting with `[Draft] `, a description naming the feature and screens, steps
   with real ids only. A new kind of step needs a handler in `_handle_special`
   plus a unit test for it.
3. `check-flows`, `check-ids cc_<id>`, `unit-tests` for any test you added, then
   `run cc_<id> --label new`. Fix and run again if it fails for a test reason
   (at most $max_fix_attempts more runs). Keep a draft only if it passes; otherwise
   remove it completely and report it as dropped, with the reason.
4. A scenario that fails because the app is wrong is not dropped silently: keep
   the evidence and report it as an app bug.
