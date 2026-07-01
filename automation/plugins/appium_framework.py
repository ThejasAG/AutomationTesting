import subprocess
import os
import time
import shutil
from typing import Dict, Any

from automation.plugins.base import TestFramework
from automation.projects.repository import repository_manager
from automation.appium_service.manager import appium_manager

class AppiumFramework(TestFramework):
    
    def prepare(self, project_id: str, device_id: str) -> bool:
        """Ensures python venv is ready and dependencies are installed."""
        if not repository_manager.ensure_venv_exists(project_id):
            return False
            
        config = repository_manager.validate_yaml(project_id)
        req_file = config.requirements.file if config else "requirements.txt"
        
        if not repository_manager.install_dependencies(project_id, req_file):
            return False
            
        # Also ensure appium-fake-driver is installed for verification if needed
        # Or let the project requirements handle it.
        return True
        
    def execute(self, project_id: str, device_id: str, config: Dict[str, Any], run_id: str) -> Dict[str, Any]:
        """Runs the pytest command connected to the real Appium server."""
        repo_path = repository_manager.get_repo_path(project_id)
        python_exe = repository_manager.get_python_executable(project_id)
        
        # 1. Allocate Appium Session
        session = appium_manager.allocate_instance(run_id, device_id)
        
        cmd_str = config.get("command", "pytest tests/")
        if cmd_str.startswith("pytest"):
            cmd = [python_exe, "-m", "pytest"] + cmd_str.split()[1:]
        else:
            cmd = cmd_str.split()
            
        # 2. Inject Environment Variables
        env = os.environ.copy()
        env["APPIUM_SERVER_URL"] = f"http://127.0.0.1:{session.port}"
        env["DEVICE_ID"] = device_id
        env["RUN_ID"] = run_id
        
        # Ensure a directory exists for evidence
        evidence_dir = os.path.join(repo_path, "reports", run_id)
        os.makedirs(evidence_dir, exist_ok=True)
        env["EVIDENCE_DIR"] = evidence_dir
        
        start_time = time.time()
        
        process = subprocess.Popen(
            cmd,
            cwd=repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env
        )
        
        logs = []
        for line in iter(process.stdout.readline, ''):
            logs.append(line.strip())
            
        process.stdout.close()
        process.wait()
        
        end_time = time.time()
        
        # Save logs to evidence dir
        with open(os.path.join(evidence_dir, "execution.log"), "w") as f:
            f.write("\n".join(logs))
        
        # 3. Release Instance
        appium_manager.release_instance(session.session_id)
        
        return {
            "status": "passed" if process.returncode == 0 else "failed",
            "logs": logs,
            "exit_code": process.returncode,
            "duration_ms": int((end_time - start_time) * 1000),
            "evidence_dir": evidence_dir
        }
        
    def collect_evidence(self, project_id: str, execution_result: Dict[str, Any]) -> Dict[str, Any]:
        """Bundles evidence into a zip."""
        evidence_dir = execution_result.get("evidence_dir")
        if not evidence_dir or not os.path.exists(evidence_dir):
            return {"error": "No evidence directory found"}
            
        # Create a zip of the evidence directory
        archive_name = shutil.make_archive(evidence_dir, 'zip', evidence_dir)
        
        return {
            "appium_logs": [os.path.join(evidence_dir, "execution.log")],
            "evidence_zip": archive_name
        }
        
    def cleanup(self, project_id: str) -> None:
        """Clean up."""
        pass
