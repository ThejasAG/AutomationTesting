import requests
import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

class GitHubIntegration:
    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json"
        }
        self.base_url = "https://api.github.com"

    def fetch_pr_metadata(self, owner: str, repo: str, pr_number: int) -> Dict[str, Any]:
        """Fetch PR details"""
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls/{pr_number}"
        try:
            res = requests.get(url, headers=self.headers, timeout=10)
            res.raise_for_status()
            data = res.json()
            return {
                "title": data.get("title"),
                "author": data.get("user", {}).get("login"),
                "branch": data.get("head", {}).get("ref"),
                "commit_sha": data.get("head", {}).get("sha"),
                "body": data.get("body")
            }
        except Exception as e:
            logger.error(f"Failed to fetch PR {pr_number} metadata: {e}")
            return {}

    def fetch_commit_diff(self, owner: str, repo: str, commit_sha: str) -> str:
        """Fetch unified diff for a commit"""
        url = f"{self.base_url}/repos/{owner}/{repo}/commits/{commit_sha}"
        headers = self.headers.copy()
        headers["Accept"] = "application/vnd.github.v3.diff"
        try:
            res = requests.get(url, headers=headers, timeout=10)
            res.raise_for_status()
            return res.text
        except Exception as e:
            logger.error(f"Failed to fetch diff for {commit_sha}: {e}")
            return ""

    def comment_on_pr(self, owner: str, repo: str, pr_number: int, body: str) -> bool:
        """Post a comment on a PR"""
        url = f"{self.base_url}/repos/{owner}/{repo}/issues/{pr_number}/comments"
        try:
            res = requests.post(url, headers=self.headers, json={"body": body}, timeout=10)
            res.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Failed to comment on PR {pr_number}: {e}")
            return False
