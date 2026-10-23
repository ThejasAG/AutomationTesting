"""The AI Agent's plain (no-AI) parts, shared by its Claude Code sessions.

The agent itself is Claude Code — see automation/claude_code/. What lives here is
deterministic and free:

    settings.py      configuration (DB row over defaults) for the AI Agent page
    preflight.py     is the rig able to run at all? (Appium, sims, staging)
    classifier.py    rule-based failure sorter (INFRA / TIMING / LOCATOR / APP_BUG …)
    inventory.py     every screen of both apps, its element ids, and which the flows drive
    runinfo.py       runs and flows as text, @token validation, waiting for a run
    sources.py       where the app checkouts and the knowledge files are

The earlier API-key agent (its own Claude tool loop, triage, DB-override fixes and
batch scheduler) was retired on 2026-10-22: the agent runs on the Claude plan
through Claude Code only.
"""
