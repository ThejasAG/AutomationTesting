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

    @staticmethod
    def _shape_pr(pr: Dict[str, Any]) -> Dict[str, Any]:
        merged_at = pr.get("merged_at")
        state = "merged" if merged_at else pr.get("state", "open")
        return {
            "number": pr["number"],
            "title": pr["title"],
            "author": pr.get("user", {}).get("login"),
            "branch": pr.get("head", {}).get("ref"),
            "base": pr.get("base", {}).get("ref"),
            "commit_sha": pr.get("head", {}).get("sha"),
            "draft": pr.get("draft", False),
            "state": state,
            "merged_at": merged_at,
            "updated_at": pr.get("updated_at"),
            "url": pr.get("html_url"),
        }

    def _fetch_prs(self, owner: str, repo: str, state: str, per_page: int = 50) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/repos/{owner}/{repo}/pulls"
        res = requests.get(
            url,
            headers=self.headers,
            params={"state": state, "sort": "updated", "direction": "desc", "per_page": per_page},
            timeout=15,
        )
        res.raise_for_status()
        return res.json()

    def list_open_prs(self, owner: str, repo: str) -> List[Dict[str, Any]]:
        """Open pull requests for a repo, newest first. Raises on API error so the
        caller can distinguish 'no PRs' from 'bad token / rate limited'."""
        return [self._shape_pr(pr) for pr in self._fetch_prs(owner, repo, "open")]

    def list_prs(self, owner: str, repo: str, merged_limit: int = 20) -> List[Dict[str, Any]]:
        """Open PRs plus the most recently MERGED PRs (newest first). Merged PRs are
        what 'pull the latest merge to test' targets — their code already lives on the
        base branch, so testing one = rebuild the base branch. Closed-but-not-merged
        PRs are omitted (nothing to test). Raises on API error."""
        prs = [self._shape_pr(pr) for pr in self._fetch_prs(owner, repo, "open")]
        # Closed list is sorted by updated desc; keep only the ones actually merged.
        merged = [self._shape_pr(pr) for pr in self._fetch_prs(owner, repo, "closed", per_page=50)
                  if pr.get("merged_at")][:merged_limit]
        return prs + merged

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
                "base": data.get("base", {}).get("ref"),
                "commit_sha": data.get("head", {}).get("sha"),
                "merged_at": data.get("merged_at"),
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
