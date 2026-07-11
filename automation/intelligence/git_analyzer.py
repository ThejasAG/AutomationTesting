import os
import subprocess
from typing import List, Dict, Any
import logging

logger = logging.getLogger(__name__)

class GitImpactAnalyzer:
    def __init__(self):
        # Fallback mapping if not provided by automation.yaml
        self.default_module_map = {
            "Login": ["Auth", "Login", "SignIn", "Security"],
            "Registration": ["Signup", "Register", "Onboarding"],
            "Checkout": ["Cart", "Payment", "Checkout", "Order"],
            "Profile": ["User", "Settings", "Account"]
        }
        
    def extract_changed_files(self, repo_path: str, commit_sha: str = "HEAD") -> List[str]:
        """Extracts a list of files changed in the specified commit/branch."""
        try:
            # For this MVP, we look at the last commit if HEAD is passed
            cmd = ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit_sha]
            if commit_sha == "HEAD":
                cmd = ["git", "diff", "--name-only", "HEAD~1", "HEAD"]
                
            process = subprocess.Popen(
                cmd,
                cwd=repo_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            stdout, stderr = process.communicate()
            if process.returncode != 0:
                logger.error(f"Failed to extract git diff: {stderr}")
                return []
                
            files = [f.strip() for f in stdout.split('\n') if f.strip()]
            return files
        except Exception as e:
            logger.error(f"Git diff extraction error: {e}")
            return []

    def extract_files_from_webhook(self, payload: Dict[str, Any]) -> List[str]:
        """Extracts changed file paths from a GitHub webhook payload.

        - Push events: union of commits[].added + commits[].modified (+ removed),
          plus head_commit if present.
        - Pull request events: file paths from the pull_request payload when the
          diff/files list is included (head/base comparison).
        Returns a de-duplicated flat list of file paths.
        """
        files = set()

        # ── Push events ──────────────────────────────────────────────
        for commit in payload.get("commits", []) or []:
            files.update(commit.get("added", []) or [])
            files.update(commit.get("modified", []) or [])
            files.update(commit.get("removed", []) or [])

        head_commit = payload.get("head_commit") or {}
        if head_commit:
            files.update(head_commit.get("added", []) or [])
            files.update(head_commit.get("modified", []) or [])
            files.update(head_commit.get("removed", []) or [])

        # ── Pull request events (head/base comparison) ───────────────
        pr = payload.get("pull_request", {}) or {}
        if pr:
            for f in pr.get("files", []) or []:
                # GitHub's files API entries are dicts with a "filename";
                # tolerate plain-string lists too.
                fname = f.get("filename") if isinstance(f, dict) else f
                if fname:
                    files.add(fname)

        return [f for f in files if f]

    def identify_modules(self, changed_files: List[str], custom_map: Dict[str, List[str]] = None) -> List[str]:
        """Maps changed files to higher-level application modules."""
        mapping = custom_map or self.default_module_map
        affected_modules = set()
        
        for file_path in changed_files:
            file_name_lower = os.path.basename(file_path).lower()
            
            for module_name, keywords in mapping.items():
                for kw in keywords:
                    if kw.lower() in file_name_lower or kw.lower() in file_path.lower():
                        affected_modules.add(module_name)
                        break
                        
        return list(affected_modules)

git_analyzer = GitImpactAnalyzer()
