"""Evidence Collection Module"""

import subprocess
import os
import json
from datetime import datetime
from typing import Any, Optional, Dict, List
from dataclasses import dataclass
try:
    import git
except ImportError:
    git = None

@dataclass
class EvidenceCollectorConfig:
    repo_path: str = "."
    appium_log_path: str = "/tmp/appium.log"
    screenshot_dir: str = "/tmp/screenshots"
    device_log_cmd: Optional[str] = None
    max_log_lines: int = 1000


class EvidenceCollector:
    """Collect evidence from test failures"""

    def __init__(self, config: EvidenceCollectorConfig):
        self.config = config

    def collect_git_info(self) -> Dict[str, Any]:
        """Get git metadata using GitPython"""
        info = {
            "git_commit": None,
            "git_author": None,
            "git_branch": None,
            "git_diff": "No changes detected",
            "changed_files": []
        }
        
        if not git:
            return info
            
        try:
            repo = git.Repo(self.config.repo_path, search_parent_directories=True)
            if not repo.bare:
                commit = repo.head.commit
                info["git_commit"] = commit.hexsha
                info["git_author"] = f"{commit.author.name} <{commit.author.email}>"
                try:
                    info["git_branch"] = repo.active_branch.name
                except TypeError:
                    info["git_branch"] = "detached"
                
                # Get diff against previous commit or just local uncommitted changes
                diff_str = repo.git.diff('HEAD~1', 'HEAD')
                info["git_diff"] = diff_str if diff_str else "No changes detected"
                info["changed_files"] = repo.git.diff('HEAD~1', 'HEAD', name_only=True).splitlines()
        except Exception as e:
            info["git_diff"] = f"Unable to retrieve git info: {str(e)}"
            
        return info

    def collect_appium_logs(self, since: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Collect and parse Appium logs"""
        logs = []
        try:
            with open(self.config.appium_log_path, "r") as f:
                lines = f.readlines()[-self.config.max_log_lines :]

            for line in lines:
                parsed = self._parse_log_line(line)
                if parsed:
                    logs.append(parsed)
        except FileNotFoundError:
            logs = [{"timestamp": datetime.utcnow().isoformat(), "level": "ERROR", "message": "Appium log not found"}]

        return logs

    def _parse_log_line(self, line: str) -> Optional[Dict[str, Any]]:
        """Parse a log line into structured format"""
        import re

        timestamp_match = re.search(r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}", line)
        level_match = re.search(r"\b(ERROR|WARN|INFO|DEBUG)\b", line)

        return {
            "timestamp": timestamp_match.group(0) if timestamp_match else None,
            "level": level_match.group(1) if level_match else "UNKNOWN",
            "message": line.strip(),
        }

    def collect_device_logs(self, device_udid: Optional[str] = None) -> str:
        """Collect device logs (syslog, crash logs)"""
        if self.config.device_log_cmd:
            try:
                result = subprocess.run(
                    self.config.device_log_cmd.split(),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                return result.stdout
            except (subprocess.TimeoutExpired, subprocess.SubprocessError):
                return "Unable to retrieve device logs"
        return "Device log collection not configured"

    def collect_screenshot(self, test_name: str) -> Optional[str]:
        """Get screenshot path for test"""
        screenshot_path = os.path.join(self.config.screenshot_dir, f"{test_name}.png")
        if os.path.exists(screenshot_path):
            return screenshot_path
        return None

    def summarize_evidence(self, logs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Extract error-relevant log entries"""
        relevant = []
        error_keywords = ["error", "exception", "failed", "timeout", "unable", "not found"]

        for log in logs:
            msg_lower = log.get("message", "").lower()
            if any(kw in msg_lower for kw in error_keywords):
                relevant.append(log)

        return relevant[-50:] if len(relevant) > 50 else relevant

    def create_evidence_bundle(
        self,
        run_id: str,
        error_message: str,
        test_name: str,
        driver_session=None,
        failed_locator: str = None
    ) -> Dict[str, Any]:
        """Create complete evidence bundle for LLM analysis, including live driver data if available"""
        git_info = self.collect_git_info()
        appium_logs = self.collect_appium_logs()
        summarized_logs = self.summarize_evidence(appium_logs)
        device_logs = self.collect_device_logs()
        screenshot = self.collect_screenshot(test_name)
        
        xml_page_source = None
        network_logs = None
        
        if driver_session:
            try:
                xml_page_source = driver_session.page_source
                # Optionally capture network/performance logs if supported
                if driver_session.capabilities.get('platformName', '').lower() == 'chrome':
                     network_logs = json.dumps(driver_session.get_log('performance'))
            except Exception:
                xml_page_source = "Failed to capture page source"

        return {
            "run_id": run_id,
            "test_name": test_name,
            "error_message": error_message,
            "git_commit": git_info["git_commit"],
            "git_author": git_info["git_author"],
            "git_branch": git_info["git_branch"],
            "git_diff": git_info["git_diff"],
            "changed_files": git_info["changed_files"],
            "appium_logs": summarized_logs,
            "device_logs": device_logs,
            "screenshot_summary": f"Screenshot available at {screenshot}" if screenshot else "No screenshot available",
            "screenshot_path": screenshot,
            "xml_page_source": xml_page_source,
            "network_logs": network_logs,
            "failed_locator": failed_locator
        }