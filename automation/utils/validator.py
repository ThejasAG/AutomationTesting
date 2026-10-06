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

    # ── Project-type specific checks ─────────────────────────────────────────

    def check_npm_available(self) -> Optional[ValidationIssue]:
        if not shutil.which("npm"):
            return ValidationIssue(
                check="npm Available",
                problem="npm command not found in PATH.",
                cause="Node.js/npm is not installed.",
                impact="Cannot install JavaScript dependencies or run the test script.",
                resolution="Install Node.js 18+ from https://nodejs.org",
            )
        return None

    def check_package_json(self) -> Optional[ValidationIssue]:
        if not os.path.exists(os.path.join(self.repo_path, "package.json")):
            return ValidationIssue(
                check="package.json Exists",
                problem="package.json not found in repository root.",
                cause="This project was detected as React Native but has no package.json.",
                impact="Cannot resolve or install JavaScript dependencies.",
                resolution="Ensure package.json is committed at the repository root.",
            )
        return None

    def check_node_modules(self) -> Optional[ValidationIssue]:
        if not os.path.isdir(os.path.join(self.repo_path, "node_modules")):
            return ValidationIssue(
                check="node_modules Installed",
                problem="node_modules directory not found.",
                cause="npm install has not been run for this project.",
                impact="Test execution will fail on missing packages.",
                resolution="The platform installs these automatically. Or run: npm install",
                is_fatal=False,  # preparation installs these right after validation
            )
        return None

    def check_gradle_available(self) -> Optional[ValidationIssue]:
        has_wrapper = os.path.exists(os.path.join(self.repo_path, "gradlew"))
        if has_wrapper or shutil.which("gradle"):
            return None
        return ValidationIssue(
            check="Gradle Available",
            problem="Neither ./gradlew nor a global gradle was found.",
            cause="Gradle is not installed and the project has no wrapper.",
            impact="Cannot build or run the Android test suite.",
            resolution="Commit the Gradle wrapper, or install Gradle from https://gradle.org",
        )

    def check_android_sdk(self) -> Optional[ValidationIssue]:
        if os.getenv("ANDROID_HOME") or os.getenv("ANDROID_SDK_ROOT") or shutil.which("adb"):
            return None
        return ValidationIssue(
            check="Android SDK",
            problem="ANDROID_HOME / ANDROID_SDK_ROOT is not set and adb is not in PATH.",
            cause="The Android SDK is not installed or not exported.",
            impact="Cannot build the app or talk to Android devices.",
            resolution="Install the Android SDK and export ANDROID_HOME.",
        )

    def check_xcode_available(self) -> Optional[ValidationIssue]:
        if not shutil.which("xcodebuild"):
            return ValidationIssue(
                check="Xcode Available",
                problem="xcodebuild not found in PATH.",
                cause="Xcode or the command line tools are not installed.",
                impact="Cannot build or test an iOS application.",
                resolution="Install Xcode, then run: xcode-select --install",
            )
        return None

    def check_cocoapods(self) -> Optional[ValidationIssue]:
        # Only relevant when the project actually declares a Podfile.
        if not os.path.exists(os.path.join(self.repo_path, "Podfile")):
            return None
        if not shutil.which("pod"):
            return ValidationIssue(
                check="CocoaPods Available",
                problem="The project has a Podfile but 'pod' is not in PATH.",
                cause="CocoaPods is not installed.",
                impact="iOS dependencies cannot be resolved.",
                resolution="Install CocoaPods: sudo gem install cocoapods",
            )
        return None

    def check_webdriveragent(self) -> Optional[ValidationIssue]:
        """WebDriverAgent ships with the Appium XCUITest driver."""
        out = _run(["npx", "appium", "driver", "list", "--installed"], timeout=20)
        if out and "xcuitest" in out.lower():
            return None
        return ValidationIssue(
            check="WebDriverAgent (XCUITest driver)",
            problem="The Appium XCUITest driver does not appear to be installed.",
            cause="WebDriverAgent is provided by the xcuitest driver, which is missing.",
            impact="Appium cannot drive an iOS simulator or device.",
            resolution="Install it: appium driver install xcuitest",
            is_fatal=False,
        )

    def check_maven_available(self) -> Optional[ValidationIssue]:
        if not shutil.which("mvn"):
            return ValidationIssue(
                check="Maven Available",
                problem="mvn command not found in PATH.",
                cause="Apache Maven is not installed.",
                impact="Cannot resolve dependencies or run the Java test suite.",
                resolution="Install Maven from https://maven.apache.org",
            )
        return None

    def check_flutter_available(self) -> Optional[ValidationIssue]:
        if not shutil.which("flutter"):
            return ValidationIssue(
                check="Flutter SDK",
                problem="flutter command not found in PATH.",
                cause="The Flutter SDK is not installed.",
                impact="Cannot resolve packages or run Flutter integration tests.",
                resolution="Install Flutter from https://docs.flutter.dev/get-started/install",
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

    def _type_specific_checks(self, project_type: str) -> List[Optional[ValidationIssue]]:
        """Toolchain checks that apply only to a given project type.

        A React Native / Flutter / native mobile project must NEVER be forced to
        have a Python .venv — that mismatch was the source of the spurious
        ".venv not found" failures.
        """
        from automation.projects.detector import ProjectType

        if project_type == ProjectType.PYTHON:
            return [
                self.check_venv_healthy(),
                self.check_dependencies_installed(),
                self.check_python_version_compat(),
            ]

        if project_type == ProjectType.REACT_NATIVE:
            return [
                self.check_package_json(),
                self.check_npm_available(),
                self.check_node_modules(),
                self.check_node_version_compat(),
            ]

        if project_type == ProjectType.FLUTTER:
            return [self.check_flutter_available()]

        if project_type == ProjectType.ANDROID:
            return [self.check_gradle_available(), self.check_android_sdk()]

        if project_type == ProjectType.IOS:
            return [
                self.check_xcode_available(),
                self.check_cocoapods(),
                self.check_webdriveragent(),
            ]

        if project_type == ProjectType.JAVA:
            return [self.check_maven_available()]

        # Unknown type — we cannot assert a toolchain; warn rather than block.
        return [
            ValidationIssue(
                check="Project Type Detected",
                problem="Could not determine the project type from the repository.",
                cause="No package.json, pubspec.yaml, build.gradle, *.xcodeproj, pom.xml or requirements.txt was found.",
                impact="Type-specific dependency checks were skipped.",
                resolution="Add the appropriate manifest file to the repository root.",
                is_fatal=False,
            )
        ]

    def validate_pre_execution(
        self,
        device_id: Optional[str] = None,
        platform: Optional[str] = None,
        project_type: Optional[str] = None,
    ) -> ValidationResult:
        """
        Run all pre-execution checks. Returns a ValidationResult.
        Fatal issues block execution; warnings allow continuation with a logged notice.

        *platform* ("ios"/"android") selects the device-tooling checks. When not
        supplied it is inferred from the device-id format (iOS simulator UDID vs
        ADB serial), defaulting to "android".

        *project_type* selects the toolchain checks. When not supplied it is
        auto-detected from the cloned repository, so Python-only checks are never
        applied to a React Native or native mobile project.
        """
        if platform is None:
            platform = "ios" if _looks_like_ios_udid(device_id) else "android"

        # Repository must exist before anything else can be meaningfully checked.
        repo_issue = self.check_repository_exists()
        if repo_issue:
            logger.error(
                f"[PRE-FLIGHT FAIL] {repo_issue.check}: {repo_issue.problem} — Fix: {repo_issue.resolution}"
            )
            return ValidationResult(passed=False, issues=[repo_issue], warnings=[])

        if project_type is None:
            from automation.projects.detector import detect_project_type

            project_type = detect_project_type(self.repo_path).project_type

        fatal_issues: List[ValidationIssue] = []
        warnings: List[ValidationIssue] = []

        # Checks that apply to every project, regardless of type.
        checks: List[Optional[ValidationIssue]] = [
            self.check_automation_yaml(),
            self.check_git_available(),
            self.check_adb_available(platform),
            self.check_appium_available(platform),
        ]

        # Checks that depend on what kind of project this is.
        checks.extend(self._type_specific_checks(project_type))

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
