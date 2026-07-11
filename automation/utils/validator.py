"""
Pre-Execution Environment Validator

Validates the full environment before any automation job is allowed to run.
Called by the Execution Agent before starting a pytest session.
"""

import os
import subprocess
import shutil
import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ValidationIssue:
    check: str
    problem: str
    cause: str
    impact: str
    resolution: str
    doc_link: str = "https://github.com/your-org/automation-platform/docs"
    is_fatal: bool = True


@dataclass
class ValidationResult:
    passed: bool
    issues: List[ValidationIssue] = field(default_factory=list)
    warnings: List[ValidationIssue] = field(default_factory=list)

    def to_dict(self):
        return {
            "passed": self.passed,
            "issues": [
                {
                    "check": i.check,
                    "problem": i.problem,
                    "cause": i.cause,
                    "impact": i.impact,
                    "resolution": i.resolution,
                    "doc_link": i.doc_link,
                    "fatal": i.is_fatal
                }
                for i in self.issues
            ],
            "warnings": [
                {
                    "check": w.check,
                    "problem": w.problem,
                    "resolution": w.resolution
                }
                for w in self.warnings
            ]
        }


def _run(cmd: List[str], cwd: Optional[str] = None, timeout: int = 10) -> Optional[str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)
        return (result.stdout + result.stderr).strip()
    except Exception:
        return None


def _looks_like_ios_udid(device_id: Optional[str]) -> bool:
    """iOS simulator UDIDs are 36-char hex strings with dashes (8-4-4-4-12)."""
    if not device_id or len(device_id) != 36:
        return False
    parts = device_id.split("-")
    if len(parts) != 5 or [len(p) for p in parts] != [8, 4, 4, 4, 12]:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in device_id.replace("-", ""))


class EnvironmentValidator:
    """
    Validates the complete pre-execution environment for a given project.
    All checks return ValidationResult; fatal failures prevent execution.
    """

    def __init__(self, project_id: str, repo_base: str = "repos"):
        self.project_id = project_id
        self.repo_path = os.path.abspath(os.path.join(repo_base, project_id))
        self.venv_path = os.path.join(self.repo_path, ".venv")
        if os.name == "nt":
            self.python_exe = os.path.join(self.venv_path, "Scripts", "python.exe")
        else:
            self.python_exe = os.path.join(self.venv_path, "bin", "python")

    # ── Individual Checks ────────────────────────────────────────────────────

    def check_repository_exists(self) -> Optional[ValidationIssue]:
        if not os.path.isdir(self.repo_path):
            return ValidationIssue(
                check="Repository Exists",
                problem=f"Repository directory not found: {self.repo_path}",
                cause="The project has not been cloned yet.",
                impact="Cannot execute any tests without the source repository.",
                resolution="Register the project and trigger a sync, or run: git clone <url> repos/<project_id>"
            )
        return None

    def check_automation_yaml(self) -> Optional[ValidationIssue]:
        yaml_path = os.path.join(self.repo_path, "automation.yaml")
        if not os.path.exists(yaml_path):
            return ValidationIssue(
                check="automation.yaml Exists",
                problem="automation.yaml not found in repository root.",
                cause="The repository has not been configured for this platform.",
                impact="Cannot determine execution parameters (framework, command, platform).",
                resolution="Create automation.yaml in the repository root. See docs/sample-automation.yaml."
            )
        return None

    def check_venv_healthy(self) -> Optional[ValidationIssue]:
        if not os.path.isdir(self.venv_path):
            return ValidationIssue(
                check="Virtual Environment",
                problem="Python virtual environment (.venv) not found.",
                cause="Dependencies have not been installed for this project.",
                impact="Cannot run pytest without an isolated Python environment.",
                resolution="The agent will attempt to create it automatically. Or run: python -m venv .venv in the repo."
            )
        if not os.path.exists(self.python_exe):
            return ValidationIssue(
                check="Virtual Environment Python",
                problem=f"Python executable not found at {self.python_exe}",
                cause="The virtual environment may be corrupted.",
                impact="Cannot execute automation scripts.",
                resolution=f"Remove and recreate: rmdir /s {self.venv_path} && python -m venv .venv"
            )
        return None

    def check_dependencies_installed(self) -> Optional[ValidationIssue]:
        if not os.path.exists(self.python_exe):
            return None  # Already caught by venv check
        out = _run([self.python_exe, "-m", "pip", "show", "pytest"], cwd=self.repo_path)
        if not out or "Version:" not in out:
            return ValidationIssue(
                check="Dependencies Installed",
                problem="pytest is not installed in the virtual environment.",
                cause="pip install -r requirements.txt has not been run.",
                impact="Cannot execute test suite.",
                resolution="Run: <python_exe> -m pip install -r requirements.txt"
            )
        return None

    def check_git_available(self) -> Optional[ValidationIssue]:
        if not shutil.which("git"):
            return ValidationIssue(
                check="Git Available",
                problem="git command not found in PATH.",
                cause="Git is not installed or not in PATH.",
                impact="Cannot clone or pull repositories.",
                resolution="Install git from https://git-scm.com and ensure it is in PATH.",
                is_fatal=False
            )
        return None

    def check_adb_available(self, platform: str = "android") -> Optional[ValidationIssue]:
        if platform == "ios":
            # iOS uses xcrun/simctl, not ADB — this check is not applicable.
            return None
        if not shutil.which("adb"):
            return ValidationIssue(
                check="ADB Available",
                problem="adb command not found in PATH.",
                cause="Android SDK Platform Tools not installed or not in PATH.",
                impact="Cannot discover or connect to Android devices.",
                resolution="Download Android SDK Platform Tools from https://developer.android.com/tools/releases/platform-tools",
                is_fatal=False
            )
        return None

    def check_device_connected(self, device_id: str, platform: str = "android") -> Optional[ValidationIssue]:
        if platform == "ios":
            out = _run(["xcrun", "simctl", "list", "devices", "booted"])
            if out is None:
                return ValidationIssue(
                    check="Simulator Booted",
                    problem="Could not query simctl for booted simulators.",
                    cause="xcrun/simctl unavailable — Xcode command line tools not installed.",
                    impact="Cannot run automation on an iOS simulator.",
                    resolution="Install Xcode and command line tools: xcode-select --install",
                    is_fatal=False
                )
            if device_id not in out:
                return ValidationIssue(
                    check=f"Simulator Booted ({device_id})",
                    problem=f"Simulator '{device_id}' is not booted.",
                    cause="The iOS simulator is shut down, or the UDID is incorrect.",
                    impact="Automation cannot be executed without a booted simulator.",
                    resolution=f"Boot it: xcrun simctl boot {device_id} (or launch it from Simulator.app).",
                    is_fatal=False
                )
            return None
        if not shutil.which("adb"):
            return None  # Already caught
        out = _run(["adb", "devices"])
        if not out:
            return ValidationIssue(
                check="Device Connected",
                problem="Could not query ADB for devices.",
                cause="ADB daemon failed to start.",
                impact="Cannot run automation on device.",
                resolution="Run 'adb kill-server && adb start-server' and reconnect your device."
            )
        if device_id not in out:
            return ValidationIssue(
                check=f"Device Connected ({device_id})",
                problem=f"Device '{device_id}' is not visible in ADB.",
                cause="Device is disconnected, unauthorized, or in wrong mode.",
                impact="Automation cannot be executed without an accessible device.",
                resolution="Connect the device, enable USB Debugging, and accept the ADB authorization prompt."
            )
        return None

    def check_appium_available(self, platform: str = "android") -> Optional[ValidationIssue]:
        out = _run(["npx", "appium", "--version"], timeout=15)
        if not out:
            out = _run(["appium", "--version"], timeout=15)
        if not out:
            driver = "xcuitest" if platform == "ios" else "uiautomator2"
            return ValidationIssue(
                check="Appium Available",
                problem="Appium is not installed or not accessible.",
                cause="Appium was not installed globally or via npx.",
                impact="Cannot start Appium server for device automation.",
                resolution=f"Install Appium: npm install -g appium && appium driver install {driver}",
                is_fatal=False
            )
        return None

    def check_python_version_compat(self) -> Optional[ValidationIssue]:
        import sys
        ver = sys.version_info
        if ver.major < 3 or (ver.major == 3 and ver.minor < 10):
            return ValidationIssue(
                check="Python Version Compatible",
                problem=f"Python {ver.major}.{ver.minor} detected.",
                cause="Platform requires Python 3.10 or newer.",
                impact="Some features may not work correctly.",
                resolution="Upgrade Python from https://python.org/downloads",
                is_fatal=False
            )
        return None

    def check_node_version_compat(self) -> Optional[ValidationIssue]:
        out = _run(["node", "--version"])
        if not out:
            return ValidationIssue(
                check="Node.js Version Compatible",
                problem="Node.js not found in PATH.",
                cause="Node.js is not installed.",
                impact="Dashboard will not build; Appium cannot be invoked via npx.",
                resolution="Install Node.js 18+ from https://nodejs.org",
                is_fatal=False
            )
        ver = out.lstrip("v")
        major = int(ver.split(".")[0])
        if major < 18:
            return ValidationIssue(
                check="Node.js Version Compatible",
                problem=f"Node {ver} detected. Node 18+ required.",
                cause="Outdated Node.js version.",
                impact="Appium and dashboard may not function correctly.",
                resolution="Upgrade Node.js from https://nodejs.org",
                is_fatal=False
            )
        return None

    # ── Full Validation Run ──────────────────────────────────────────────────

    def validate_pre_execution(
        self, device_id: Optional[str] = None, platform: Optional[str] = None
    ) -> ValidationResult:
        """
        Run all pre-execution checks. Returns a ValidationResult.
        Fatal issues block execution; warnings allow continuation with a logged notice.

        *platform* ("ios"/"android") selects the device-tooling checks. When not
        supplied it is inferred from the device-id format (iOS simulator UDID vs
        ADB serial), defaulting to "android".
        """
        if platform is None:
            platform = "ios" if _looks_like_ios_udid(device_id) else "android"

        fatal_issues: List[ValidationIssue] = []
        warnings: List[ValidationIssue] = []

        checks = [
            self.check_repository_exists(),
            self.check_automation_yaml(),
            self.check_venv_healthy(),
            self.check_dependencies_installed(),
            self.check_git_available(),
            self.check_adb_available(platform),
            self.check_appium_available(platform),
            self.check_python_version_compat(),
            self.check_node_version_compat(),
        ]

        if device_id:
            checks.append(self.check_device_connected(device_id, platform))

        for issue in checks:
            if issue is None:
                continue
            if issue.is_fatal:
                fatal_issues.append(issue)
                logger.error(
                    f"[PRE-FLIGHT FAIL] {issue.check}: {issue.problem} — Fix: {issue.resolution}"
                )
            else:
                warnings.append(issue)
                logger.warning(
                    f"[PRE-FLIGHT WARN] {issue.check}: {issue.problem} — Fix: {issue.resolution}"
                )

        passed = len(fatal_issues) == 0
        return ValidationResult(passed=passed, issues=fatal_issues, warnings=warnings)
