@AGENTS.md

## For Claude Code

- Known rig/staging/app issues — read before investigating a failure:
  `automation/knowledge/known_issues.md`.
- Procedures (also available as skills): `/run-all-heal`, `/heal-failure`,
  `/write-scenario`, `/analyze-apps`. Helper agents: `app-analyst`, `flow-debugger`.
- Platform actions go through `.venv/bin/python scripts/agent_tools.py` (`--help`).
- The AI Agent page runs these same procedures unattended (automation/claude_code/):
  each session works in its own git worktree and a person approves its changes.
