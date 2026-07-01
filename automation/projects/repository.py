import os
import subprocess
import shutil
import yaml
import logging
from typing import Optional, Dict, Any

from automation.projects.config import AutomationYamlConfig

logger = logging.getLogger(__name__)

class RepositoryManager:
    def __init__(self, repos_base_dir: str = "repos"):
        self.base_dir = os.path.abspath(repos_base_dir)
        if not os.path.exists(self.base_dir):
            os.makedirs(self.base_dir)
            
    def get_repo_path(self, project_id: str) -> str:
        return os.path.join(self.base_dir, project_id)
        
    def _run_git(self, args: list, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
        cmd = ["git"] + args
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)

    def clone_or_pull(self, project_id: str, git_url: str, branch: str = "main") -> bool:
        """Clones if missing, pulls if exists."""
        repo_path = self.get_repo_path(project_id)
        
        if not os.path.exists(repo_path):
            logger.info(f"Cloning {git_url} into {repo_path}")
            res = self._run_git(["clone", "-b", branch, git_url, repo_path])
            if res.returncode != 0:
                logger.error(f"Clone failed: {res.stderr}")
                return False
        else:
            logger.info(f"Pulling latest for {project_id}")
            res = self._run_git(["pull", "origin", branch], cwd=repo_path)
            if res.returncode != 0:
                logger.error(f"Pull failed: {res.stderr}")
                return False
                
        return True

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
        """Returns the absolute path to the venv's python executable."""
        venv_path = self.get_venv_path(project_id)
        if os.name == 'nt':
            return os.path.join(venv_path, "Scripts", "python.exe")
        return os.path.join(venv_path, "bin", "python")

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
