"""Agent configuration: one DB row (edited from the AI Agent page) over defaults.

Secrets are NOT settings: ANTHROPIC_API_KEY, SMTP_PASSWORD and GITHUB_TOKEN stay
in .env. The page only shows whether each is configured.
"""
from __future__ import annotations

import os
from typing import Any, Dict

from automation.database.config import SessionLocal
from automation.database.models import AgentSetting

# Quick demos are smoke checks for a live demo, not regression coverage.
DEMO_FLOWS = ("flow_book_demo", "flow_waiter_demo", "flow_kitchen_demo")

DEFAULTS: Dict[str, Any] = {
    "enabled": False,               # nightly schedule on/off (Run now always works)
    "nightly_time": "01:00",        # local time, HH:MM
    "env": "staging",               # staging | prod
    "flows": [],                    # [] = every flow except the quick demos
    "include_demos": False,
    "model": "claude-opus-5-5",
    "budget_usd": 10.0,             # hard cap on Claude spend per batch
    "triage": True,                 # ask Claude to diagnose failures
    "autofix_tests": True,          # apply + verify test-step fixes
    "app_fix_mode": "propose",      # off | propose (patch only) | pr (push branch + PR)
    "max_fix_attempts": 2,
    "infra_wait_minutes": 15,       # how long to wait for staging to come back
    "report_emails": [],            # [] = REPORT_EMAIL_TO from .env
    "report_slack": True,
    "generator_enabled": False,     # weekly: propose new scenarios
    "generator_day": "sun",         # mon..sun
}

_ROW_ID = "default"


def get() -> Dict[str, Any]:
    with SessionLocal() as db:
        row = db.get(AgentSetting, _ROW_ID)
        stored = dict(row.data or {}) if row else {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in stored.items() if k in DEFAULTS})
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
    return {
        "anthropic": bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")),
        "smtp": smtp,
        "slack": bool(os.getenv("SLACK_WEBHOOK_URL")),
        "github": bool(os.getenv("GITHUB_TOKEN")),
    }
