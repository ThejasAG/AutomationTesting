import os
import requests
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


def _adf_to_text(node: Any) -> str:
    """Flatten Atlassian Document Format (rich description JSON) to plain text.
    REST v2 usually returns description as a plain string; v3/some issues return ADF."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return str(node)
    out: List[str] = []
    if node.get("type") == "text":
        out.append(node.get("text", ""))
    for child in (node.get("content") or []):
        out.append(_adf_to_text(child))
    if node.get("type") in ("paragraph", "heading", "listItem", "blockquote"):
        out.append("\n")
    return "".join(out)


_warned_credentials = False


def _warn_bad_credentials(base_url: str, email: str, code: int) -> None:
    """Say it once, loudly, with the fix — not once per PR."""
    global _warned_credentials
    if _warned_credentials:
        return
    _warned_credentials = True
    logger.error(
        "Jira rejected the credentials (HTTP %s) for %s as %s. Tickets will NOT load and "
        "failure tickets cannot be filed. Atlassian Basic auth is email:API-token and the "
        "token must belong to THAT account — generate one at "
        "id.atlassian.com > Security > API tokens, set JIRA_TOKEN, and verify with: "
        "curl -u '%s:<token>' %s/rest/api/3/myself",
        code, base_url, email, email, base_url,
    )


class JiraIntegration:
    def __init__(self, base_url: str, email: str, token: str):
        self.base_url = base_url.rstrip('/')
        self.auth = (email, token)
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

    @classmethod
    def from_env(cls) -> Optional["JiraIntegration"]:
        """Build from JIRA_URL / JIRA_EMAIL / JIRA_TOKEN env vars, or None if not
        configured — callers then fall back to locally-stored ticket text."""
        url = (os.getenv("JIRA_URL") or "").strip()
        email = (os.getenv("JIRA_EMAIL") or "").strip()
        token = (os.getenv("JIRA_TOKEN") or "").strip()
        if not (url and email and token):
            return None
        return cls(url, email, token)

    def _credentials_ok(self) -> Optional[bool]:
        """True/False if we could determine it, None if Jira was unreachable.
        Cached per instance — one /myself call, not one per PR."""
        if getattr(self, "_creds_ok", "unset") != "unset":
            return self._creds_ok
        try:
            res = requests.get(f"{self.base_url}/rest/api/3/myself", auth=self.auth,
                               headers=self.headers, timeout=10)
            self._creds_ok = False if res.status_code in (401, 403) else bool(res.ok)
        except Exception:
            self._creds_ok = None            # network problem — don't blame the token
        return self._creds_ok

    def check(self) -> Dict[str, Any]:
        """Is this configuration actually usable? Calls /myself — the one endpoint that
        separates 'bad credentials' from 'no such issue'. Jira answers 404 for an issue you
        cannot see, so an auth failure otherwise looks exactly like a missing ticket."""
        out: Dict[str, Any] = {"url": self.base_url, "email": self.auth[0], "ok": False}
        try:
            res = requests.get(f"{self.base_url}/rest/api/3/myself", auth=self.auth,
                               headers=self.headers, timeout=10)
        except Exception as e:
            out["error"] = f"Could not reach Jira: {e}"
            return out
        out["status_code"] = res.status_code
        if res.status_code in (401, 403):
            out["error"] = (
                f"Jira rejected the credentials (HTTP {res.status_code}). The API token must "
                f"belong to {self.auth[0]}. Create one at id.atlassian.com > Security > "
                f"API tokens and set JIRA_TOKEN."
            )
            return out
        if not res.ok:
            out["error"] = f"Jira returned HTTP {res.status_code}."
            return out
        me = res.json() or {}
        out.update(ok=True, account=me.get("emailAddress") or me.get("displayName"))
        out["projects"] = self.list_project_keys()
        want = (os.getenv("JIRA_PROJECT_KEY") or "").strip()
        out["project_key"] = want or None
        if not want:
            out["warning"] = ("JIRA_PROJECT_KEY is not set — tickets cannot be FILED on "
                              f"failure. Visible projects: {out['projects'] or '(none)'}")
        elif out["projects"] and want not in out["projects"]:
            out["warning"] = (f"JIRA_PROJECT_KEY={want} is not among the visible projects "
                              f"{out['projects']}.")
        return out

    def list_project_keys(self) -> List[str]:
        """All Jira project keys (e.g. ['NEWVYA', …]) so PR key-extraction only matches
        REAL projects — a branch like 'NEWVYA-BUGS-LIST-2' otherwise yields a bogus 'LIST-2'
        that 404s. Returns [] on failure (caller falls back to a generic pattern)."""
        try:
            res = requests.get(f"{self.base_url}/rest/api/2/project", auth=self.auth,
                               headers=self.headers, timeout=10)
            res.raise_for_status()
            return [p.get("key") for p in (res.json() or []) if p.get("key")]
        except Exception as e:
            logger.warning("Failed to list Jira projects: %s", e)
            return []

    def get_issue(self, key: str) -> Dict[str, Any]:
        """Fetch a Jira issue's summary + description by key (e.g. 'NEWVYA-1134').
        Returns {key, summary, description, status, url} or {} on any failure —
        never raises, so ticket→PR linking degrades gracefully."""
        if not key:
            return {}
        url = f"{self.base_url}/rest/api/2/issue/{key}"
        try:
            res = requests.get(url, auth=self.auth, headers=self.headers,
                               params={"fields": "summary,description,status"}, timeout=10)
            if res.status_code in (401, 403):
                # A CONFIG error, not a missing ticket. This used to fall through to the
                # generic `return {}` below, so an invalid token was indistinguishable from
                # "this PR has no ticket" — Jira had been rejecting every request with 401
                # while the UI simply showed no ticket and nobody knew.
                _warn_bad_credentials(self.base_url, self.auth[0], res.status_code)
                return {}
            if res.status_code == 404:
                # Jira answers 404 for BOTH "no such issue" and "you may not see it" — and
                # a rejected token produces the second. So a 404 alone cannot tell a
                # key-shaped branch fragment apart from a completely broken credential,
                # which is exactly how a 401-ing token stayed invisible: every PR simply
                # showed "no ticket". Verify the credentials ONCE, then decide.
                if self._credentials_ok() is False:
                    _warn_bad_credentials(self.base_url, self.auth[0], 401)
                else:
                    logger.debug("Jira issue %s not found (404) — skipping", key)
                return {}
            res.raise_for_status()
            fields = (res.json() or {}).get("fields", {}) or {}
            desc = fields.get("description")
            if isinstance(desc, dict):          # ADF rich text → flatten
                desc = _adf_to_text(desc)
            return {
                "key": key,
                "summary": fields.get("summary"),
                "description": (desc or "").strip(),
                "status": ((fields.get("status") or {}).get("name")),
                "url": f"{self.base_url}/browse/{key}",
            }
        except Exception as e:
            logger.warning("Failed to fetch Jira issue %s: %s", key, e)
            return {}

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
