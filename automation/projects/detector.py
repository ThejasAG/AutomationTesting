"""Project type detection and automation.yaml template generation.

Single source of truth for "what kind of project is this?". Both the API
(validate / status endpoints) and the execution pipeline (agent, framework
plugins) use this so detection logic is never duplicated.
"""

import os
import glob
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class ProjectType:
    """Known project types. Values are stored verbatim in TestProject.project_type."""

    REACT_NATIVE = "react_native"
    FLUTTER = "flutter"
    ANDROID = "android"
    IOS = "ios"
    PYTHON = "python"
    JAVA = "java"
    UNKNOWN = "unknown"


# Human-readable labels for the dashboard.
PROJECT_TYPE_LABELS: Dict[str, str] = {
    ProjectType.REACT_NATIVE: "React Native",
    ProjectType.FLUTTER: "Flutter",
    ProjectType.ANDROID: "Android",
    ProjectType.IOS: "iOS",
    ProjectType.PYTHON: "Python",
    ProjectType.JAVA: "Java",
    ProjectType.UNKNOWN: "Unknown",
}


@dataclass
class DetectionResult:
    project_type: str
    label: str
    markers: List[str]  # the files that produced this verdict

    def to_dict(self) -> dict:
        return {
            "project_type": self.project_type,
            "label": self.label,
            "markers": self.markers,
        }


def _exists(repo_path: str, *names: str) -> Optional[str]:
    for name in names:
        if os.path.exists(os.path.join(repo_path, name)):
            return name
    return None


def _glob_one(repo_path: str, pattern: str) -> Optional[str]:
    matches = glob.glob(os.path.join(repo_path, pattern))
    return os.path.basename(matches[0]) if matches else None


def detect_project_type(repo_path: str) -> DetectionResult:
    """Inspect a cloned repository and classify it.

    Order matters: a React Native repo also contains android/ and ios/ folders,
    and a Flutter repo contains both plus a pubspec.yaml — so the more specific
    cross-platform frameworks are checked before the native ones.
    """
    if not repo_path or not os.path.isdir(repo_path):
        return DetectionResult(ProjectType.UNKNOWN, PROJECT_TYPE_LABELS[ProjectType.UNKNOWN], [])

    markers: List[str] = []

    # 1. Flutter — pubspec.yaml is unambiguous.
    if (m := _exists(repo_path, "pubspec.yaml")):
        markers.append(m)
        return DetectionResult(ProjectType.FLUTTER, PROJECT_TYPE_LABELS[ProjectType.FLUTTER], markers)

    # 2. React Native — package.json (checked before native android/ios dirs).
    if (m := _exists(repo_path, "package.json")):
        markers.append(m)
        return DetectionResult(
            ProjectType.REACT_NATIVE, PROJECT_TYPE_LABELS[ProjectType.REACT_NATIVE], markers
        )

    # 3. iOS — .xcodeproj / .xcworkspace at the root.
    if (m := _glob_one(repo_path, "*.xcworkspace")) or (m := _glob_one(repo_path, "*.xcodeproj")):
        markers.append(m)
        return DetectionResult(ProjectType.IOS, PROJECT_TYPE_LABELS[ProjectType.IOS], markers)

    # 4. Android — build.gradle / build.gradle.kts / settings.gradle.
    if (m := _exists(repo_path, "build.gradle", "build.gradle.kts", "settings.gradle")):
        markers.append(m)
        return DetectionResult(ProjectType.ANDROID, PROJECT_TYPE_LABELS[ProjectType.ANDROID], markers)

    # 5. Java — Maven.
    if (m := _exists(repo_path, "pom.xml")):
        markers.append(m)
        return DetectionResult(ProjectType.JAVA, PROJECT_TYPE_LABELS[ProjectType.JAVA], markers)

    # 6. Python — requirements.txt / pyproject.toml / setup.py.
    if (m := _exists(repo_path, "requirements.txt", "pyproject.toml", "setup.py")):
        markers.append(m)
        return DetectionResult(ProjectType.PYTHON, PROJECT_TYPE_LABELS[ProjectType.PYTHON], markers)

    return DetectionResult(ProjectType.UNKNOWN, PROJECT_TYPE_LABELS[ProjectType.UNKNOWN], [])


# ── automation.yaml template generation ──────────────────────────────────────

# Per-type execution command and framework defaults used when scaffolding a
# missing automation.yaml.
_TEMPLATE_DEFAULTS: Dict[str, Dict[str, str]] = {
    ProjectType.PYTHON: {
        "framework": "appium",
        "language": "python",
        "command": "pytest tests/",
    },
    ProjectType.REACT_NATIVE: {
        "framework": "appium",
        "language": "python",
        # NOT `npm test` — that runs jest unit tests, which never touch a
        # simulator and never install the app. Device automation needs an Appium
        # suite; the platform builds and installs the app, this drives it.
        "command": "pytest tests/",
    },
    ProjectType.FLUTTER: {
        "framework": "appium",
        "language": "dart",
        "command": "flutter test integration_test",
    },
    ProjectType.ANDROID: {
        "framework": "appium",
        "language": "java",
        "command": "./gradlew connectedAndroidTest",
    },
    ProjectType.IOS: {
        "framework": "appium",
        "language": "swift",
        "command": "xcodebuild test -scheme App -destination 'platform=iOS Simulator,name=iPhone 16 Pro'",
    },
    ProjectType.JAVA: {
        "framework": "appium",
        "language": "java",
        "command": "mvn test",
    },
    ProjectType.UNKNOWN: {
        "framework": "appium",
        "language": "python",
        "command": "pytest tests/",
    },
}


def generate_automation_yaml(
    project_name: str,
    project_type: str,
    platform: str = "android",
    branch: str = "main",
) -> str:
    """Render an automation.yaml template appropriate for *project_type*.

    The output validates against AutomationYamlConfig (see projects/config.py).
    """
    defaults = _TEMPLATE_DEFAULTS.get(project_type, _TEMPLATE_DEFAULTS[ProjectType.UNKNOWN])

    return f"""# Generated by the automation platform for a {PROJECT_TYPE_LABELS.get(project_type, 'Unknown')} project.
# Review the execution command and platform before running.
project:
  name: {project_name}

repository:
  branch: {branch}

framework:
  type: {defaults['framework']}
  language: {defaults['language']}

environment:
  platform: {platform}
  app: null

execution:
  command: {defaults['command']}
  parallel: false
  retries: 0

python:
  version: "3.12"

requirements:
  file: requirements.txt

reports:
  output: reports/

evidence:
  screenshots: true
  video: true
  page_source: true

ai:
  enabled: true

notifications:
  slack: false
"""


def write_automation_yaml(
    repo_path: str,
    project_name: str,
    project_type: str,
    platform: str = "android",
    branch: str = "main",
) -> str:
    """Write a generated automation.yaml into *repo_path*. Returns its content."""
    content = generate_automation_yaml(project_name, project_type, platform, branch)
    target = os.path.join(repo_path, "automation.yaml")
    with open(target, "w") as f:
        f.write(content)
    logger.info(f"Generated automation.yaml for {project_name} ({project_type}) at {target}")
    return content
