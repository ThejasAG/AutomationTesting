"""Real-time failure alerts — push a Slack message the moment something fails, not at run end."""

from __future__ import annotations

import logging
from typing import Set

from automation.notifications.dispatch import send_slack_text, slack_enabled, dashboard_url

logger = logging.getLogger("realtime_alerts")


class RealtimeAlertService:
    def __init__(self) -> None:
        # Dedupe: never alert twice for the same (run, scope).
        self.alerted_runs: Set[str] = set()

    def alert_ios_failure(self, run_id: str, test_name: str, error: str) -> bool:
        """Immediate alert for an iOS/Appium run failure."""
        key = f"{run_id}:ios"
        if key in self.alerted_runs:
            return False
        self.alerted_runs.add(key)
        text = (
            f":rotating_light: *iOS test failed* — `{test_name}`\n"
            f"> {str(error)[:400]}\n"
            f"Run: {dashboard_url()}/runs/{run_id}"
        )
        return self._send(text)

    def _send(self, text: str) -> bool:
        if not slack_enabled():
            logger.info("[realtime-alert suppressed, no SLACK_WEBHOOK_URL] %s", text.replace("\n", " ")[:200])
            return False
        try:
            return bool(send_slack_text(text))
        except Exception as e:
            logger.warning("realtime alert send failed: %s", e)
            return False


realtime_alert_service = RealtimeAlertService()
