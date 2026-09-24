#!/usr/bin/env python3
"""
Enterprise AI Test Orchestration Platform
Installation Wizard v1.0

Run this script before starting the platform to verify all dependencies.
Usage: python install.py
"""

import sys
import shutil
import subprocess
import os
import json
from dataclasses import dataclass, field
from typing import List, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Terminal Colors ─────────────────────────────────────────────────────────
class C:
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"
    WHITE  = "\033[97m"
    BOLD   = "\033[1m"
    RESET  = "\033[0m"

def green(s):  return f"{C.GREEN}{s}{C.RESET}"
def red(s):    return f"{C.RED}{s}{C.RESET}"
def yellow(s): return f"{C.YELLOW}{s}{C.RESET}"
def bold(s):   return f"{C.BOLD}{s}{C.RESET}"
def cyan(s):   return f"{C.CYAN}{s}{C.RESET}"

# ── Check Result ─────────────────────────────────────────────────────────────
@dataclass
class CheckResult:
    name: str
    status: str          # "OK" | "MISSING" | "WARNING"
    version: str = ""
    note: str = ""
    fix: str = ""
    required: bool = True

def run_cmd(cmd: List[str], timeout: int = 8) -> Optional[str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (result.stdout + result.stderr).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

def check_python() -> CheckResult:
    ver = sys.version_info
    version = f"{ver.major}.{ver.minor}.{ver.micro}"
    if ver.major == 3 and ver.minor >= 10:
        return CheckResult("Python 3.10+", "OK", version)
    return CheckResult("Python 3.10+", "MISSING", version,
                       "Python 3.10 or newer is required.",
                       "Download from https://python.org/downloads")

def check_node() -> CheckResult:
    out = run_cmd(["node", "--version"])
    if not out:
        return CheckResult("Node.js 18+", "MISSING", "",
                           "Node.js is required for the dashboard.",
                           "Download from https://nodejs.org")
    ver = out.strip().lstrip("v")
    major = int(ver.split(".")[0])
    if major >= 18:
        return CheckResult("Node.js 18+", "OK", ver)
    return CheckResult("Node.js 18+", "WARNING", ver,
                       f"Node {ver} detected. Node 18+ is recommended.",
                       "Upgrade via https://nodejs.org")

def check_git() -> CheckResult:
    out = run_cmd(["git", "--version"])
    if not out:
        return CheckResult("Git", "MISSING", "",
                           "Git is required for repository cloning.",
                           "Download from https://git-scm.com")
    ver = out.replace("git version", "").strip()
    return CheckResult("Git", "OK", ver)

def check_adb() -> CheckResult:
    out = run_cmd(["adb", "version"])
    if not out:
        return CheckResult("ADB (Android Debug Bridge)", "MISSING", "",
                           "ADB is needed for Android device automation.",
                           "Install via Android SDK Platform Tools: https://developer.android.com/tools/releases/platform-tools")
    line = out.splitlines()[0] if out else ""
    return CheckResult("ADB (Android Debug Bridge)", "OK", line)

def check_java() -> CheckResult:
    out = run_cmd(["java", "-version"])
    if not out:
        return CheckResult("Java JDK", "MISSING", "",
                           "Java is required by Appium.",
                           "Download JDK 11+ from https://adoptium.net")
    line = out.splitlines()[0] if out else ""
    return CheckResult("Java JDK", "OK", line)

def check_appium() -> CheckResult:
    # Try npx appium --version
    out = run_cmd(["npx", "appium", "--version"], timeout=15)
    if not out:
        # Try direct
        out = run_cmd(["appium", "--version"], timeout=15)
    if not out:
        return CheckResult("Appium", "MISSING", "",
                           "Appium is required to drive mobile devices.",
                           "Install via: npm install -g appium && appium driver install uiautomator2")
    ver = out.strip().splitlines()[0]
    return CheckResult("Appium", "OK", ver)

def check_python_packages() -> CheckResult:
    try:
        import fastapi, sqlalchemy, uvicorn, pydantic, requests, yaml
        return CheckResult("Python packages", "OK", "fastapi, sqlalchemy, uvicorn, pydantic, requests, pyyaml")
    except ImportError as e:
        return CheckResult("Python packages", "MISSING", "",
                           f"Missing package: {e}",
                           "Run: pip install -r requirements.txt")

def check_node_packages() -> CheckResult:
    dashboard_nm = os.path.join("automation", "dashboard", "node_modules")
    if os.path.isdir(dashboard_nm):
        return CheckResult("Dashboard npm packages", "OK", "node_modules present")
    return CheckResult("Dashboard npm packages", "MISSING", "",
                       "Dashboard dependencies not installed.",
                       "Run: cd automation/dashboard && npm install")

def check_env_config() -> CheckResult:
    env_file = ".env"
    if not os.path.exists(env_file):
        return CheckResult("Environment Config (.env)", "WARNING", "",
                           ".env file not found. The platform will use defaults.",
                           "Copy .env.example to .env and configure your secrets.")
    return CheckResult("Environment Config (.env)", "OK", ".env present")

def check_db_file() -> CheckResult:
    # Report the CONFIGURED database, not a hardcoded filename. A fixed name here
    # described a file the platform may not even use — and on a machine configured
    # via .env it reported "OK" for a stale file while the real database was
    # somewhere else entirely.
    try:
        from automation.config import database_url
        url = database_url()
        db_path = url.removeprefix("sqlite:///") if url.startswith("sqlite:") else url
    except Exception:
        db_path = "test_automation_new.db"
    if not db_path.startswith("/") and "://" in str(db_path):
        return CheckResult("Database", "OK", db_path)   # networked DB: nothing to stat
    if os.path.exists(db_path):
        size = os.path.getsize(db_path)
        return CheckResult("Database (SQLite)", "OK", f"{db_path} ({size} bytes)")
    return CheckResult("Database (SQLite)", "WARNING", "",
                       "Database file not found. It will be created on first startup.",
                       "Run: python -m automation.database.config to initialize")

def check_reports_dir() -> CheckResult:
    if os.path.isdir("reports"):
        return CheckResult("Reports Directory", "OK", "reports/ exists")
    return CheckResult("Reports Directory", "WARNING", "",
                       "reports/ directory not found.",
                       "Create it: mkdir reports")

def check_repos_dir() -> CheckResult:
    if os.path.isdir("repos"):
        return CheckResult("Repos Directory", "OK", "repos/ exists")
    return CheckResult("Repos Directory", "WARNING", "",
                       "repos/ directory not found.",
                       "Create it: mkdir repos")

def check_adb_devices() -> CheckResult:
    out = run_cmd(["adb", "devices"])
    if not out:
        return CheckResult("ADB Connected Devices", "WARNING", "",
                           "Could not run 'adb devices'.",
                           "Ensure ADB is installed and PATH is set correctly.")
    lines = [l.strip() for l in out.splitlines() if l.strip() and "List of devices" not in l and "daemon" not in l]
    devices = [l for l in lines if "\t" in l]
    count = len(devices)
    if count == 0:
        return CheckResult("ADB Connected Devices", "WARNING", "0 devices",
                           "No Android devices/emulators detected.",
                           "Connect a device via USB with USB debugging enabled, or start an emulator.",
                           required=False)
    return CheckResult("ADB Connected Devices", "OK", f"{count} device(s): {', '.join(d.split()[0] for d in devices)}")

# ── Main Wizard ───────────────────────────────────────────────────────────────
def run_wizard():
    print()
    print(bold(cyan("=" * 64)))
    print(bold(cyan("   Enterprise AI Test Orchestration Platform")))
    print(bold(cyan("   Installation Wizard v1.0")))
    print(bold(cyan("=" * 64)))
    print()

    checks: List[CheckResult] = [
        check_python(),
        check_node(),
        check_git(),
        check_java(),
        check_adb(),
        check_appium(),
        check_python_packages(),
        check_node_packages(),
        check_env_config(),
        check_db_file(),
        check_reports_dir(),
        check_repos_dir(),
        check_adb_devices(),
    ]

    ok_count = 0
    warning_count = 0
    missing_count = 0

    print(f"  {'CHECK':<35} {'STATUS':<12} DETAILS")
    print("  " + "─" * 70)

    for c in checks:
        if c.status == "OK":
            status_str = green("  ✓ OK     ")
            ok_count += 1
        elif c.status == "WARNING":
            status_str = yellow("  ⚠ WARNING")
            warning_count += 1
        else:
            status_str = red("  ✗ MISSING")
            missing_count += 1

        version_info = f" ({c.version})" if c.version else ""
        print(f"  {c.name:<35} {status_str}  {c.note or version_info}")

    print()
    print("  " + "─" * 70)

    # Readiness Score
    total = len(checks)
    score = int((ok_count / total) * 100)

    if score == 100:
        score_color = green
        verdict = "FULLY READY ✓"
    elif score >= 80:
        score_color = yellow
        verdict = "MOSTLY READY ⚠ (some items need attention)"
    else:
        score_color = red
        verdict = "NOT READY ✗ (please resolve missing dependencies)"

    print()
    print(f"  {bold('Platform Readiness Score:')} {score_color(bold(f'{score}%'))}  —  {score_color(verdict)}")
    print()

    # Resolution Guide
    issues = [c for c in checks if c.status != "OK"]
    if issues:
        print(bold("  Resolution Guide:"))
        print()
        for c in issues:
            label = "⚠" if c.status == "WARNING" else "✗"
            color = yellow if c.status == "WARNING" else red
            print(f"  {color(label + ' ' + c.name)}")
            if c.note:
                print(f"    Problem : {c.note}")
            if c.fix:
                print(f"    Fix     : {cyan(c.fix)}")
            print()

    print(bold(cyan("=" * 64)))
    print()

    # Auto-create missing directories
    for d in ["reports", "repos", "logs"]:
        if not os.path.isdir(d):
            os.makedirs(d)
            print(green(f"  ✓ Created missing directory: {d}/"))

    if missing_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_wizard()
