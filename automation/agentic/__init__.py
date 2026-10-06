"""Autonomous test agent — the AI Agent page.

Unattended pass over the cross-app flows (nightly or "Run now"), with failures
sorted, retried, diagnosed by Claude, fixed where that is safe, and reported.

    settings.py      configuration (DB row over .env defaults)
    preflight.py     is the rig able to run at all? (Appium, sims, staging)
    classifier.py    rule-based failure sorter — no AI, no cost
    claude.py        Claude client, the tool-use loop, cost accounting
    triage.py        Claude diagnoses one failed run
    fixer.py         applies / verifies / rolls back test fixes; app patch PRs
    generator.py     Claude proposes new scenarios from the app source
    orchestrator.py  the batch: run → sort → retry → triage → fix → report
    scheduler.py     fires the nightly batch and the weekly generator
    report.py        the morning email + Slack summary

Ground rules (enforced in code, not just in prompts):
  * the run itself is plain code; Claude is only called after a failure;
  * a test is never made to pass by weakening it — steps that check a value are
    never auto-edited;
  * app source is never modified in place — app fixes are patches, and become a
    PR only when that is switched on;
  * environment failures (staging 502, simulator down) are reported, not "fixed".
"""
