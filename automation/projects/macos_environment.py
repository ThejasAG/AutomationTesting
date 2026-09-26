"""What this Mac has, what the project needs, and where the two disagree.

WHY THIS EXISTS
    A build failed on one Mac and succeeded on another with the same repository.
    The build log could not say why, because it reports the LAST thing that broke,
    and several unrelated problems mask each other: a missing pod manifest, an
    unapplied patch and a genuinely incompatible dependency all end as
    "xcodebuild failed". Fixing whichever error surfaced just promotes the next.

    So this reads the machine and the project, compares them, and says which of the
    two a given failure belongs to — BEFORE a build is attempted.

WHAT IT DELIBERATELY DOES NOT DO
    It does not fix anything. Detection and repair are separated on purpose: a
    check that also mutates cannot be run to answer "what is different about this
    Mac?", which is the question that started this. Callers decide what to act on,
    and `Report.auto_fixable` says what is safe to act on.

    It also never rewrites tracked application files. The deployment target on this
    project is 9.0/10.0/11.0 in a TRACKED pbxproj; raising it there would put the
    platform's change into someone's application diff. It is reported, not edited.

PATHS ARE NEVER ASSUMED
    `pod` is /usr/local/bin/pod on Intel and /opt/homebrew/bin/pod on Apple
    Silicon, and neither on a machine using rbenv or a project Gemfile. Every tool
    is resolved with shutil.which against the build environment's PATH.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import logging

logger = logging.getLogger(__name__)

# Status values, ordered by severity so a report can sort on them.
PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"
_SEVERITY = {FAIL: 0, WARN: 1, INFO: 2, PASS: 3}

# Categories, so a failure names the layer that owns it rather than landing in a
# generic "build failed". These are the buckets the brief asked for.
ENVIRONMENT = "ENVIRONMENT"
DEPENDENCY_INSTALL = "DEPENDENCY INSTALLATION"
PATCH = "PATCH"
COCOAPODS = "COCOAPODS"
XCODE = "XCODE"
APPLICATION_SOURCE = "APPLICATION SOURCE"
APPLICATION_DEPENDENCY = "APPLICATION DEPENDENCY"
DEVICE = "DEVICE"
PLATFORM = "AUTOMATION PLATFORM"


@dataclass
class Check:
    """One comparison between what the machine has and what the project needs."""
    name: str
    status: str
    detail: str = ""
    category: str = ENVIRONMENT
    fix: str = ""            # what WOULD repair it; empty when nothing safely can
    auto_fixable: bool = False


@dataclass
class Report:
    host: Dict[str, Optional[str]] = field(default_factory=dict)
    project: Dict[str, Optional[str]] = field(default_factory=dict)
    checks: List[Check] = field(default_factory=list)

    @property
    def failures(self) -> List[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> List[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def auto_fixable(self) -> List[Check]:
        return [c for c in self.checks if c.auto_fixable and c.status in (FAIL, WARN)]

    @property
    def manual(self) -> List[Check]:
        return [c for c in self.checks
                if not c.auto_fixable and c.status in (FAIL, WARN)]

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict:
        """Machine-readable, for diffing a good Mac against a failing one."""
        return {
            "host": self.host,
            "project": self.project,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail,
                 "category": c.category, "fix": c.fix, "auto_fixable": c.auto_fixable}
                for c in self.checks
            ],
            "ok": self.ok,
        }

    def render(self) -> str:
        w = 60
        out = ["=" * w, "BUILD ENVIRONMENT", "=" * w, ""]
        for k, v in self.host.items():
            out.append(f"{k:<20}: {v if v is not None else '— not found —'}")
        if self.project:
            out += ["", "-" * w, "PROJECT REQUIREMENTS", "-" * w, ""]
            for k, v in self.project.items():
                out.append(f"{k:<20}: {v if v is not None else '—'}")
        out += ["", "-" * w, "CHECKS", "-" * w, ""]
        for c in sorted(self.checks, key=lambda c: _SEVERITY[c.status]):
            out.append(f"[{c.status:<4}] {c.name}" + (f" — {c.detail}" if c.detail else ""))
        if self.auto_fixable:
            out += ["", "-" * w, "AUTO-FIXABLE", "-" * w, ""]
            out += [f"- {c.name}: {c.fix}" for c in self.auto_fixable]
        if self.manual:
            out += ["", "-" * w, "MANUAL REVIEW", "-" * w, ""]
            out += [f"- {c.name}: {c.fix or c.detail}" for c in self.manual]
        return "\n".join(out)


# ── Running things ───────────────────────────────────────────────────────────

def _env() -> dict:
    """The environment tools run under.

    Reuses the builder's, which forces UTF-8 — CocoaPods hard-crashes with
    "Unicode Normalization not appropriate for ASCII-8BIT" under the empty locale
    a daemonised agent passes down. That is why a `pod install` typed in a shell
    can fail where the platform's succeeds.
    """
    try:
        from automation.projects.builder import _build_env
        return _build_env()
    except Exception:                                    # pragma: no cover
        env = os.environ.copy()
        for var in ("LC_ALL", "LANG", "LC_CTYPE"):
            if not env.get(var):
                env[var] = "en_US.UTF-8"
        return env


def _run(cmd: List[str], cwd: Optional[str] = None, timeout: int = 60) -> Tuple[bool, str]:
    """Best-effort capture. A missing tool is an answer, not an exception."""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=_env())
        return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()
    except FileNotFoundError:
        return False, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return False, f"{' '.join(cmd[:2])}: timed out"
    except Exception as e:                               # pragma: no cover
        return False, str(e)


def which(tool: str) -> Optional[str]:
    """Absolute path to *tool*, or None. Never assumes a Homebrew prefix."""
    return shutil.which(tool, path=_env().get("PATH"))


def _version(tool: str, args: List[str], pattern: str = r"(\d+\.\d+(?:\.\d+)?)") -> Optional[str]:
    if not which(tool):
        return None
    ok, out = _run([tool, *args])
    if not ok and not out:
        return None
    m = re.search(pattern, out)
    return m.group(1) if m else (out.splitlines()[0].strip() if out else None)


# ── The machine ──────────────────────────────────────────────────────────────

def detect_host() -> Dict[str, Optional[str]]:
    """Versions of everything a React Native iOS build touches."""
    host: Dict[str, Optional[str]] = {}

    host["Architecture"] = platform.machine()          # arm64 | x86_64
    host["macOS"] = platform.mac_ver()[0] or None

    ok, out = _run(["xcodebuild", "-version"])
    if ok:
        host["Xcode"] = (re.search(r"Xcode\s+([\d.]+)", out) or [None, None])[1]
        host["Xcode Build"] = (re.search(r"Build version\s+(\S+)", out) or [None, None])[1]
    else:
        host["Xcode"] = host["Xcode Build"] = None

    ok, out = _run(["xcode-select", "-p"])
    host["Developer Dir"] = out.strip() if ok else None

    # The newest simulator SDK, which is what a simulator build compiles against.
    ok, out = _run(["xcodebuild", "-showsdks"])
    if ok:
        sdks = re.findall(r"iphonesimulator([\d.]+)", out)
        host["iOS SDK"] = max(sdks, key=lambda s: [int(x) for x in s.split(".")]) if sdks else None
    else:
        host["iOS SDK"] = None

    # After an Xcode update its system components (CoreDevice, Mercury) install
    # on first launch. Until then Xcode.app aborts at startup ("quit
    # unexpectedly") and device tooling is mismatched. Exit status 0 = done.
    if host.get("Xcode"):
        ok, _ = _run(["xcodebuild", "-checkFirstLaunchStatus"], timeout=30)
        host["Xcode First Launch"] = "done" if ok else "pending"

    host["Node"] = _version("node", ["--version"])
    host["Yarn"] = _version("yarn", ["--version"])
    host["npm"] = _version("npm", ["--version"])
    host["Ruby"] = _version("ruby", ["--version"])
    host["CocoaPods"] = _version("pod", ["--version"])
    host["Bundler"] = _version("bundle", ["--version"])

    brew = which("brew")
    if brew:
        ok, prefix = _run([brew, "--prefix"])
        host["Homebrew"] = prefix.strip() if ok else "installed"
    else:
        host["Homebrew"] = None

    # Paths matter as much as versions: two Macs with "CocoaPods 1.16.2" can be
    # running different installs (system gem vs Homebrew vs bundler).
    for tool in ("node", "yarn", "ruby", "pod"):
        host[f"{tool} path"] = which(tool)

    return host


# ── The project ──────────────────────────────────────────────────────────────

def _json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return json.load(fh)
    except Exception:
        return {}


def detect_project(repo_path: str) -> Dict[str, Optional[str]]:
    """What the repository says it needs. Reads files only."""
    p: Dict[str, Optional[str]] = {}
    pkg = _json(os.path.join(repo_path, "package.json"))
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}

    p["React Native"] = deps.get("react-native")
    p["React"] = deps.get("react")
    p["Package manager"] = pkg.get("packageManager")
    p["patch-package"] = deps.get("patch-package")
    p["postinstall"] = (pkg.get("scripts") or {}).get("postinstall")

    for name, rel in (("Ruby (.ruby-version)", ".ruby-version"),
                      ("Gemfile", "Gemfile"),
                      ("Gemfile.lock", "Gemfile.lock")):
        full = os.path.join(repo_path, rel)
        if os.path.exists(full):
            if rel == ".ruby-version":
                try:
                    p[name] = open(full, encoding="utf-8").read().strip()
                except OSError:
                    p[name] = "present"
            else:
                p[name] = "present"
        else:
            p[name] = None

    ios = os.path.join(repo_path, "ios")
    podfile = os.path.join(ios, "Podfile")
    if os.path.exists(podfile):
        try:
            text = open(podfile, encoding="utf-8", errors="replace").read()
            m = re.search(r"^\s*platform\s+:ios,\s*['\"]([\d.]+)['\"]", text, re.M)
            p["Podfile platform"] = m.group(1) if m else None
        except OSError:
            p["Podfile platform"] = None
    else:
        p["Podfile platform"] = None

    p["Podfile.lock"] = "present" if os.path.exists(os.path.join(ios, "Podfile.lock")) else None

    patches = os.path.join(repo_path, "patches")
    if os.path.isdir(patches):
        n = len([f for f in os.listdir(patches) if f.endswith(".patch")])
        p["Patches"] = f"{n} found"
    else:
        p["Patches"] = None

    ok, out = _run(["git", "rev-parse", "HEAD"], cwd=repo_path)
    p["Commit"] = out.strip()[:12] if ok else None
    ok, out = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
    p["Branch"] = out.strip() if ok else None

    return p


# ── Checks ───────────────────────────────────────────────────────────────────

def _check_toolchain(host: Dict[str, Optional[str]]) -> List[Check]:
    checks = []

    for tool, cat, fix in (
        ("Node", ENVIRONMENT, "install Node 18+ (nvm, or brew install node)"),
        ("Xcode", XCODE, "install Xcode from the App Store, then xcode-select --install"),
        # Not `brew install cocoapods` on Intel: Homebrew no longer ships Intel
        # bottles, so it compiles LLVM + Rust + Ruby from source (hours).
        ("CocoaPods", COCOAPODS,
         "Apple Silicon: brew install cocoapods. Intel on system Ruby 2.6: "
         "gem install --user-install zeitwerk -v 2.6.18 i18n -v 1.14.8 "
         "minitest -v 5.15.0 public_suffix -v 4.0.7 drb -v 2.0.6 "
         "activesupport -v 6.1.7.10 cocoapods -v 1.15.2, then "
         "ln -sf ~/.gem/ruby/2.6.0/bin/pod /usr/local/bin/pod"),
    ):
        if host.get(tool):
            checks.append(Check(tool, PASS, str(host[tool]), cat))
        else:
            checks.append(Check(tool, FAIL, "not found on PATH", cat, fix))

    # Yarn is only required when the project uses it; decided in the project checks.
    checks.append(Check("Yarn", PASS if host.get("Yarn") else WARN,
                        str(host.get("Yarn") or "not found"), ENVIRONMENT,
                        "" if host.get("Yarn") else "corepack enable, or npm i -g yarn"))

    if host.get("Xcode First Launch") == "pending":
        checks.append(Check(
            "Xcode first launch", FAIL,
            "Xcode's system components are not installed for this version "
            "(Xcode.app quits unexpectedly at launch)", XCODE,
            "sudo xcodebuild -license accept && sudo xcodebuild -runFirstLaunch"))
    if host.get("Xcode") and not host.get("iOS SDK"):
        checks.append(Check(
            "iOS platform", FAIL,
            f"Xcode {host['Xcode']} has no iOS Simulator SDK — builds fail with "
            "'iOS … is not installed'", XCODE,
            "xcodebuild -downloadPlatform iOS (or Xcode › Settings › Components)"))
    checks.extend(_check_nvm(host))

    if host.get("Architecture") not in ("arm64", "x86_64"):
        checks.append(Check("Architecture", WARN, str(host.get("Architecture")), ENVIRONMENT))
    else:
        checks.append(Check("Architecture", PASS, str(host["Architecture"]), ENVIRONMENT))

    # An Xcode whose command line tools point elsewhere builds with a different
    # toolchain than `xcodebuild -version` reports.
    dev = host.get("Developer Dir") or ""
    if dev and "CommandLineTools" in dev:
        checks.append(Check(
            "xcode-select", FAIL, f"points at {dev}", XCODE,
            "sudo xcode-select -s /Applications/Xcode.app/Contents/Developer "
            "— the Command Line Tools cannot build an iOS app"))
    elif dev:
        checks.append(Check("xcode-select", PASS, dev, XCODE))

    return checks


def _nvm_needs_default(home: Optional[str] = None) -> bool:
    """RN's find-node.sh sources ~/.nvm/nvm.sh and then runs `nvm use default`
    (no .nvmrc in these apps). An nvm with no default alias makes that exit
    non-zero, and every React Native script phase -- codegen first -- fails
    with 'N/A: version "default" is not yet installed'."""
    home = home or os.path.expanduser("~")
    nvm = os.path.join(home, ".nvm")
    return (os.path.isfile(os.path.join(nvm, "nvm.sh"))
            and not os.path.isfile(os.path.join(nvm, "alias", "default")))


def ensure_nvm_default(home: Optional[str] = None) -> Optional[str]:
    """Point nvm's default at the system Node when nvm has none. Returns a message
    when it changed something. Creates one file (~/.nvm/alias/default), the same
    thing `nvm alias default system` writes; an existing default is never touched."""
    home = home or os.path.expanduser("~")
    if not _nvm_needs_default(home) or not which("node"):
        return None
    alias_dir = os.path.join(home, ".nvm", "alias")
    try:
        os.makedirs(alias_dir, exist_ok=True)
        with open(os.path.join(alias_dir, "default"), "w") as f:
            f.write("system\n")
    except OSError as e:
        logger.warning("could not set nvm default alias: %s", e)
        return None
    return ("nvm had no default Node — set `nvm alias default system` so React "
            "Native's build scripts use the system Node")


def _check_nvm(host: Dict[str, Optional[str]]) -> List[Check]:
    if not _nvm_needs_default():
        return []
    return [Check("nvm default", WARN,
                  "~/.nvm exists with no default alias — React Native build "
                  "scripts abort on `nvm use default`", ENVIRONMENT,
                  "nvm alias default system (the platform sets this before building)")]


def _check_ruby(repo_path: str, host: Dict[str, Optional[str]]) -> List[Check]:
    """Ruby only matters through CocoaPods, so that is how it is judged.

    A broken native gem shows up as `Ignoring ffi-1.16.3 because its extensions
    are not built` — the gem is installed but unusable, so `pod` may still answer
    --version and then fail mid-install.
    """
    checks = []
    pod = which("pod")
    if not pod:
        return checks     # already reported as a FAIL by the toolchain check

    ok, out = _run([pod, "--version"])
    broken = re.findall(r"Ignoring ([\w.-]+) because its extensions are not built", out)
    if broken:
        gems = ", ".join(sorted(set(broken)))
        checks.append(Check(
            "Ruby native gems", WARN, f"unbuilt extensions: {gems}", ENVIRONMENT,
            f"gem pristine {gems.split(',')[0].rsplit('-', 1)[0]} "
            "— or use the project's Gemfile via bundler",
            auto_fixable=False))   # touching system Ruby is not ours to do silently
    elif ok:
        checks.append(Check("Ruby native gems", PASS, "no unbuilt extensions", ENVIRONMENT))

    # A project-managed Ruby beats whatever is on PATH.
    if os.path.exists(os.path.join(repo_path, "Gemfile")):
        if which("bundle"):
            checks.append(Check(
                "Bundler", INFO,
                "project has a Gemfile — `bundle exec pod` is the reproducible path",
                COCOAPODS))
        else:
            checks.append(Check(
                "Bundler", WARN, "project has a Gemfile but bundler is not installed",
                COCOAPODS, "gem install bundler && bundle install"))
    return checks


def _check_node_modules(repo_path: str) -> List[Check]:
    checks = []
    nm = os.path.join(repo_path, "node_modules")
    if not os.path.isdir(nm):
        return [Check("node_modules", FAIL, "not installed", DEPENDENCY_INSTALL,
                      "yarn install (the platform does this during preparation)",
                      auto_fixable=True)]
    checks.append(Check("node_modules", PASS, "present", DEPENDENCY_INSTALL))

    # Installed BEFORE the lockfile changed means the tree does not match the lock.
    lock = os.path.join(repo_path, "yarn.lock")
    if os.path.exists(lock):
        try:
            if os.path.getmtime(lock) > os.path.getmtime(nm):
                checks.append(Check(
                    "node_modules freshness", WARN,
                    "yarn.lock is newer than node_modules", DEPENDENCY_INSTALL,
                    "yarn install — the installed tree predates the lockfile",
                    auto_fixable=True))
        except OSError:
            pass
    return checks


def _check_patches(repo_path: str) -> List[Check]:
    """patch-package patches: declared, present, and actually applied.

    A patch that is listed but not applied is the quietest failure of the lot —
    the build proceeds against unpatched source and fails somewhere unrelated.
    """
    checks = []
    patches_dir = os.path.join(repo_path, "patches")
    pkg = _json(os.path.join(repo_path, "package.json"))
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    declared = "patch-package" in deps
    postinstall = (pkg.get("scripts") or {}).get("postinstall") or ""

    if not os.path.isdir(patches_dir):
        if declared:
            checks.append(Check(
                "Patches", WARN,
                "patch-package is a dependency but patches/ does not exist on this branch",
                PATCH, "nothing to apply — check whether this branch should carry patches"))
        return checks

    files = sorted(f for f in os.listdir(patches_dir) if f.endswith(".patch"))
    if not files:
        return checks

    if not declared:
        checks.append(Check(
            "patch-package", FAIL,
            f"{len(files)} patch file(s) present but patch-package is not a dependency",
            PATCH, "yarn add -D patch-package — patches are never applied without it"))
    elif "patch-package" not in postinstall:
        checks.append(Check(
            "patch-package postinstall", FAIL,
            f"postinstall is {postinstall!r}; it does not run patch-package",
            PATCH, "add `patch-package` to the postinstall script, "
                   "or the platform must run it explicitly after install"))
    else:
        checks.append(Check("patch-package", PASS, "declared and wired to postinstall", PATCH))

    # Per-patch: does the target package exist, and at the version the patch names?
    for fname in files:
        # patch-package names files <package>+<version>.patch, or
        # <scope>+<package>+<version>.patch for a scoped package.
        stem = fname[:-len(".patch")]
        parts = stem.split("+")
        version = parts[-1] if len(parts) > 1 else ""
        name = "/".join(parts[:-1]) if len(parts) > 1 else stem

        pkg_json = os.path.join(repo_path, "node_modules", *name.split("/"), "package.json")
        if not os.path.exists(pkg_json):
            # Not installed is not a failure: a patch for an optional or removed
            # dependency is inert, not broken.
            checks.append(Check(
                f"patch {name}", INFO,
                f"{name} is not installed — patch not applicable", PATCH))
            continue

        installed = _json(pkg_json).get("version", "")
        if version and installed and installed != version:
            checks.append(Check(
                f"patch {name}", WARN,
                f"patch targets {version}, installed is {installed}", PATCH,
                "patch-package will refuse or need --force; verify the result "
                "rather than editing the patch"))
        else:
            checks.append(Check(f"patch {name}", PASS, f"targets {installed or version}", PATCH))

    return checks


def _check_pods(repo_path: str) -> List[Check]:
    """Podfile / Podfile.lock / Pods / Manifest.lock must agree.

    Xcode's sandbox check compares Podfile.lock with Pods/Manifest.lock and fails
    the build when they differ — a state `pod install` produces silently if it is
    interrupted, and one that an existing Pods/ directory hides.
    """
    checks = []
    ios = os.path.join(repo_path, "ios")
    podfile = os.path.join(ios, "Podfile")
    if not os.path.exists(podfile):
        return [Check("CocoaPods", INFO, "no Podfile — not a CocoaPods project", COCOAPODS)]

    lock = os.path.join(ios, "Podfile.lock")
    pods = os.path.join(ios, "Pods")
    manifest = os.path.join(pods, "Manifest.lock")
    proj = os.path.join(pods, "Pods.xcodeproj")

    if not os.path.isdir(pods):
        return [Check("Pods", FAIL, "ios/Pods does not exist", COCOAPODS,
                      "pod install", auto_fixable=True)]
    if not os.path.isdir(proj):
        return [Check("Pods", FAIL, "Pods/ exists but Pods.xcodeproj does not "
                      "— a previous install did not finish", COCOAPODS,
                      "pod install", auto_fixable=True)]
    if not os.path.exists(lock):
        return [Check("Podfile.lock", FAIL, "missing — the build is not reproducible",
                      COCOAPODS, "pod install (commit the lock in the app repo)",
                      auto_fixable=True)]
    if not os.path.exists(manifest):
        return [Check("Manifest.lock", FAIL,
                      "Pods/Manifest.lock missing — Xcode's sandbox check will fail",
                      COCOAPODS, "pod install", auto_fixable=True)]

    try:
        same = (open(lock, encoding="utf-8", errors="replace").read()
                == open(manifest, encoding="utf-8", errors="replace").read())
    except OSError as e:
        return [Check("Pod state", WARN, f"could not compare locks: {e}", COCOAPODS)]

    if not same:
        checks.append(Check(
            "Pod state", FAIL,
            "Podfile.lock and Pods/Manifest.lock differ — this is the "
            "\"sandbox is not in sync\" build error", COCOAPODS,
            "pod install", auto_fixable=True))
    else:
        checks.append(Check("Pod state", PASS, "Podfile.lock and Manifest.lock agree", COCOAPODS))

    # Pods built from an older node_modules compile the previous dependency versions.
    nm = os.path.join(repo_path, "node_modules")
    if os.path.isdir(nm):
        try:
            if os.path.getmtime(nm) > os.path.getmtime(proj):
                checks.append(Check(
                    "Pod freshness", WARN,
                    "node_modules is newer than Pods — pods may be built from "
                    "previous dependency versions", COCOAPODS,
                    "pod install", auto_fixable=True))
        except OSError:
            pass
    return checks


def _check_deployment_target(repo_path: str) -> List[Check]:
    """Deployment targets below what the dependencies need.

    REPORTED, NEVER REWRITTEN. These live in a TRACKED pbxproj: editing them
    would put the platform's change into the application's git diff. The Pods
    project is generated and may be adjusted at install time; the app project is
    the user's to change.
    """
    checks = []
    ios = os.path.join(repo_path, "ios")
    if not os.path.isdir(ios):
        return checks

    pbx = None
    for entry in os.listdir(ios):
        if entry.endswith(".xcodeproj"):
            candidate = os.path.join(ios, entry, "project.pbxproj")
            if os.path.exists(candidate):
                pbx = candidate
                break
    if not pbx:
        return checks

    try:
        text = open(pbx, encoding="utf-8", errors="replace").read()
    except OSError:
        return checks

    targets = sorted({m for m in re.findall(
        r"IPHONEOS_DEPLOYMENT_TARGET = ([\d.]+);", text)},
        key=lambda s: float(s))
    if not targets:
        return checks

    lowest = float(targets[0])
    # 12.4 is React Native 0.68's floor; several common pods need 13.0 for APIs
    # such as readDataUpToLength:error:.
    if lowest < 13.0:
        checks.append(Check(
            "Deployment target", WARN,
            f"app project targets iOS {', '.join(targets)} (lowest {targets[0]})",
            APPLICATION_SOURCE,
            "raise IPHONEOS_DEPLOYMENT_TARGET to 13.0 in the APPLICATION repo — "
            "a pod calling an iOS 13 API cannot compile against a lower target. "
            "The platform does not edit this tracked file.",
            auto_fixable=False))
    else:
        checks.append(Check("Deployment target", PASS,
                            f"iOS {', '.join(targets)}", APPLICATION_SOURCE))
    return checks


# AssetsLibrary was removed in the iOS 26 SDK. A dependency can reference it in
# ways that differ enormously in consequence, so they are not treated alike.
_ASSETSLIB_SWIFT = re.compile(r"^\s*import\s+AssetsLibrary", re.M)
_ASSETSLIB_OBJC = re.compile(r"#\s*import\s+<AssetsLibrary/|@import\s+AssetsLibrary")
_ASSETSLIB_PODSPEC = re.compile(r"AssetsLibrary")


def check_assetslibrary(repo_path: str, sdk_version: Optional[str] = None) -> List[Check]:
    """Which dependencies reference AssetsLibrary, and whether it can still build.

    Apple removed the framework in the iOS 26 SDK. The three reference kinds are
    NOT equivalent:

      Swift `import AssetsLibrary`   — fatal. There is no module to compile
                                       against, so the build cannot succeed and
                                       no patch can help: nothing can supply a
                                       symbol the SDK does not ship.
      Objective-C `#import <...>`    — usually survives. The headers are still
                                       present and compile with deprecation
                                       warnings.
      podspec frameworks declaration — a link-time reference; it fails only if
                                       the framework is actually needed.

    Reported, never repaired. Removing the framework from a podspec or deleting
    the import changes what the application does, which is not the platform's
    call to make.
    """
    checks: List[Check] = []
    nm = os.path.join(repo_path, "node_modules")
    if not os.path.isdir(nm):
        return checks

    sdk_major = 0
    if sdk_version:
        try:
            sdk_major = int(str(sdk_version).split(".")[0])
        except ValueError:
            sdk_major = 0
    removed = sdk_major >= 26

    swift_importers: Dict[str, List[str]] = {}
    objc_importers: Dict[str, List[str]] = {}
    podspec_only: List[str] = []

    for entry in sorted(os.listdir(nm)):
        if entry.startswith("."):
            continue
        pkg_dir = os.path.join(nm, entry)
        ios_dir = os.path.join(pkg_dir, "ios")
        if not os.path.isdir(ios_dir):
            continue

        for root, _dirs, files in os.walk(ios_dir):
            for f in files:
                ext = os.path.splitext(f)[1]
                if ext not in (".swift", ".m", ".mm", ".h"):
                    continue
                path = os.path.join(root, f)
                try:
                    text = open(path, encoding="utf-8", errors="replace").read()
                except OSError:
                    continue
                rel = os.path.relpath(path, pkg_dir)
                if ext == ".swift" and _ASSETSLIB_SWIFT.search(text):
                    swift_importers.setdefault(entry, []).append(rel)
                elif _ASSETSLIB_OBJC.search(text):
                    objc_importers.setdefault(entry, []).append(rel)

        if entry not in swift_importers and entry not in objc_importers:
            for f in os.listdir(pkg_dir):
                if f.endswith(".podspec"):
                    try:
                        if _ASSETSLIB_PODSPEC.search(
                                open(os.path.join(pkg_dir, f),
                                     encoding="utf-8", errors="replace").read()):
                            podspec_only.append(entry)
                    except OSError:
                        pass

    for pkg, files in sorted(swift_importers.items()):
        version = _json(os.path.join(nm, pkg, "package.json")).get("version", "?")
        checks.append(Check(
            f"AssetsLibrary ({pkg})", FAIL if removed else WARN,
            f"Swift imports AssetsLibrary in {', '.join(files[:3])}",
            APPLICATION_DEPENDENCY,
            f"{pkg}@{version} cannot build against this SDK — Apple removed "
            "AssetsLibrary in iOS 26 and a Swift import needs a module that no "
            "longer exists. The dependency must be upgraded or replaced; the "
            "platform cannot patch in a missing framework.",
            auto_fixable=False))

    for pkg, files in sorted(objc_importers.items()):
        version = _json(os.path.join(nm, pkg, "package.json")).get("version", "?")
        checks.append(Check(
            f"AssetsLibrary ({pkg})", INFO,
            f"Objective-C references AssetsLibrary in {', '.join(files[:2])}",
            APPLICATION_DEPENDENCY,
            f"{pkg}@{version} uses the deprecated headers, which still compile "
            "with warnings. Advisory only — this is not what fails a build."))

    for pkg in sorted(podspec_only):
        checks.append(Check(
            f"AssetsLibrary ({pkg})", INFO,
            "podspec declares the framework but no source imports it",
            APPLICATION_DEPENDENCY,
            "link-time reference only — not a compile blocker"))

    return checks


# ── Entry point ──────────────────────────────────────────────────────────────

def doctor(repo_path: Optional[str] = None) -> Report:
    """Full preflight. Reads only; never repairs, never writes.

    Safe to run repeatedly and safe to run on a healthy machine, which is what
    makes it usable for comparing a working Mac against a failing one.
    """
    host = detect_host()
    report = Report(host=host)

    report.checks.extend(_check_toolchain(host))

    if repo_path and os.path.isdir(repo_path):
        repo_path = os.path.abspath(repo_path)
        report.project = detect_project(repo_path)
        report.checks.extend(_check_ruby(repo_path, host))
        report.checks.extend(_check_node_modules(repo_path))
        report.checks.extend(_check_patches(repo_path))
        report.checks.extend(_check_pods(repo_path))
        report.checks.extend(_check_deployment_target(repo_path))
        report.checks.extend(check_assetslibrary(repo_path, host.get("iOS SDK")))

    return report


def compare(a: dict, b: dict, label_a: str = "A", label_b: str = "B") -> str:
    """Differences between two doctor reports — a good Mac against a failing one.

    Takes `Report.to_dict()` output so the two can come from different machines.
    """
    lines = [f"{'':<22}{label_a:<28}{label_b}"]
    lines.append("-" * 78)
    for section in ("host", "project"):
        keys = sorted(set(a.get(section, {})) | set(b.get(section, {})))
        for k in keys:
            va, vb = a.get(section, {}).get(k), b.get(section, {}).get(k)
            if va != vb:
                lines.append(f"{k:<22}{str(va):<28}{vb}")

    status_a = {c["name"]: c["status"] for c in a.get("checks", [])}
    status_b = {c["name"]: c["status"] for c in b.get("checks", [])}
    for name in sorted(set(status_a) | set(status_b)):
        sa, sb = status_a.get(name, "—"), status_b.get(name, "—")
        if sa != sb:
            lines.append(f"{name:<22}{sa:<28}{sb}")

    if len(lines) == 2:
        lines.append("(no differences)")
    return "\n".join(lines)


def _main() -> int:
    """`python -m automation.projects.macos_environment [repo] [--json]`

    --json prints the machine-readable report, which is what gets carried from a
    working Mac to a failing one and fed to `compare`.
    """
    import argparse
    ap = argparse.ArgumentParser(description="Build environment preflight (read-only).")
    ap.add_argument("repo", nargs="?", help="path to the project clone")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--compare", metavar="FILE",
                    help="diff this machine against a --json report from another Mac")
    args = ap.parse_args()

    report = doctor(args.repo)

    if args.compare:
        with open(args.compare, encoding="utf-8") as fh:
            other = json.load(fh)
        print(compare(other, report.to_dict(), os.path.basename(args.compare), "this Mac"))
        return 0

    print(json.dumps(report.to_dict(), indent=2) if args.json else report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":                               # pragma: no cover
    raise SystemExit(_main())
