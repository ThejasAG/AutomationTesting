"""Notification & ticketing dispatch — reads config from env and fans out.

Slack:  SLACK_WEBHOOK_URL
Jira:   JIRA_URL, JIRA_EMAIL, JIRA_TOKEN, JIRA_PROJECT_KEY
Common: DASHBOARD_URL (for links)

All calls are best-effort and no-op when unconfigured, so callers can invoke them
unconditionally.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger("notify")


def dashboard_url() -> str:
    return os.getenv("DASHBOARD_URL", "http://localhost:5173").rstrip("/")


def slack_enabled() -> bool:
    return bool(os.getenv("SLACK_WEBHOOK_URL"))


def jira_enabled() -> bool:
    return all(os.getenv(k) for k in ("JIRA_URL", "JIRA_EMAIL", "JIRA_TOKEN", "JIRA_PROJECT_KEY"))


def send_slack_text(text: str) -> bool:
    """Post a plain markdown-ish message to Slack."""
    webhook = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook:
        return False
    import httpx
    try:
        httpx.post(webhook, json={"text": text}, timeout=10)
        return True
    except Exception as e:
        logger.warning("Slack post failed: %s", e)
        return False


def notify_run_result(run_id: str, test_name: str, status: str, rca_data: Optional[dict] = None) -> None:
    """Rich Slack card for a run result (uses the existing Notifier)."""
    if not slack_enabled():
        return
    from automation.notifications.notifier import Notifier
    try:
        Notifier(slack_webhook=os.getenv("SLACK_WEBHOOK_URL")).notify_all(
            run_id, test_name, status, rca_data or {}, dashboard_url())
    except Exception as e:
        logger.warning("Slack notify failed: %s", e)


def create_jira_ticket(summary: str, description: str, evidence_path: Optional[str] = None) -> Optional[str]:
    """Create a Jira issue for a failure; returns the issue key (or None)."""
    if not jira_enabled():
        return None
    from automation.integrations.jira import JiraIntegration
    try:
        jira = JiraIntegration(os.getenv("JIRA_URL"), os.getenv("JIRA_EMAIL"), os.getenv("JIRA_TOKEN"))
        key = jira.create_issue(os.getenv("JIRA_PROJECT_KEY"), summary, description)
        if key and evidence_path and os.path.exists(evidence_path):
            jira.attach_evidence(key, evidence_path, os.path.basename(evidence_path))
        return key
    except Exception as e:
        logger.warning("Jira ticket creation failed: %s", e)
        return None


def jira_browse_url(key: str) -> str:
    base = (os.getenv("JIRA_URL") or "").rstrip("/")
    return f"{base}/browse/{key}" if base and key else ""


def status() -> dict:
    return {"slack": slack_enabled(), "jira": jira_enabled()}
