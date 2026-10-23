"""Real-time failure alerts — push a Slack message the moment something fails, not at run end."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Set

from automation.notifications.dispatch import send_slack_text, slack_enabled, dashboard_url

logger = logging.getLogger("realtime_alerts")


class RealtimeAlertService:
    def __init__(self) -> None:
        # Dedupe: never alert twice for the same (run, scope).
        self.alerted_runs: Set[str] = set()

    def check_and_alert(self, run_id: str, scenario_result: Dict[str, Any]) -> bool:
        """Alert immediately if a scenario/step failed. Returns True if an alert was sent."""
        status = str(scenario_result.get("status", "")).lower()
        if status not in ("failed", "error", "fail"):
            return False

        scenario = (scenario_result.get("scenario_name")
                    or scenario_result.get("name")
                    or scenario_result.get("scenario_id") or "a scenario")
        key = f"{run_id}:{scenario}"
        if key in self.alerted_runs:
            return False
        self.alerted_runs.add(key)

        error = (scenario_result.get("error")
                 or scenario_result.get("error_message")
                 or scenario_result.get("message") or "no detail")
        app = scenario_result.get("app") or scenario_result.get("bot_type") or ""
        text = (
            f":rotating_light: *Test failure* — `{scenario}`{f' ({app})' if app else ''}\n"
            f"> {str(error)[:400]}\n"
            f"Run: {dashboard_url()}/runs/{run_id}"
        )
        return self._send(text)

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

    def alert_run_complete(self, run_id: str, summary: Dict[str, Any]) -> bool:
        """One summary alert when a run finishes (only if something failed)."""
        key = f"{run_id}:complete"
        if key in self.alerted_runs:
            return False
        failed = int(summary.get("failed", 0) or 0)
        total = int(summary.get("total", 0) or 0)
        passed = int(summary.get("passed", total - failed) or 0)
        status = str(summary.get("status", "")).lower()
        if failed == 0 and status not in ("failed", "error"):
            return False  # all green → stay quiet
        self.alerted_runs.add(key)
        emoji = ":x:" if failed else ":white_check_mark:"
        text = (
            f"{emoji} *Run complete* — {passed}/{total} passed, *{failed} failed*\n"
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
