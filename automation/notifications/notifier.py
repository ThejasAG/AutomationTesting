"""Notifications Module"""

import httpx
import logging

class Notifier:
    def __init__(self, slack_webhook: str = None, teams_webhook: str = None):
        self.slack_webhook = slack_webhook
        self.teams_webhook = teams_webhook
        
    def send_slack(self, run_id: str, test_name: str, status: str, root_cause: str, severity: str, dashboard_link: str, rca_data: dict = None):
        if not self.slack_webhook:
            return
            
        color = "#36a64f" if status == "passed" else "#ff0000"
        device = rca_data.get("device_name", "Unknown") if isinstance(rca_data, dict) else "Unknown"
        suggested_fix = rca_data.get("suggested_fix", "No fix suggested") if isinstance(rca_data, dict) else "N/A"
        
        payload = {
            "attachments": [
                {
                    "color": color,
                    "title": f"Test Execution: {test_name}",
                    "title_link": dashboard_link,
                    "fields": [
                        {"title": "Status", "value": status, "short": True},
                        {"title": "Device", "value": device, "short": True},
                        {"title": "AI RCA", "value": root_cause, "short": False},
                        {"title": "Suggested Fix", "value": suggested_fix, "short": False}
                    ]
                }
            ]
        }
        
        try:
            httpx.post(self.slack_webhook, json=payload)
        except Exception as e:
            logging.error(f"Failed to send Slack notification: {str(e)}")

    def notify_all(self, run_id: str, test_name: str, status: str, rca_data: dict, dashboard_url: str):
        """Send notifications to all configured channels"""
        root_cause = rca_data.get("root_cause", "No AI analysis available") if status == "failed" else "N/A"
        severity = rca_data.get("severity", "Info") if status == "failed" else "Info"
        link = f"{dashboard_url}/run/{run_id}"
        
        # Log to console for demonstration
        print(f"\\n[NOTIFICATION] Test: {test_name} | Status: {status} | Severity: {severity}")
        print(f"[NOTIFICATION] Root Cause: {root_cause}")
        print(f"[NOTIFICATION] Dashboard: {link}\\n")
        
        if isinstance(rca_data, dict) and "device_name" not in rca_data:
            rca_data["device_name"] = "Unknown"
            
        self.send_slack(run_id, test_name, status, root_cause, severity, link, rca_data)
