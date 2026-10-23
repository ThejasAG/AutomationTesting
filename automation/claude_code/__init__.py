"""The AI Agent: Claude Code (the CLI, on the logged-in Claude plan — no API key)
analyzing the Vyapy apps, writing scenarios, and fixing the automation when it fails.

    session.py    start / follow / stop / approve / reject (API + CLI)
    runner.py     one session end to end, in its own process: worktree → claude -p → gates → branch
    prompts/      what each kind of session is told (common.md + heal / fix_run / analyze / generate / nightly)
    guard.py      PreToolUse hook: re-checks every tool call against the session's limits
    gates.py      the checks a change must pass before a person can approve it
    impact.py     which flows a change to the flow engine reaches
    baselines.py  unit-test failures per commit (so a session is blamed only for new ones)
    events.py     the transcript as short lines for the AI Agent page
    report.py     email + Slack when a session ends
    schedule.py   the nightly launchd job
    cli.py        the same from a terminal

Kinds of session:
    heal      run the flows (or the failures a Run all just had) and fix what the automation got wrong
    fix_run   one failed run, from its Run Details page
    analyze   map both apps' features against the flows: covered / partial / missing, with risk
    generate  write new flows for the gaps; keep only the ones that pass on the simulators
    nightly   heal + analyze + generate, unattended

Ground rules (enforced in code, not only in the prompt):
  * a session works in its own git worktree; nothing reaches the person's checkout until
    they approve it on the AI Agent page (merge into the current branch, never main);
  * it may edit only automation/, tests/ and scripts/ — not its own guardrails, the app
    checkouts or secrets — and may run only scripts/agent_tools.py and read-only git;
  * a change is approvable only when every gate passed: it compiles, the flows load, no
    value check was removed, the unit suite has no new failure, and the flows it touches
    passed on the simulators after the last edit;
  * a person's runs always go first on the simulators.
"""
