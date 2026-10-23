"""AI Agent configuration: one DB row (edited from the AI Agent page) over defaults.

The agent is Claude Code on the logged-in Claude plan — there is no API key to
configure. Secrets that remain (SMTP_PASSWORD, SLACK_WEBHOOK_URL, GITHUB_TOKEN)
stay in .env; the page only shows whether each is configured.
"""
from __future__ import annotations

import os
from typing import Any, Dict

from automation.database.config import SessionLocal
from automation.database.models import AgentSetting

# Quick demos are smoke checks for a live demo, not regression coverage.
DEMO_FLOWS = ("flow_book_demo", "flow_waiter_demo", "flow_kitchen_demo")

DEFAULTS: Dict[str, Any] = {
    "nightly_enabled": True,        # launchd job (survives reboots) — see claude_code/schedule.py
    "nightly_time": "01:00",        # local time, HH:MM
    "nightly_mode": "auto",         # auto (full analysis on Sunday, delta otherwise) | delta | full
    "auto_heal_after_run_all": True,  # a dashboard Run all with failures starts a heal session
    "env": "staging",
    "include_demos": False,         # heal/nightly: also run the quick demo flows
    "model": "opus",                # the session's model (helper agents: see .claude/agents/)
    "effort": "high",
    "max_new_scenarios": 3,         # per generate / nightly session (full analysis: double)
    "max_fix_attempts": 2,          # per failure, before it is left for a person
    "max_changed_lines": 800,       # a bigger change is not approvable
    "hours": {"heal": 4.0, "fix_run": 1.5, "analyze": 1.5, "generate": 3.0, "nightly": 5.0},
    "report_emails": [],            # [] = REPORT_EMAIL_TO from .env
    "report_slack": True,
    "report_manual_sessions": False,  # also email sessions started by hand
}

_ROW_ID = "default"


def get() -> Dict[str, Any]:
    with SessionLocal() as db:
        row = db.get(AgentSetting, _ROW_ID)
        stored = dict(row.data or {}) if row else {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in stored.items() if k in DEFAULTS})
    out["hours"] = {**DEFAULTS["hours"], **(out.get("hours") or {})}
    if not out["report_emails"]:
        env = os.getenv("REPORT_EMAIL_TO", "")
        out["report_emails"] = [e.strip() for e in env.split(",") if e.strip()]
    return out


def update(changes: Dict[str, Any]) -> Dict[str, Any]:
    clean = {k: v for k, v in (changes or {}).items() if k in DEFAULTS}
    with SessionLocal() as db:
        row = db.get(AgentSetting, _ROW_ID)
        if row is None:
            row = AgentSetting(id=_ROW_ID, data={})
            db.add(row)
        data = dict(row.data or {})
        data.update(clean)
        row.data = data
        db.commit()
    return get()


def secrets_status() -> Dict[str, bool]:
    """Which secrets are configured — never their values."""
    smtp = all(os.getenv(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"))
    return {"smtp": smtp, "slack": bool(os.getenv("SLACK_WEBHOOK_URL")),
            "github": bool(os.getenv("GITHUB_TOKEN"))}
