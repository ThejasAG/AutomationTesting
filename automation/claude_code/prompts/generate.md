**Write new regression scenarios for features the flows do not cover yet.**

What to cover:
$focus

At most **$max_new** new scenarios. Follow `.claude/skills/write-scenario/SKILL.md`
and `automation/knowledge/scenario_authoring.md`. The authoring guide holds what
earlier sessions spent half their time rediscovering, so read it first:

1. `steps` lists every step form, `@token` (with its handler) and shared block;
   `show-flow` the closest flow. Do not send helper agents to study the platform.
2. Each feature above lists its screens and ids. Read only those files and confirm
   the ids you will tap. With more than two features, draft them in parallel: one
   `app-analyst` per feature that has no screens listed, all in one message.
3. Write **all** drafts at the end of `FLOWS` in
   `automation/scenarios/cross_app_flows.py`, under the comment
   `# ── AI Agent drafts ──` (create it once): id `cc_<short_snake_name>`, a name
   starting with `[Draft] `, a description naming the feature and screens, steps
   with real ids only, and an outcome check (`@see:<text>`) at the end of each
   segment that does something. A new kind of step needs a handler in
   `_handle_special` plus a unit test for it.
4. `check-flows` once, `check-ids cc_<id>` per draft, `unit-tests` for the test
   files you added (never the whole suite: the checks after the session run it).
5. Run the drafts back to back: `run cc_<id> --label new`. Fix and run again if one
   fails for a test reason (at most $max_fix_attempts more runs each; failed in
   segment 2+ → `--resume <failed run>`). Keep a draft only if it passes; otherwise
   remove it completely and report it as dropped, with the reason. A pass stays
   valid when you later edit or remove ANOTHER draft. A change to a handler it
   uses, a shared block or the engine core means running it again.
6. A scenario that fails because the app is wrong is not dropped silently: keep
   the evidence and report it as an app bug.
