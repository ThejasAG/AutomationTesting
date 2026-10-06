import os
import subprocess
from typing import List, Dict, Any, Optional
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
        
    def _run_git(self, args: List[str], repo_path: str):
        process = subprocess.Popen(
            ["git"] + args, cwd=repo_path,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        stdout, stderr = process.communicate()
        return process.returncode, stdout, stderr

    def extract_changed_files(
        self,
        repo_path: str,
        commit_sha: str = "HEAD",
        base_branch: Optional[str] = None,
    ) -> List[str]:
        """Files changed. For a PR, EVERY file the PR touches vs its base.

        `HEAD~1..HEAD` only ever showed the LAST COMMIT, so a 12-commit PR was
        judged on its final commit alone: touch payment in commit 1 and README in
        commit 12, and the payment tests were never selected.

        With *base_branch* this uses the three-dot form:

            git diff origin/<base>...<head> --name-only

        which diffs from the MERGE BASE — the whole PR against where it forked,
        without dragging in changes made on base since. That is the set a merge
        will actually apply, so "green alone, broken together" becomes visible.
        """
        try:
            if base_branch:
                base_ref = (base_branch if base_branch.startswith("origin/")
                            else f"origin/{base_branch}")
                head = commit_sha or "HEAD"

                # The merge base must exist locally or the diff is meaningless.
                self._run_git(["fetch", "origin", base_branch.replace("origin/", "")],
                              repo_path)

                code, out, err = self._run_git(
                    ["diff", "--name-only", f"{base_ref}...{head}"], repo_path)
                if code == 0:
                    return [f.strip() for f in out.split("\n") if f.strip()]

                logger.warning(
                    "PR diff %s...%s failed (%s) — falling back to the single "
                    "commit, which UNDER-REPORTS the PR.", base_ref, head, err.strip())

            if commit_sha == "HEAD":
                cmd = ["diff", "--name-only", "HEAD~1", "HEAD"]
            else:
                cmd = ["diff-tree", "--no-commit-id", "--name-only", "-r", commit_sha]

            code, out, err = self._run_git(cmd, repo_path)
            if code != 0:
                logger.error(f"Failed to extract git diff: {err}")
                return []
            return [f.strip() for f in out.split("\n") if f.strip()]
        except Exception as e:
            logger.error(f"Git diff extraction error: {e}")
            return []

    def extract_pr_changed_files(
        self, repo_path: str, head_sha: str, base_branch: str
    ) -> List[str]:
        """Every file a PR changes relative to its merge base with *base_branch*."""
        return self.extract_changed_files(repo_path, head_sha, base_branch=base_branch)

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
