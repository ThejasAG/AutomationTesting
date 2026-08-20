import os
import subprocess
import shutil
import sys
import tempfile
import yaml
import logging
from typing import Optional, Dict, Any

from automation.projects.config import AutomationYamlConfig

logger = logging.getLogger(__name__)

# Paths that must never be touched by git operations (they are local build
# artifacts, not part of the tracked source tree).
_IGNORED_PATHS = [".venv/", "__pycache__/"]

class RepositoryManager:
    # TRACKED files the platform regenerates locally. They must survive the
    # worktree reset, or the regeneration cost is paid on every single run.
    REGENERATED_TRACKED = ["ios/Podfile.lock"]

    def __init__(self, repos_base_dir: str = "repos"):
        self.base_dir = os.path.abspath(repos_base_dir)
        if not os.path.exists(self.base_dir):
            os.makedirs(self.base_dir)

    def get_repo_path(self, project_id: str) -> str:
        return os.path.join(self.base_dir, project_id)

    def _run_git(self, args: list, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
        cmd = ["git"] + args
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)

    # ── Remote branch resolution ─────────────────────────────────────────────

    def get_remote_default_branch(self, git_url: str) -> Optional[str]:
        """The branch the remote's HEAD points at (e.g. 'main' or 'master').

        Returns None when the remote cannot be reached, so callers keep the
        requested branch and let git report the real error (auth, bad url).
        """
        res = self._run_git(["ls-remote", "--symref", git_url, "HEAD"])
        if res.returncode != 0:
            return None
        for line in res.stdout.splitlines():
            # Format: "ref: refs/heads/master\tHEAD"
            if line.startswith("ref:"):
                parts = line.split(maxsplit=2)
                if len(parts) >= 2 and parts[1].startswith("refs/heads/"):
                    return parts[1][len("refs/heads/"):]
        return None

    def remote_branch_exists(self, git_url: str, branch: str) -> bool:
        res = self._run_git(["ls-remote", "--heads", git_url, branch])
        return res.returncode == 0 and bool(res.stdout.strip())

    def resolve_branch(self, git_url: str, branch: str) -> str:
        """Return *branch* when the remote has it, else the remote's default.

        A project configured for `main` against a repo that only has `master`
        (or vice versa) otherwise fails every clone/checkout/pull with
        "Remote branch not found in upstream origin".
        """
        if branch and self.remote_branch_exists(git_url, branch):
            return branch

        default = self.get_remote_default_branch(git_url)
        if not default:
            return branch

        if branch and default != branch:
            logger.warning(
                f"Branch '{branch}' not found on {git_url} — using default branch '{default}'"
            )
        return default

    def _ensure_gitignore(self, repo_path: str) -> None:
        """Ensure .venv/ and __pycache__/ are ignored so git never manages them.

        Appends any missing entries to the repo's .gitignore (creating it if
        absent). This keeps the virtual environment and bytecode caches out of
        every checkout / clean operation below.
        """
        gitignore_path = os.path.join(repo_path, ".gitignore")
        existing_lines: list = []
        if os.path.exists(gitignore_path):
            try:
                with open(gitignore_path, "r") as f:
                    existing_lines = [ln.strip() for ln in f.readlines()]
            except Exception as e:
                logger.warning(f"Could not read .gitignore in {repo_path}: {e}")
                return

        missing = [p for p in _IGNORED_PATHS if p not in existing_lines]
        if not missing:
            return

        try:
            needs_newline = bool(existing_lines) and existing_lines[-1] != ""
            with open(gitignore_path, "a") as f:
                if needs_newline:
                    f.write("\n")
                for entry in missing:
                    f.write(entry + "\n")
            logger.info(f"Added {missing} to .gitignore in {repo_path}")
        except Exception as e:
            logger.warning(f"Could not update .gitignore in {repo_path}: {e}")

    def _reset_worktree(self, repo_path: str) -> None:
        """Discard local edits before pulling, while preserving local artifacts.

        1. `git checkout -- .` reverts modifications to *tracked* files only
           (the venv/pycache are ignored/untracked, so they are never touched).
        2. `git clean -fd` with excludes removes untracked cruft but KEEPS:
             - .venv / __pycache__  — expensive to rebuild
             - automation.yaml      — a platform-generated config is untracked,
               so an unguarded clean would delete it on every pull and the
               dashboard would keep re-prompting "automation.yaml not found".
           We intentionally omit `-x` so ignored paths are left alone; the
           excludes are belt-and-braces in case they are not yet ignored.
        """
        # Make sure the venv/pycache are ignored before any clean runs.
        self._ensure_gitignore(repo_path)

        # 1. Revert tracked files only — untracked venv/pycache are unaffected.
        #
        #    ...except files the platform REGENERATES. ios/Podfile.lock is tracked,
        #    and the committed one does not match the Podfile, so the builder has to
        #    regenerate it. Reverting it here restores the stale committed copy, the
        #    builder detects drift again, and pays a full `pod install --repo-update`
        #    — several minutes — on EVERY run, forever. Excluding it lets the
        #    regenerated lock persist, so pod install settles into a fast no-op.
        #    (Local only. Nothing here is ever committed or pushed.)
        res = self._run_git(
            ["checkout", "--", ".", *(f":(exclude){p}" for p in self.REGENERATED_TRACKED)],
            cwd=repo_path,
        )
        if res.returncode != 0:
            logger.warning(f"git checkout -- . reported: {res.stderr.strip()}")

        # 2. Remove untracked cruft but keep the venv, caches, any generated
        #    automation.yaml, and the test suite.
        #
        #    The test-suite excludes matter: a suite that has been written but not
        #    yet COMMITTED is untracked, so an unguarded `git clean` deletes it on
        #    the very next sync — silently destroying the user's tests and then
        #    reporting "no tests to run". (This is not hypothetical; it happened.)
        res = self._run_git(
            [
                "clean", "-fd",
                "-e", ".venv",
                "-e", "__pycache__",
                "-e", "automation.yaml",
                "-e", "e2e",
                "-e", "tests",
                "-e", "test",
                "-e", "pytest.ini",
                "-e", "conftest.py",
                # .yarnrc.yml is what stops Yarn Berry from using Plug'n'Play.
                # Deleting it does not merely lose a setting: the NEXT install
                # silently switches to PnP, which DELETES node_modules entirely
                # and writes .pnp.cjs instead. React Native cannot use PnP, so the
                # app then fails to bundle (@babel/runtime unresolvable) — i.e.
                # cleaning this one file destroys the whole working tree.
                "-e", ".yarnrc.yml",
            ],
            cwd=repo_path,
        )
        if res.returncode != 0:
            logger.warning(f"git clean reported: {res.stderr.strip()}")

    def _stash_venv(self, repo_path: str) -> Optional[str]:
        """Move ``.venv`` OUT of the repo to a temp dir before git touches it.

        Returns the temp path holding the venv, or None when there is no venv to
        protect. Moving it outside the working tree means no git operation
        (clean, checkout, pull, reset) can possibly wipe it.
        """
        venv_path = os.path.join(repo_path, ".venv")
        if not os.path.isdir(venv_path):
            return None

        tmp_dir = tempfile.mkdtemp(prefix="venv-stash-")
        stashed = os.path.join(tmp_dir, ".venv")
        try:
            shutil.move(venv_path, stashed)
            logger.info(f"Stashed .venv to {stashed} for the duration of the pull")
            return stashed
        except Exception as e:
            logger.error(f"Could not stash .venv out of {repo_path}: {e}")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return None

    def _restore_venv(self, repo_path: str, stashed: Optional[str]) -> None:
        """Move a stashed ``.venv`` back into the repo. Safe to call with None."""
        if not stashed:
            return

        venv_path = os.path.join(repo_path, ".venv")
        tmp_dir = os.path.dirname(stashed)
        try:
            # A pull should never recreate .venv, but if something did, the
            # stashed copy is the authoritative one.
            if os.path.isdir(venv_path):
                shutil.rmtree(venv_path, ignore_errors=True)
            shutil.move(stashed, venv_path)
            logger.info(f"Restored .venv into {repo_path}")
        except Exception as e:
            logger.error(f"Could not restore .venv into {repo_path}: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def clone_or_pull(self, project_id: str, git_url: str, branch: str = "main") -> bool:
        """Clones if missing, pulls if exists.

        On pull the project's ``.venv`` is moved OUT of the working tree first
        and moved back afterwards — including when the pull fails — so no git
        operation can ever wipe it and dependencies are not reinstalled on every
        run. ``.venv/`` and ``__pycache__/`` are also added to the repo's
        .gitignore automatically.
        """
        repo_path = self.get_repo_path(project_id)

        if not os.path.exists(repo_path):
            target = self.resolve_branch(git_url, branch)
            logger.info(f"Cloning {git_url} (branch {target}) into {repo_path}")
            res = self._run_git(["clone", "-b", target, git_url, repo_path])
            if res.returncode != 0:
                logger.error(f"Clone failed: {res.stderr}")
                # A partial clone leaves a directory behind that the next
                # attempt would mistake for an existing checkout.
                shutil.rmtree(repo_path, ignore_errors=True)
                return False
            # Ensure a fresh clone ignores the venv/pycache from the start.
            self._ensure_gitignore(repo_path)
            return True

        logger.info(f"Pulling latest for {project_id}")

        # 1. Move the venv completely outside the repo so git cannot touch it.
        stashed_venv = self._stash_venv(repo_path)

        try:
            # 2. Discard local tracked edits and clean untracked cruft. With the
            #    venv stashed away, this is now safe unconditionally.
            self._reset_worktree(repo_path)

            # 3. Pull.
            res = self._run_git(["pull", "origin", branch], cwd=repo_path)
            if res.returncode != 0:
                # Restore the venv before surfacing the failure.
                self._restore_venv(repo_path, stashed_venv)
                stashed_venv = None
                logger.error(f"Pull failed: {res.stderr}")
                raise RuntimeError(f"git pull failed for {project_id}: {res.stderr.strip()}")

        except RuntimeError:
            raise
        except Exception as e:
            # Any unexpected error must not strand the venv outside the repo.
            self._restore_venv(repo_path, stashed_venv)
            stashed_venv = None
            logger.error(f"Pull aborted for {project_id}: {e}")
            raise
        finally:
            # 4. Always put the venv back (no-op if already restored above).
            self._restore_venv(repo_path, stashed_venv)

        return True

    # ── Explicit repository lifecycle operations ─────────────────────────────

    def is_cloned(self, project_id: str) -> bool:
        """True when the repo directory exists AND is a real git checkout."""
        repo_path = self.get_repo_path(project_id)
        return os.path.isdir(os.path.join(repo_path, ".git"))

    def clone(self, project_id: str, git_url: str, branch: str = "main") -> None:
        """Clone the repository. Raises RuntimeError on failure."""
        repo_path = self.get_repo_path(project_id)
        if self.is_cloned(project_id):
            logger.info(f"{project_id} already cloned — nothing to do")
            return

        # A leftover non-git directory would make `git clone` fail; clear it.
        if os.path.exists(repo_path):
            shutil.rmtree(repo_path, ignore_errors=True)

        target = self.resolve_branch(git_url, branch)
        logger.info(f"Cloning {git_url} (branch {target}) into {repo_path}")
        res = self._run_git(["clone", "-b", target, git_url, repo_path])
        if res.returncode != 0:
            logger.error(f"Clone failed: {res.stderr}")
            # A partial clone leaves a directory behind that the next attempt
            # would mistake for an existing checkout.
            shutil.rmtree(repo_path, ignore_errors=True)
            raise RuntimeError(f"git clone failed: {res.stderr.strip()}")

        self._ensure_gitignore(repo_path)

    def re_clone(self, project_id: str, git_url: str, branch: str = "main") -> None:
        """Delete the local checkout and clone it again from scratch."""
        self.delete_local_repo(project_id)
        self.clone(project_id, git_url, branch)

    def checkout_branch(self, project_id: str, branch: str) -> None:
        """Check out *branch*, fetching it from origin when it is not local yet."""
        repo_path = self.get_repo_path(project_id)
        if not self.is_cloned(project_id):
            raise RuntimeError(f"Cannot checkout: {project_id} is not cloned")

        if self.get_current_branch(project_id) == branch:
            return

        # Discard local edits FIRST. Every successful run dirties the tree — preparation
        # rewrites automation.yaml, the RN compatibility repair rewrites package.json and
        # yarn.lock, and stray .pyc files get tracked — so the NEXT job that needs a
        # different branch died with "Your local changes would be overwritten by checkout".
        # pull() has always reset before pulling; switching branches needs the same, or a
        # PR test can never run after a build has happened.
        self._reset_worktree(repo_path)

        # Make sure the ref is known locally before switching to it.
        self._run_git(["fetch", "origin", branch], cwd=repo_path)

        res = self._run_git(["checkout", branch], cwd=repo_path)
        if res.returncode != 0:
            # Branch may only exist on the remote — create a tracking branch.
            res = self._run_git(
                ["checkout", "-B", branch, f"origin/{branch}"], cwd=repo_path
            )
            if res.returncode != 0:
                raise RuntimeError(f"git checkout {branch} failed: {res.stderr.strip()}")

        logger.info(f"Checked out branch {branch} for {project_id}")

    def pull(self, project_id: str, branch: str = "main") -> None:
        """Pull latest changes, preserving .venv. Raises RuntimeError on failure."""
        repo_path = self.get_repo_path(project_id)
        if not self.is_cloned(project_id):
            raise RuntimeError(f"Cannot pull: {project_id} is not cloned")

        stashed_venv = self._stash_venv(repo_path)
        try:
            self._reset_worktree(repo_path)
            # REBASE, don't merge. Some checkouts carry deliberate local commits — the
            # staging apps are the prod repo plus patches that set their own bundle id
            # ("vyaconsumerstaging"), app name and staging API URL, all marked
            # "do not push". Once upstream moves on, plain `git pull` sees divergent
            # branches and refuses ("need to specify how to reconcile them"), which is
            # what broke Latest build. Rebase replays those patches on top of the new
            # upstream, so the checkout gets the latest code AND stays staging.
            # `reset --hard` would take the latest code and silently destroy them.
            # autoStash because _reset_worktree deliberately KEEPS ios/Podfile.lock
            # modified (reverting it costs a multi-minute `pod install --repo-update`
            # on every run). Rebase refuses to start with unstaged changes, so without
            # this the pull fails on exactly the repos the reset was tuned for.
            res = self._run_git(
                ["-c", "rebase.autoStash=true", "pull", "--rebase", "origin", branch],
                cwd=repo_path,
            )
            if res.returncode != 0:
                # Never leave the repo mid-rebase — that breaks every later run with a
                # confusing state rather than a clear error.
                self._run_git(["rebase", "--abort"], cwd=repo_path)
                self._restore_venv(repo_path, stashed_venv)
                stashed_venv = None
                logger.error(f"Pull (rebase) failed: {res.stderr}")
                raise RuntimeError(
                    f"git pull --rebase failed: {res.stderr.strip()[:400]}"
                )
        except RuntimeError:
            raise
        except Exception as e:
            self._restore_venv(repo_path, stashed_venv)
            stashed_venv = None
            raise RuntimeError(f"Pull aborted for {project_id}: {e}")
        finally:
            self._restore_venv(repo_path, stashed_venv)

    def delete_local_repo(self, project_id: str) -> bool:
        """Remove the local clone from disk. Returns True if anything was deleted."""
        repo_path = self.get_repo_path(project_id)
        if not os.path.exists(repo_path):
            return False
        shutil.rmtree(repo_path, ignore_errors=True)
        logger.info(f"Deleted local repository {repo_path}")
        return not os.path.exists(repo_path)

    def get_current_branch(self, project_id: str) -> Optional[str]:
        if not self.is_cloned(project_id):
            return None
        res = self._run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"], cwd=self.get_repo_path(project_id)
        )
        return res.stdout.strip() or None if res.returncode == 0 else None

    def has_remote_updates(self, project_id: str, branch: str = "main") -> bool:
        """True when origin/<branch> is ahead of the local checkout (i.e. outdated)."""
        if not self.is_cloned(project_id):
            return False
        repo_path = self.get_repo_path(project_id)

        # A fetch is required for the remote ref to be up to date.
        fetch = self._run_git(["fetch", "origin", branch], cwd=repo_path)
        if fetch.returncode != 0:
            # Offline / no remote access — cannot tell, assume up to date.
            return False

        res = self._run_git(
            ["rev-list", "--count", f"HEAD..origin/{branch}"], cwd=repo_path
        )
        if res.returncode != 0:
            return False
        try:
            return int(res.stdout.strip() or "0") > 0
        except ValueError:
            return False

    def validate_yaml(self, project_id: str) -> Optional[AutomationYamlConfig]:
        """Parses and validates automation.yaml."""
        repo_path = self.get_repo_path(project_id)
        yaml_path = os.path.join(repo_path, "automation.yaml")

        if not os.path.exists(yaml_path):
            logger.error(f"automation.yaml not found in {repo_path}")
            return None

        try:
            with open(yaml_path, 'r') as f:
                data = yaml.safe_load(f)
            return AutomationYamlConfig(**data)
        except Exception as e:
            logger.error(f"Invalid automation.yaml: {e}")
            return None

    def get_venv_path(self, project_id: str) -> str:
        """Returns path to the virtual environment for a given project."""
        repo_path = self.get_repo_path(project_id)
        return os.path.join(repo_path, ".venv")

    def get_python_executable(self, project_id: str) -> str:
        """Absolute path to the interpreter that should run this project's tests.

        Only Python projects get a repo-local ``.venv``. A React Native / Flutter /
        native project still runs its Appium suite through pytest, so falling back
        to the interpreter running the agent (which already has pytest and the
        Appium client) is what keeps those projects executable at all.
        """
        venv_path = self.get_venv_path(project_id)
        exe = (
            os.path.join(venv_path, "Scripts", "python.exe") if os.name == 'nt'
            else os.path.join(venv_path, "bin", "python")
        )
        if os.path.exists(exe):
            return exe
        logger.info(
            f"No repo-local .venv for {project_id} — running tests with the agent's "
            f"interpreter ({sys.executable})."
        )
        return sys.executable

    def ensure_venv_exists(self, project_id: str) -> bool:
        """Creates a venv if it doesn't exist."""
        venv_path = self.get_venv_path(project_id)
        if not os.path.exists(venv_path):
            logger.info(f"Creating virtual environment for {project_id}")
            res = subprocess.run(["python", "-m", "venv", ".venv"], cwd=self.get_repo_path(project_id), capture_output=True, text=True)
            if res.returncode != 0:
                logger.error(f"Venv creation failed: {res.stderr}")
                return False
        return True

    def install_dependencies(self, project_id: str, requirements_file: str = "requirements.txt") -> bool:
        """Installs dependencies inside the project's venv."""
        repo_path = self.get_repo_path(project_id)
        req_path = os.path.join(repo_path, requirements_file)

        if not os.path.exists(req_path):
            logger.warning(f"No {requirements_file} found in {repo_path}")
            return True

        python_exe = self.get_python_executable(project_id)
        logger.info(f"Installing dependencies for {project_id} using {python_exe}")

        res = subprocess.run([python_exe, "-m", "pip", "install", "-r", requirements_file], cwd=repo_path, capture_output=True, text=True)
        if res.returncode != 0:
            logger.error(f"Dependency installation failed: {res.stderr}")
            return False
        return True

    def get_git_metadata(self, project_id: str) -> Dict[str, Any]:
        """Fetch current commit info."""
        repo_path = self.get_repo_path(project_id)
        if not os.path.exists(repo_path):
            return {}

        try:
            commit_sha = self._run_git(["rev-parse", "HEAD"], cwd=repo_path).stdout.strip()
            branch = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path).stdout.strip()
            author = self._run_git(["log", "-1", "--pretty=format:%an"], cwd=repo_path).stdout.strip()
            return {
                "commit_sha": commit_sha,
                "branch": branch,
                "author": author
            }
        except Exception:
            return {}

    def get_health(self, project_id: str) -> Dict[str, Any]:
        """Assess overall repository health."""
        repo_path = self.get_repo_path(project_id)
        health = {
            "repository_exists": os.path.exists(repo_path),
            "yaml_valid": False,
            "venv_exists": os.path.exists(self.get_venv_path(project_id)),
            "dependencies_installed": False
        }

        if health["repository_exists"]:
            config = self.validate_yaml(project_id)
            if config:
                health["yaml_valid"] = True

            # If venv exists, try running `pip freeze` to see if pytest is installed
            if health["venv_exists"]:
                python_exe = self.get_python_executable(project_id)
                res = subprocess.run([python_exe, "-m", "pip", "show", "pytest"], cwd=repo_path, capture_output=True, text=True)
                if res.returncode == 0:
                    health["dependencies_installed"] = True

        return health

    def get_git_diff(self, project_id: str, base_ref: str = "HEAD~1") -> Optional[str]:
        """Get the git diff against the previous commit."""
        repo_path = self.get_repo_path(project_id)
        if not os.path.exists(repo_path):
            return None
        try:
            result = self._run_git(["diff", base_ref, "HEAD"], cwd=repo_path)
            return result.stdout.strip() or None
        except Exception:
            return None

repository_manager = RepositoryManager()


class ProjectRepository:
    """Thin per-project wrapper around RepositoryManager for convenience."""

    def __init__(self, project_id: str, git_url: str, branch: str = "main"):
        self.project_id = project_id
        self.git_url = git_url
        self.branch = branch
        self._manager = repository_manager

    def clone_or_pull(self) -> bool:
        return self._manager.clone_or_pull(self.project_id, self.git_url, self.branch)

    def clone(self) -> None:
        self._manager.clone(self.project_id, self.git_url, self.branch)

    def re_clone(self) -> None:
        self._manager.re_clone(self.project_id, self.git_url, self.branch)

    def pull(self) -> None:
        self._manager.pull(self.project_id, self.branch)

    def checkout_branch(self, branch: Optional[str] = None) -> None:
        self._manager.checkout_branch(self.project_id, branch or self.branch)

    def is_cloned(self) -> bool:
        return self._manager.is_cloned(self.project_id)

    def delete_local_repo(self) -> bool:
        return self._manager.delete_local_repo(self.project_id)

    def get_repo_path(self) -> str:
        return self._manager.get_repo_path(self.project_id)

    def get_current_branch(self) -> Optional[str]:
        return self._manager.get_current_branch(self.project_id)

    def get_git_diff(self) -> Optional[str]:
        return self._manager.get_git_diff(self.project_id)

    def get_health(self) -> Dict[str, Any]:
        return self._manager.get_health(self.project_id)

    def get_python_executable(self) -> str:
        return self._manager.get_python_executable(self.project_id)

    def ensure_venv_exists(self) -> bool:
        return self._manager.ensure_venv_exists(self.project_id)

    def install_dependencies(self, requirements_file: str = "requirements.txt") -> bool:
        return self._manager.install_dependencies(self.project_id, requirements_file)

    def validate_yaml(self) -> Optional[AutomationYamlConfig]:
        return self._manager.validate_yaml(self.project_id)

    def get_git_metadata(self) -> Dict[str, Any]:
        return self._manager.get_git_metadata(self.project_id)
