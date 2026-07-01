import requests
import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

class JiraIntegration:
    def __init__(self, base_url: str, email: str, token: str):
        self.base_url = base_url.rstrip('/')
        self.auth = (email, token)
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

    def create_issue(self, project_key: str, summary: str, description: str, issue_type: str = "Bug") -> str:
        """Create a Jira issue and return the issue key."""
        url = f"{self.base_url}/rest/api/2/issue"
        payload = {
            "fields": {
                "project": {"key": project_key},
                "summary": summary,
                "description": description,
                "issuetype": {"name": issue_type}
            }
        }
        
        try:
            res = requests.post(url, json=payload, auth=self.auth, headers=self.headers, timeout=10)
            res.raise_for_status()
            data = res.json()
            logger.info(f"Created Jira issue: {data.get('key')}")
            return data.get("key")
        except Exception as e:
            logger.error(f"Failed to create Jira issue: {e}")
            return None

    def attach_evidence(self, issue_key: str, file_path: str, filename: str) -> bool:
        """Attach a file to a Jira issue."""
        if not issue_key:
            return False
            
        url = f"{self.base_url}/rest/api/2/issue/{issue_key}/attachments"
        headers = {"X-Atlassian-Token": "no-check"}
        
        try:
            with open(file_path, 'rb') as f:
                files = {'file': (filename, f, 'application/octet-stream')}
                res = requests.post(url, files=files, auth=self.auth, headers=headers, timeout=30)
                res.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to attach evidence to {issue_key}: {e}")
            return False
