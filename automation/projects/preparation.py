"""Project preparation pipeline.

Implements the corrected execution flow in ONE place so the agent, the API and
the framework plugins all share it:

    repository exists?  no -> clone
                        yes -> pull latest
        -> checkout configured branch
        -> detect project type
        -> run type-aware validation
        -> install missing dependencies
        -> (caller executes tests)

Validation now runs *after* the repository is on disk and prepared, which is
what fixes the "automation.yaml not found" / ".venv not found" failures that
came from validating before the repo was ready.
"""

import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from automation.database import database
from automation.database.config import SessionLocal
from automation.database.models import TestProject
from automation.projects.detector import (
    ProjectType,
    detect_project_type,
    write_automation_yaml,
)
from automation.projects.builder import app_builder
from automation.projects.repository import repository_manager
from automation.projects import setup_measure as measure
from automation.projects.setup_report import PENDING as PENDING_STATUS, SetupReport
from automation.utils.validator import EnvironmentValidator, ValidationResult

logger = logging.getLogger(__name__)

# Repository lifecycle states (mirrors TestProject.clone_status).
NOT_CLONED = "not_cloned"
SYNCING = "syncing"
CLONED = "cloned"
READY = "ready"
OUTDATED = "outdated"
CLONE_FAILED = "clone_failed"


@dataclass
class PreparationResult:
    ok: bool
    project_type: str = ProjectType.UNKNOWN
    branch: Optional[str] = None
    clone_status: str = NOT_CLONED
    steps: List[str] = field(default_factory=list)
    error: Optional[str] = None
    validation: Optional[ValidationResult] = None
    # True when automation.yaml is missing — the dashboard offers to generate it.
    needs_automation_yaml: bool = False
    # Measured setup report (stage timings, sizes, counts). None when preparation
    # bailed out before any stage was recorded.
    report: Optional["SetupReport"] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "project_type": self.project_type,
            "branch": self.branch,
            "clone_status": self.clone_status,
            "steps": self.steps,
            "error": self.error,
            "needs_automation_yaml": self.needs_automation_yaml,
            "validation": self.validation.to_dict() if self.validation else None,
            "setup_report": self.report.to_dict() if self.report else None,
        }


class ProjectPreparationService:
    """Prepares a project's working copy and environment for execution."""

    # ── DB helpers ───────────────────────────────────────────────────────────

    def _load_project(self, project_id: str) -> Optional[TestProject]:
        try:
            with SessionLocal() as db:
                p = database.get_test_project(db, project_id)
                if not p:
                    return None
                # Detach a plain snapshot so callers don't hold a live session.
                db.expunge(p)
                return p
        except Exception as e:
            logger.warning(f"[{project_id}] Could not load project from DB: {e}")
            return None

    def _env_config_for_project(self, project_id: str, project_name: str,
                                step: Callable[[str], None]):
        """The environment build configuration this project should be built as.

        Derived from the project's own configured bundle id (falling back to its
        name), so nothing here knows about any particular app. A project with no
        entry in project-environments.json builds exactly as it always did -- this
        must not become a new way for a build to fail.
        """
        from automation.projects import environments as envmod
        project = self._load_project(project_id)
        bundle = getattr(project, "app_bundle_id", None) if project else None
        env_name = envmod.environment_for_bundle(bundle) if bundle else None
        if not env_name:
            return None
        try:
            cfg = envmod.resolve(env_name, bundle_id=bundle, name=project_name)
        except envmod.EnvironmentNotConfigured as e:
            step(f"⚠ {e}")
            return None
        # Make the decision visible: a build that quietly picks a variant is how the
        # wrong app got deployed for weeks.
        step(f"Environment: {cfg.environment} (bundle {cfg.bundle_id}, "
             f"configuration {cfg.configuration})")
        return cfg

    def _update_project(self, project_id: str, **fields) -> None:
        """Persist project fields. Never fatal — the agent may run without DB access."""
        try:
            with SessionLocal() as db:
                p = db.query(TestProject).filter(TestProject.id == project_id).first()
                if not p:
                    return
                for k, v in fields.items():
                    if hasattr(p, k):
                        setattr(p, k, v)
                db.commit()
        except Exception as e:
            logger.warning(f"[{project_id}] Could not persist project state: {e}")

    # ── Repository sync ──────────────────────────────────────────────────────

    def sync_repository(
        self,
        project_id: str,
        git_url: str,
        branch: str,
        force_reclone: bool = False,
        on_step: Optional[Callable[[str], None]] = None,
    ) -> PreparationResult:
        """Clone (if missing) or pull, then check out *branch*.

        Never validates — this only guarantees the working copy exists and is
        up to date. Persists clone_status/current_branch/last_pull_at.
        """
        steps: List[str] = []

        def step(msg: str) -> None:
            steps.append(msg)
            logger.info(f"[{project_id}] {msg}")
            if on_step:
                on_step(msg)

        self._update_project(project_id, clone_status=SYNCING, clone_error=None)

        try:
            # The configured branch may not exist on the remote ('main' vs
            # 'master'). Resolve it once, up front, so the clone, the checkout
            # and the pull below all agree on the same branch.
            resolved = repository_manager.resolve_branch(git_url, branch)
            if resolved != branch:
                step(
                    f"Branch '{branch}' not found on the remote — using the "
                    f"repository's default branch '{resolved}' instead."
                )
                branch = resolved

            if force_reclone:
                step("Re-cloning repository from scratch...")
                repository_manager.re_clone(project_id, git_url, branch)
                step("Re-clone complete.")
            elif not repository_manager.is_cloned(project_id):
                step(f"Repository not cloned — cloning {git_url} (branch {branch})...")
                repository_manager.clone(project_id, git_url, branch)
                step("Clone complete.")
            else:
                step("Repository already cloned.")

            # Always land on the configured branch before pulling.
            step(f"Checking out branch '{branch}'...")
            repository_manager.checkout_branch(project_id, branch)

            step("Pulling latest changes...")
            repository_manager.pull(project_id, branch)
            step("Pull complete.")

            current = repository_manager.get_current_branch(project_id) or branch
            self._update_project(
                project_id,
                clone_status=CLONED,
                current_branch=current,
                last_pull_at=datetime.utcnow(),
                clone_error=None,
            )
            return PreparationResult(
                ok=True, branch=current, clone_status=CLONED, steps=steps
            )

        except Exception as e:
            msg = str(e)
            step(f"Repository sync FAILED: {msg}")
            self._update_project(
                project_id, clone_status=CLONE_FAILED, clone_error=msg
            )
            return PreparationResult(
                ok=False, clone_status=CLONE_FAILED, steps=steps, error=msg
            )

    # ── Type detection ───────────────────────────────────────────────────────

    def detect_and_store_type(self, project_id: str) -> str:
        """Detect the project type from the cloned tree and persist it."""
        repo_path = repository_manager.get_repo_path(project_id)
        result = detect_project_type(repo_path)
        self._update_project(project_id, project_type=result.project_type)
        logger.info(
            f"[{project_id}] Detected project type: {result.label} "
            f"(markers: {', '.join(result.markers) or 'none'})"
        )
        return result.project_type

    # ── Dependency install ───────────────────────────────────────────────────

    def install_dependencies(
        self, project_id: str, project_type: str, stage=None
    ) -> "tuple[bool, Optional[str]]":
        """Install dependencies appropriate to the detected project type.

        Returns ``(ok, error_output)`` — the error text is surfaced to the
        dashboard so a failure is diagnosable without digging through logs.

        Only Python projects get a .venv — React Native / native mobile projects
        use their own toolchains and must never be forced through pip.
        """
        import os

        repo_path = repository_manager.get_repo_path(project_id)

        if project_type == ProjectType.PYTHON:
            if not repository_manager.ensure_venv_exists(project_id):
                return False, "Could not create the Python virtual environment."
            config = repository_manager.validate_yaml(project_id)
            req_file = config.requirements.file if config else "requirements.txt"
            ok = repository_manager.install_dependencies(project_id, req_file)
            return ok, None if ok else f"pip install -r {req_file} failed. See server logs."

        if project_type == ProjectType.REACT_NATIVE:
            return self._install_node_deps(project_id, repo_path, stage=stage)

        if project_type == ProjectType.FLUTTER:
            return self._run_in_repo(project_id, ["flutter", "pub", "get"])

        if project_type == ProjectType.IOS:
            # CocoaPods lives in ios/ for RN and at the root for a native app —
            # the builder resolves whichever applies.
            ok, out = app_builder._pod_install(repo_path)
            return ok, None if ok else out

        if project_type == ProjectType.JAVA:
            return self._run_in_repo(project_id, ["mvn", "-q", "dependency:resolve"])

        if project_type == ProjectType.ANDROID:
            # Gradle resolves dependencies as part of the build itself.
            return True, None

        # Unknown type — nothing safe to install; let validation report it.
        return True, None

    def _ensure_node_modules_linker(self, project_id: str, repo_path: str) -> None:
        """Force Yarn Berry to produce a real node_modules tree.

        Berry defaults to Plug'n'Play, which React Native and Metro do not
        support — the install "succeeds" and leaves node_modules empty, so every
        module resolves as missing. Only written when the project has not already
        chosen a linker.
        """
        rc = os.path.join(repo_path, ".yarnrc.yml")
        try:
            if os.path.exists(rc):
                with open(rc) as f:
                    text = f.read()
                if "nodeLinker" in text:
                    # Respect the project's own choice -- UNLESS it chose PnP,
                    # which React Native cannot use. Yarn writes `nodeLinker: pnp`
                    # itself when it takes that path, so this is as often the
                    # tool's decision as the team's, and it leaves Metro with no
                    # node_modules to resolve from.
                    if not re.search(r"^\s*nodeLinker:\s*pnp\b", text, re.M):
                        return
                    logger.warning("[%s] .yarnrc.yml selects Plug'n'Play, which "
                                   "Metro cannot use — switching to node-modules",
                                   project_id)
                    text = re.sub(r"^\s*nodeLinker:.*$", "nodeLinker: node-modules",
                                  text, count=1, flags=re.M)
                    with open(rc, "w") as f:
                        f.write(text)
                    return
                with open(rc, "a") as f:
                    f.write("\nnodeLinker: node-modules\n")
            else:
                with open(rc, "w") as f:
                    f.write("nodeLinker: node-modules\n")
            logger.info(f"[{project_id}] Set nodeLinker: node-modules (RN cannot use Yarn PnP)")
        except OSError as e:
            logger.warning(f"[{project_id}] Could not write .yarnrc.yml: {e}")

    def apply_patch_package(self, repo_path: str) -> List[str]:
        """Apply patches/*.patch, reporting one line per patch.

        WHY THE PLATFORM DOES THIS
            patch-package normally runs from a postinstall script. This project
            ships four patches and declares neither patch-package nor a
            postinstall that runs it, so a clean install applies none of them --
            which is the difference between the Mac where the app builds and a
            fresh one.

        SAFE BY CONSTRUCTION
            Every patch edits node_modules, which is generated and gitignored.
            Nothing tracked is touched, so this can never end up in an
            application diff.

        NEVER FATAL
            A patch that does not apply is reported and skipped. Most patches fix
            one symptom; failing the whole build over one is worse than building
            without it, and the message names the file so it can be judged.
        """
        import json
        import subprocess

        messages: List[str] = []
        patches_dir = os.path.join(repo_path, "patches")
        if not os.path.isdir(patches_dir):
            return messages

        patches = sorted(f for f in os.listdir(patches_dir) if f.endswith(".patch"))
        if not patches:
            return messages

        node_modules = os.path.join(repo_path, "node_modules")
        applied, skipped, failed = 0, 0, 0

        for fname in patches:
            stem = fname[: -len(".patch")]
            parts = stem.split("+")
            pkg = "/".join(parts[:-1]) if len(parts) > 1 else stem
            want = parts[-1] if len(parts) > 1 else ""

            pkg_json = os.path.join(node_modules, *pkg.split("/"), "package.json")
            if not os.path.exists(pkg_json):
                # Not installed: an inert patch, not a broken one.
                skipped += 1
                messages.append(f"Patch skipped — {pkg} is not installed ({fname})")
                continue

            try:
                with open(pkg_json, encoding="utf-8", errors="replace") as fh:
                    have = json.load(fh).get("version", "")
            except Exception:
                have = ""

            # A patch built against another version may still apply, but it is
            # reported rather than forced silently: the version it targets is the
            # only claim the filename makes.
            if want and have and want != have:
                messages.append(
                    f"Patch targets {pkg}@{want} but {have} is installed — "
                    f"applying anyway and verifying the result ({fname})")

            proc = subprocess.run(
                ["git", "apply", "--check", os.path.join("patches", fname)],
                cwd=repo_path, capture_output=True, text=True, timeout=120)
            if proc.returncode != 0:
                # Already applied is the common case on a re-run, and is success.
                reverse = subprocess.run(
                    ["git", "apply", "--reverse", "--check",
                     os.path.join("patches", fname)],
                    cwd=repo_path, capture_output=True, text=True, timeout=120)
                if reverse.returncode == 0:
                    applied += 1
                    messages.append(f"Patch already applied — {pkg} ({fname})")
                else:
                    failed += 1
                    messages.append(
                        f"Patch does NOT apply to {pkg}@{have} — skipped, the "
                        f"build continues unpatched ({fname})")
                continue

            proc = subprocess.run(
                ["git", "apply", os.path.join("patches", fname)],
                cwd=repo_path, capture_output=True, text=True, timeout=120)
            if proc.returncode == 0:
                applied += 1
                messages.append(f"Patch applied — {pkg}@{have} ({fname})")
            else:
                failed += 1
                messages.append(
                    f"Patch FAILED on {pkg}@{have}: "
                    f"{(proc.stderr or '').strip()[:200]} ({fname})")

        messages.append(
            f"Patches: {applied} applied, {skipped} not applicable, {failed} failed")
        return messages

    _QR_CSS_IMPORT = 'import { LocalSvg } from "react-native-svg/css";'
    _QR_CSS_STUB = ('// platform: react-native-svg < 13 has no /css entry; LocalSvg '
                    '(local-asset logos only) renders nothing.\n'
                    'const LocalSvg = () => null;')

    def _fix_qrcode_svg_css_import(self, repo_path: str) -> List[str]:
        """Let Metro bundle react-native-qrcode-svg >= 6.3 against react-native-svg 12.

        vya-consumer's own lockfile pairs qrcode-svg 6.3.12 (peer: svg >= 13.2)
        with svg 12.5.1. qrcode-svg's LogoSVG imports `react-native-svg/css`,
        which svg 12 does not ship, and Metro fails the WHOLE bundle on an
        unresolvable import -- a red "Failed to compile" screen at launch, even
        though the app never passes `logoSVG` and so never renders LocalSvg.
        Only applied while the installed svg lacks the entry; node_modules only.
        """
        nm = os.path.join(repo_path, "node_modules")
        logo = os.path.join(nm, "react-native-qrcode-svg", "src", "LogoSVG",
                            "index.native.js")
        if not os.path.isfile(logo) or os.path.exists(
                os.path.join(nm, "react-native-svg", "css")):
            return []
        try:
            with open(logo, encoding="utf-8") as fh:
                src = fh.read()
            if self._QR_CSS_IMPORT not in src:
                return []
            with open(logo, "w", encoding="utf-8") as fh:
                fh.write(src.replace(self._QR_CSS_IMPORT, self._QR_CSS_STUB))
        except OSError as e:
            return [f"Toolchain fix FAILED on react-native-qrcode-svg LogoSVG: {e}"]
        return ["Toolchain fix applied — react-native-qrcode-svg imports "
                "react-native-svg/css, which the installed react-native-svg "
                "does not have; stubbed LocalSvg so Metro can bundle"]

    _QR_TEXTENCODER = "this.data = new TextEncoder().encode(data)"
    # UTF-8 without TextEncoder: what qrcode <= 1.5.3 did via encode-utf8.
    _QR_UTF8 = (
        "this.data = typeof TextEncoder !== 'undefined'\n"
        "      ? new TextEncoder().encode(data)\n"
        "      // platform: RN 0.68's JavaScriptCore has no TextEncoder\n"
        "      : new Uint8Array(unescape(encodeURIComponent(data)).split('')"
        ".map(function (c) { return c.charCodeAt(0) }))")

    def _fix_qrcode_textencoder(self, repo_path: str) -> List[str]:
        """Let qrcode >= 1.5.4 encode text on React Native's JavaScriptCore.

        qrcode 1.5.4 replaced its UTF-8 helper with `new TextEncoder()`, which RN
        0.68 does not provide. react-native-qrcode-svg 6.1.2 (the RN-0.68
        compatible version the platform pins) resolves a nested qrcode 1.5.4, so
        every QR code -- the booking card's -- threw "Can't find variable:
        TextEncoder" and blanked the screen. Any copy, top-level or nested.
        """
        import glob as _glob
        out: List[str] = []
        nm = os.path.join(repo_path, "node_modules")
        for path in _glob.glob(os.path.join(nm, "**", "qrcode", "lib", "core",
                                            "byte-data.js"), recursive=True):
            try:
                with open(path, encoding="utf-8") as fh:
                    src = fh.read()
                if self._QR_TEXTENCODER not in src or "platform: RN 0.68" in src:
                    continue
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(src.replace(self._QR_TEXTENCODER, self._QR_UTF8))
            except OSError as e:
                out.append(f"Toolchain fix FAILED on {os.path.relpath(path, repo_path)}: {e}")
                continue
            out.append(f"Toolchain fix applied — {os.path.relpath(path, repo_path)}: "
                       "UTF-8 fallback for TextEncoder (missing on RN 0.68)")
        return out

    # `operator"" _pt` -> `operator""_pt`. Identical meaning; only the spelling
    # newer clang accepts.
    _LITERAL_OP_SPACE = re.compile(r'operator""\s+(_\w+)')

    def apply_toolchain_fixes(self, repo_path: str) -> List[str]:
        """Rewrite React Native source that the current Xcode refuses to compile.

        WHY
            RN <= 0.70's Yoga declares `operator"" _pt` (space before the
            suffix). Xcode 26's clang warns -Wdeprecated-literal-operator on it,
            and Yoga.podspec compiles with -Werror, so a spelling warning fails
            the whole build. Other pods (Folly, boost, fmt) carry the same
            spelling but do not use -Werror, so they only warn.

        SAFE BY CONSTRUCTION
            Same guarantee as apply_patch_package: only node_modules (generated,
            gitignored) is edited, never the application's tracked files, and the
            rewrite is token-for-token equivalent C++. Idempotent — a re-run finds
            nothing to change.
        """
        messages: List[str] = self._fix_qrcode_svg_css_import(repo_path)
        messages += self._fix_qrcode_textencoder(repo_path)
        yoga_dir = os.path.join(repo_path, "node_modules", "react-native",
                                "ReactCommon", "yoga")
        if not os.path.isdir(yoga_dir):
            return messages

        for root, _dirs, files in os.walk(yoga_dir):
            for fname in files:
                if not fname.endswith((".h", ".hpp", ".cpp")):
                    continue
                path = os.path.join(root, fname)
                try:
                    with open(path, encoding="utf-8") as fh:
                        src = fh.read()
                    fixed, n = self._LITERAL_OP_SPACE.subn(r'operator""\1', src)
                    if not n:
                        continue
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(fixed)
                except (OSError, UnicodeDecodeError) as e:
                    messages.append(f"Toolchain fix FAILED on "
                                    f"{os.path.relpath(path, repo_path)}: {e}")
                    continue
                messages.append(
                    f"Toolchain fix applied — {os.path.relpath(path, repo_path)}: "
                    f"{n} literal operator(s) respelled for Xcode 26 clang "
                    f"(-Wdeprecated-literal-operator under Yoga's -Werror)")
        return messages

    def _install_node_deps(
        self, project_id: str, repo_path: str, stage=None
    ) -> "tuple[bool, Optional[str]]":
        """Install JS dependencies, tolerating the peer-dependency conflicts that
        are endemic to real React Native apps.

        npm 7+ hard-fails on any unmet peer range (ERESOLVE). Mature RN projects
        routinely carry such conflicts and are installed in practice with yarn or
        --legacy-peer-deps, so a bare `npm install` is not a usable gate. We
        honour the project's own lockfile first, then fall back.
        """
        import os

        # 1. Respect the project's lockfile — and the package manager that wrote
        #    it. This is not a preference, it is correctness: a Yarn Berry (v2+)
        #    lockfile is a different FORMAT, and npm/Yarn-Classic cannot read it.
        #    They do not fail — they DISCARD it and re-resolve every caret range
        #    to the newest release. That silently turned a project pinned to
        #    axios 1.7.7 / apisauce 3.1.0 into axios 1.18.1 / apisauce 3.2.2,
        #    which then could not bundle at all. Always install with the manager
        #    that owns the lockfile.
        if os.path.exists(os.path.join(repo_path, "yarn.lock")):
            pm = app_builder.detect_package_manager(repo_path)

            # React Native cannot use Yarn Berry's default Plug'n'Play linker —
            # Metro needs a real node_modules tree. Berry projects that omit a
            # .yarnrc.yml would otherwise install to .pnp.cjs and leave
            # node_modules empty.
            # Any Berry invocation, however the version is pinned. This was an
            # equality check against ["corepack", "yarn"] and silently stopped
            # matching the moment the Yarn major started being pinned
            # (["corepack", "yarn@3", "--"]), so the linker was never written and
            # Yarn fell back to Plug'n'Play — "ESM support for PnP", and a
            # validation step reporting node_modules as missing because it was.
            if pm[0] == "corepack":
                self._ensure_node_modules_linker(project_id, repo_path)

            if stage is not None:
                # Same command, but streamed so the install can be measured while
                # it runs rather than only after it returns.
                ok, out = measure.measure_js_install(stage, repo_path, pm + ["install"])
                err = None if ok else out
            else:
                ok, err = self._run_in_repo(project_id, pm + ["install"])
            if ok:
                return True, None

            # Do NOT fall through to npm. npm cannot read a Berry lockfile: it
            # discards it, re-resolves every caret range to the newest release
            # and rewrites the file, which is how this project's axios went
            # 1.7.7 -> 1.20.0 and stopped bundling. A failed yarn install is a
            # problem to report, not a reason to reach for the tool that
            # corrupts the tree.
            return False, (
                f"`{' '.join(pm + ['install'])}` failed in this project.\n\n"
                f"Not retried with npm on purpose: this project has a Yarn "
                f"lockfile, and npm would discard it and re-resolve every "
                f"version rather than install the ones it pins.\n\n{err or ''}")

        # 2. Plain npm install.
        if stage is not None:
            ok, out = measure.measure_js_install(stage, repo_path, ["npm", "install"])
            err = None if ok else out
        else:
            ok, err = self._run_in_repo(project_id, ["npm", "install"])
        if ok:
            return True, None

        # 3. Peer-dependency conflict → retry the way the RN ecosystem actually
        #    installs. Anything else is a genuine failure and is reported as-is.
        if err and "ERESOLVE" in err:
            logger.warning(
                f"[{project_id}] npm ERESOLVE peer conflict — retrying with --legacy-peer-deps"
            )
            ok, err2 = self._run_in_repo(
                project_id, ["npm", "install", "--legacy-peer-deps"]
            )
            if ok:
                # This is NOT a clean success. --legacy-peer-deps accepts a
                # dependency graph npm considers invalid, so native modules may
                # be built against an incompatible React Native version. That
                # surfaces much later as an obscure compile error, so say so now
                # rather than letting the build fail mysteriously.
                return True, (
                    "WARNING: npm reported peer-dependency conflicts (ERESOLVE) and they "
                    "were bypassed with --legacy-peer-deps. The installed native module "
                    "versions may not match this project's React Native version and can "
                    "fail to compile. Fix the version constraints in package.json for a "
                    "reliable build."
                )
            return False, err2

        return False, err

    def _run_in_repo(
        self, project_id: str, cmd: List[str]
    ) -> "tuple[bool, Optional[str]]":
        """Run *cmd* in the repo. Returns (ok, error_output)."""
        import subprocess

        repo_path = repository_manager.get_repo_path(project_id)
        try:
            res = subprocess.run(
                cmd, cwd=repo_path, capture_output=True, text=True, timeout=1800
            )
            if res.returncode != 0:
                # npm writes its errors to stdout as often as stderr.
                output = (res.stderr or "") + (res.stdout or "")
                logger.error(f"[{project_id}] {' '.join(cmd)} failed: {output[:1000]}")
                return False, output.strip()[-1500:]
            return True, None
        except FileNotFoundError:
            msg = (
                f"Command not found: {cmd[0]}. Install it and make sure it is on the "
                f"PATH of the process running the platform."
            )
            logger.error(f"[{project_id}] {msg}")
            return False, msg
        except subprocess.TimeoutExpired:
            msg = f"{' '.join(cmd)} timed out after 30 minutes."
            logger.error(f"[{project_id}] {msg}")
            return False, msg
        except Exception as e:
            logger.error(f"[{project_id}] {' '.join(cmd)} error: {e}")
            return False, str(e)

    # ── Full pipeline ────────────────────────────────────────────────────────

    def prepare_for_execution(
        self,
        project_id: str,
        device_id: Optional[str] = None,
        # Generate a missing automation.yaml rather than refusing to prepare.
        # automation.yaml is PLATFORM-OWNED and untracked (it records the built
        # artifact path, which is machine-specific), so a fresh clone never has one --
        # on any new machine, for every project. Defaulting to False meant the first
        # prepare after a clone failed with "automation.yaml not found" and a template
        # the platform can write itself. Callers that want the old behaviour (e.g. a
        # UI that offers to generate it) still pass False explicitly.
        auto_generate_yaml: bool = True,
        on_step: Optional[Callable[[str], None]] = None,
        git_url: Optional[str] = None,
        branch: Optional[str] = None,
        platform: Optional[str] = None,
        project_name: Optional[str] = None,
    ) -> PreparationResult:
        """Run the full pre-execution pipeline for a project.

        clone/pull -> checkout -> detect -> validate -> install deps.
        Returns a PreparationResult; ``ok=False`` means execution must not start.

        The git_url/branch/platform/project_name overrides let the execution
        agent drive this straight from its job payload, without needing database
        access of its own.
        """
        project = self._load_project(project_id)

        git_url = git_url or (project.git_url if project else None)
        branch = branch or (project.default_branch if project else None) or "main"
        platform = platform or (project.platform if project else None) or "ios"
        project_name = project_name or (project.name if project else project_id)

        if not git_url:
            return PreparationResult(
                ok=False,
                error=f"Project {project_id} not found and no git_url supplied.",
            )

        # The measured report. Stages are recorded around the SAME calls the
        # pipeline already makes, so the report can never describe work that did
        # not run.
        import os as _os

        report = SetupReport(project_name=project_name or project_id)
        repo_path_early = repository_manager.get_repo_path(project_id)
        report.disk_free_before = measure.disk_free(
            repo_path_early if _os.path.isdir(repo_path_early)
            else _os.path.dirname(repo_path_early) or "/")
        repo_existed = _os.path.isdir(_os.path.join(repo_path_early, ".git"))
        if repo_existed:
            report.warm, report.reuse = measure.observe_state(repo_path_early)

        # 0. Environment — reused wholesale from the existing doctor rather than
        # re-shelling every tool here.
        env_stage = report.stage("environment", "Environment validation").start()
        try:
            from automation.projects import macos_environment as _env
            report.host = _env.detect_host()
            env_stage.complete()
        except Exception as e:                       # never fail setup on reporting
            env_stage.fail(str(e), classification="ENVIRONMENT")
            logger.warning("[%s] environment detection failed: %s", project_id, e)

        # 1-3. Clone / pull / checkout.
        clone_stage = report.stage("clone", "Repository preparation")
        sync_holder: Dict[str, Any] = {}

        def _do_sync():
            res = self.sync_repository(project_id, git_url, branch, on_step=on_step)
            sync_holder["result"] = res
            return res.ok, "\n".join(res.steps) + ("\n" + (res.error or ""))

        measure.measure_clone(clone_stage, repo_path_early, _do_sync, repo_existed)
        sync = sync_holder["result"]
        if not sync.ok:
            sync.report = report
            return sync

        try:
            from automation.projects import macos_environment as _env2
            report.project = _env2.detect_project(repo_path_early)
        except Exception:
            pass
        if report.warm is None:
            report.warm, report.reuse = measure.observe_state(repo_path_early)

        steps = list(sync.steps)

        def step(msg: str) -> None:
            steps.append(msg)
            logger.info(f"[{project_id}] {msg}")
            if on_step:
                on_step(msg)

        # 4. Detect project type.
        project_type = self.detect_and_store_type(project_id)
        step(f"Detected project type: {project_type}")

        # 5. automation.yaml — offer to generate rather than failing outright.
        import os

        repo_path = repository_manager.get_repo_path(project_id)
        yaml_path = os.path.join(repo_path, "automation.yaml")
        if not os.path.exists(yaml_path):
            if auto_generate_yaml:
                write_automation_yaml(
                    repo_path,
                    project_name=project_name,
                    project_type=project_type,
                    platform=platform,
                    branch=branch,
                )
                step("automation.yaml was missing — generated a template.")
            else:
                step("automation.yaml not found.")
                return PreparationResult(
                    ok=False,
                    project_type=project_type,
                    branch=sync.branch,
                    clone_status=CLONED,
                    steps=steps,
                    error="automation.yaml not found.",
                    needs_automation_yaml=True,
                    report=report,
                )

        # 6. Type-aware validation.
        step("Running validation...")
        validator = EnvironmentValidator(project_id)
        validation = validator.validate_pre_execution(
            device_id=device_id,
            platform=platform,
            project_type=project_type,
        )

        if not validation.passed:
            reasons = "; ".join(i.check for i in validation.issues)
            step(f"Validation FAILED: {reasons}")
            self._update_project(project_id, clone_status=CLONED)
            return PreparationResult(
                ok=False,
                project_type=project_type,
                branch=sync.branch,
                clone_status=CLONED,
                steps=steps,
                error=f"Validation failed: {reasons}",
                validation=validation,
                report=report,
            )

        for w in validation.warnings:
            step(f"WARNING: {w.check} — {w.problem}")

        # 7. Install dependencies.
        step("Installing dependencies...")
        js_stage = report.stage("js", "JS dependencies")
        decl = measure.declared_dependency_count(repo_path)
        if decl.get("dependencies") is not None:
            js_stage.metrics["Declared (deps)"] = decl["dependencies"]
            js_stage.metrics["Declared (dev)"] = decl["devDependencies"]

        # Was the tree already there? Needed to report a SKIPPED install honestly
        # rather than as a suspiciously fast one.
        _nm = os.path.join(repo_path, "node_modules")
        _nm_present_before = os.path.isdir(_nm) and bool(os.listdir(_nm))

        deps_ok, deps_err = self.install_dependencies(
            project_id, project_type, stage=js_stage)

        # A non-JS project never touches the JS stage; drop it rather than
        # leaving an empty "pending" row in the report.
        if js_stage.status == PENDING_STATUS:
            report.stages.remove(js_stage)
        elif _nm_present_before and js_stage.status == "completed":
            js_stage.notes.append(
                "node_modules was already present — the package manager "
                "reconciled it rather than installing from empty")

        # 7a. patch-package patches, applied because nothing else does.
        #
        # vya-consumer's preprod-2-May18 ships four patches but declares neither
        # patch-package nor a postinstall that runs it, so on a clean machine the
        # patches are inert: the Mac where the app "works" has a node_modules
        # patched by hand at some point, and a fresh clone silently gets unpatched
        # source. Every one of them edits node_modules only, so applying them here
        # changes nothing tracked.
        if project_type == ProjectType.REACT_NATIVE and deps_ok:
            for msg in self.apply_patch_package(repo_path):
                step(msg)
            # Old RN source that the current clang rejects — node_modules only.
            for msg in self.apply_toolchain_fixes(repo_path):
                step(msg)

        # 7b. Platform-injected dependencies: always reported, including "0".
        if project_type == ProjectType.REACT_NATIVE:
            plan = app_builder.platform_dependency_plan(repo_path)
            measure.report_platform_deps(
                report.stage("platform_deps", "Platform dependencies"),
                plan["injected"], plan["skipped"])
            for pkg in plan["injected"]:
                step(f"Platform dependency injected: {pkg}")
            for pkg in plan["skipped"]:
                step(f"Platform dependency skipped ({pkg}): no source import detected")

            # 7c. Patches. A patch that the platform's own injection has just
            # disabled is reported as such, not left for the reader to spot.
            patch_stage = report.stage("patches", "Patches")
            patch_stage._injection_conflicts = measure.patches_conflicting_with_injection(
                repo_path, plan["injected"])
            measure.measure_patches(patch_stage, repo_path)
            for msg in patch_stage._injection_conflicts:
                step(f"WARNING: {msg}")

        # A "successful" install can still carry a warning worth seeing (e.g. peer
        # conflicts bypassed) — surface it instead of hiding it behind success.
        if deps_ok and deps_err:
            for line in deps_err.splitlines():
                if line.strip():
                    step(line.rstrip())

        if not deps_ok:
            step("Dependency installation FAILED.")
            # Surface the tool's own output — a swallowed error is undebuggable.
            for line in (deps_err or "").splitlines()[-25:]:
                if line.strip():
                    step(line.rstrip())
            return PreparationResult(
                ok=False,
                project_type=project_type,
                branch=sync.branch,
                clone_status=CLONED,
                steps=steps,
                error=f"Dependency installation failed: {(deps_err or '').strip()[:300]}",
                validation=validation,
                report=report,
            )

        # 8. Build the app and install it on the target device.
        #    Without this there is nothing on the simulator to automate.
        if device_id and platform == "ios":
            # A stale/foreign UDID (e.g. copied from another Mac) would fail the
            # whole run at 'xcodebuild -destination id=…'. Swap it for a real sim.
            # Hint the resolver toward the app's intended device family.
            _n = (project_name or "").lower()
            prefer = None
            if any(k in _n for k in ("business", "ipad", "kitchen", "waiter", "merchant", "pos")):
                prefer = "iPad"
            elif any(k in _n for k in ("consumer", "diner", "customer", "iphone", "user")):
                prefer = "iPhone"
            resolved, note = app_builder.resolve_ios_device(device_id, prefer=prefer)
            if resolved is None:
                return PreparationResult(
                    ok=False, project_type=project_type, branch=sync.branch,
                    clone_status=CLONED, steps=steps,
                    error=f"No usable iOS simulator: {note}", validation=validation,
                    report=report,
                )
            if note:
                step(f"⚠ {note}")
            device_id = resolved
            # simctl install/launch need the sim booted (build alone doesn't).
            boot_ok, boot_msg = app_builder.ensure_ios_booted(device_id)
            step(boot_msg)
            if not boot_ok:
                return PreparationResult(
                    ok=False, project_type=project_type, branch=sync.branch,
                    clone_status=CLONED, steps=steps,
                    error=boot_msg, validation=validation,
                    report=report,
                )
        if device_id:
            build_ok, build_err = self._build_and_install(
                project_id, repo_path, platform, device_id, project_name, step,
                setup_report=report,
            )
            report.disk_free_after = measure.disk_free(repo_path)
            if not build_ok:
                return PreparationResult(
                    ok=False,
                    project_type=project_type,
                    branch=sync.branch,
                    clone_status=CLONED,
                    steps=steps,
                    error=f"App build/install failed: {(build_err or '')[:300]}",
                    validation=validation,
                    report=report,
                )
        else:
            step("No device selected — skipping app build/install.")

        if report.disk_free_after is None:
            report.disk_free_after = measure.disk_free(repo_path)

        # The whole measured report, into the step log. Plain text with no cursor
        # control, so the saved log reads exactly as the terminal did.
        for line in report.render().splitlines():
            step(line)

        step("Project ready for execution.")
        self._update_project(project_id, clone_status=READY)

        return PreparationResult(
            ok=True,
            project_type=project_type,
            branch=sync.branch,
            clone_status=READY,
            steps=steps,
            validation=validation,
            report=report,
        )

    def _build_and_install(
        self,
        project_id: str,
        repo_path: str,
        platform: str,
        device_id: str,
        project_name: str,
        step: Callable[[str], None],
        setup_report=None,
    ) -> "tuple[bool, Optional[str]]":
        """Build the app, install it on the device, launch it, and record the
        artifact path in automation.yaml as ``environment.app``."""
        # Preflight BEFORE xcodebuild. A build reports the last thing that broke,
        # and unrelated problems mask each other — a stale pod manifest, an
        # unapplied patch and an incompatible dependency all end as "xcodebuild
        # failed", so fixing whichever surfaced just promotes the next. This says
        # up front which layer a failure belongs to, and on a machine that differs
        # from a working one it names the difference.
        #
        # Read-only and non-fatal: it never blocks a build, because a check that
        # can veto is a check that gets bypassed. A genuine blocker still has to
        # come from the build itself.
        # Fix the machine itself first (Xcode first-launch, iOS platform,
        # CocoaPods, nvm) so a new Mac needs no hand-run commands. A no-op on a
        # ready machine.
        if platform == "ios":
            try:
                from automation.projects.machine_setup import auto_setup
                auto_setup(step)
            except Exception as e:
                logger.warning("machine setup failed to run: %s", e)

        if platform == "ios":
            try:
                from automation.projects.macos_environment import doctor
                report = doctor(repo_path)
                for check in report.failures + report.warnings:
                    step(f"[preflight] {check.status} {check.category}: "
                         f"{check.name} — {check.detail}")
                    if check.fix:
                        step(f"[preflight]   → {check.fix}")
                if report.ok and not report.warnings:
                    step("[preflight] environment and project checks passed.")
            except Exception as e:
                # A broken preflight must never be why a build does not happen.
                logger.warning("preflight failed to run: %s", e)

        # CocoaPods, measured. Runs the builder's own _pod_install — the same
        # call build_ios would make — so pod timing/size is attributable instead
        # of hidden inside the build stage. `pod install` only; never pod update.
        if platform == "ios" and setup_report is not None:
            # Pin RN-incompatible native modules BEFORE pods, not inside the build:
            # pods installed first are generated for the unpinned versions, and
            # the pin then swaps node_modules out from under them. build_ios runs
            # it again and finds nothing left to do.
            from automation.projects.macos_environment import ensure_nvm_default
            nvm_msg = ensure_nvm_default()
            if nvm_msg:
                step(nvm_msg)
            try:
                fixed_now = app_builder.fix_rn_compatibility(repo_path).get("fixed", [])
                for fixed in fixed_now:
                    step(f"React Native compatibility: {fixed}")
                if fixed_now:
                    # build_ios will find nothing left to fix and so no longer
                    # restarts Metro; a packager started against the old tree
                    # would keep serving the replaced modules. Same kill it did.
                    app_builder._kill_metro_for_repo(repo_path)
            except Exception as e:
                logger.warning("[%s] compatibility pinning failed early: %s",
                               project_id, e)
            pods_stage = setup_report.stage("pods", "CocoaPods")
            pods_ok = measure.measure_pods(
                pods_stage, repo_path, lambda: app_builder._pod_install(repo_path))
            for line in pods_stage.render():
                step(line)
            if not pods_ok:
                self._update_project(project_id, build_status="build_failed",
                                     build_error=pods_stage.error)
                return False, pods_stage.error

        step(f"Building the {platform} app (this can take several minutes)...")
        self._update_project(project_id, build_status="building", build_error=None)

        build_stage = (setup_report.stage("build", f"{platform} build").start()
                       if setup_report is not None else None)

        # Pass the target device so iOS builds only the arch we will install onto.
        # Build the variant this PROJECT is configured for, not whatever the repo's
        # own settings happen to say. Without this the bundle id was left to the
        # checkout -- so a project configured for a staging bundle silently produced
        # a production artifact (the staging difference lived in unpushed commits),
        # and the staging scenario then failed preflight as "…staging is not
        # installed". Resolution is config-driven; an unconfigured project simply
        # builds as before.
        env_config = self._env_config_for_project(project_id, project_name, step)
        result = app_builder.build(repo_path, platform, device_id=device_id,
                                   env_config=env_config)

        if result.skipped:
            if build_stage is not None:
                build_stage.skip("no native app to build for this platform")
            step("No native app to build for this platform — skipping.")
            return True, None

        if not result.ok:
            if build_stage is not None:
                from automation.projects.setup_report import classify_failure
                build_stage.fail(
                    (result.error or "")[-4000:],
                    classification=classify_failure(result.error or ""),
                    remedy="See the Xcode errors above; they name the failing target.")
            step("App build FAILED.")
            for line in (result.error or "").splitlines()[-20:]:
                if line.strip():
                    step(line.rstrip())
            self._update_project(
                project_id, build_status="build_failed", build_error=result.error
            )
            return False, result.error

        if build_stage is not None:
            app = result.artifact_path or ""
            build_stage.complete(**{
                "Artifact": os.path.basename(app) or measure.UNAVAILABLE,
                "Artifact size": measure.human_bytes(measure.dir_size(app)),
                "Bundle id": result.bundle_id or measure.UNAVAILABLE,
            })
        step(f"Build succeeded: {os.path.basename(result.artifact_path)}")
        # A DECLARED bundle id is the project's intent and must survive the build.
        # Overwriting it with whatever was produced is self-defeating for a variant:
        # a staging project whose build fell back to the production id would quietly
        # rewrite itself as a production project, and the next run would then look
        # correctly configured while driving the wrong app. The build's own identity
        # check has already failed the build if the artifact was the wrong variant,
        # so by here they agree -- this only protects the declaration.
        declared = getattr(self._load_project(project_id), "app_bundle_id", None)
        fields = dict(
            build_status="built",
            app_path=result.artifact_path,
            build_error=None,
            last_build_at=datetime.utcnow(),
        )
        if not declared:
            fields["app_bundle_id"] = result.bundle_id
        elif result.bundle_id and result.bundle_id != declared:
            step(f"⚠ built bundle id {result.bundle_id} differs from the project's "
                 f"declared {declared} — keeping the declared value")
        self._update_project(project_id, **fields)

        # Point automation.yaml at the artifact so Appium installs/launches it too.
        self._set_yaml_app_path(repo_path, result.artifact_path, step)

        # Remove the old copy FIRST. `simctl install` does not replace a container
        # that was built from a different derived-data path — it adds a second one
        # under the same bundle id, and `simctl launch`/`listapps` then resolve that
        # id to whichever container iOS picks, not the one just installed. That is
        # how a June build and a September build ended up installed side by side and
        # a run drove the stale one: a debug build carries no main.jsbundle, so the
        # wrong container against the wrong Metro renders an empty root view.
        if result.bundle_id:
            step(f"Removing any previously installed {result.bundle_id}...")
            _, un_out = app_builder.uninstall(device_id, result.bundle_id, platform)
            step(un_out)

        step(f"Installing the app on {device_id}...")
        ok, out = app_builder.install(device_id, result.artifact_path, platform)
        if not ok:
            step("App install FAILED.")
            for line in (out or "").splitlines()[-15:]:
                if line.strip():
                    step(line.rstrip())
            return False, out
        step("App installed.")

        # A Debug React Native build loads its JS from Metro at launch. Start it
        # BEFORE launching, or the app comes up on a red error screen and Appium
        # would attach to a dead app.
        # On the environment's port, with the app pointed at it: staging's Metro is
        # 8084, and an app without RCT_jsLocation asks 8081 -- where it got a
        # stale bundle from a build-spawned packager and died on launch.
        metro_ok, metro_msg = app_builder.ensure_metro(
            repo_path,
            port=getattr(env_config, "metro_port", None),
            udid=device_id if platform == "ios" else None,
            bundle_id=result.bundle_id)
        step(f"Metro: {metro_msg}")
        if not metro_ok:
            # Not fatal — a native (non-JS) app does not need Metro at all, and a
            # red screen is still diagnosable. Surface it loudly rather than
            # failing the whole run.
            step("WARNING: the app may show a red screen without the JS bundler.")

        # Launching makes the app visible in the live stream immediately.
        if result.bundle_id or platform == "android":
            launched, lout = app_builder.launch(device_id, result.bundle_id, platform)
            step("App launched." if launched else f"App installed but could not be launched: {lout[:200]}")

        return True, None

    def _set_yaml_app_path(
        self, repo_path: str, artifact_path: str, step: Callable[[str], None]
    ) -> None:
        """Write the built artifact path into automation.yaml -> environment.app."""
        import yaml as _yaml

        yaml_path = os.path.join(repo_path, "automation.yaml")
        try:
            with open(yaml_path) as f:
                data = _yaml.safe_load(f) or {}
            env = data.setdefault("environment", {})
            if env.get("app") == artifact_path:
                return
            env["app"] = artifact_path
            with open(yaml_path, "w") as f:
                _yaml.safe_dump(data, f, sort_keys=False)
            step("automation.yaml updated: environment.app -> built artifact.")
        except Exception as e:
            logger.warning(f"Could not write environment.app into automation.yaml: {e}")

    # ── Status (read-only, for the dashboard) ────────────────────────────────

    def get_status(self, project_id: str, check_remote: bool = False) -> Dict[str, Any]:
        """Return the current repository/health status for a project."""
        project = self._load_project(project_id)
        if not project:
            return {"error": "Project not found"}

        branch = project.default_branch or "main"
        cloned = repository_manager.is_cloned(project_id)

        if not cloned:
            clone_status = NOT_CLONED
        elif project.clone_status in (SYNCING, CLONE_FAILED):
            clone_status = project.clone_status
        elif check_remote and repository_manager.has_remote_updates(project_id, branch):
            clone_status = OUTDATED
        else:
            clone_status = project.clone_status or CLONED

        repo_path = repository_manager.get_repo_path(project_id)
        detection = detect_project_type(repo_path) if cloned else None

        import os

        return {
            "project_id": project_id,
            "clone_status": clone_status,
            "clone_error": project.clone_error,
            "cloned": cloned,
            "local_path": repo_path,
            "current_branch": repository_manager.get_current_branch(project_id),
            "default_branch": branch,
            "project_type": detection.project_type if detection else (project.project_type or ProjectType.UNKNOWN),
            "project_type_label": detection.label if detection else None,
            "platform": project.platform,
            "repo_type": project.repo_type,
            "has_automation_yaml": (
                os.path.exists(os.path.join(repo_path, "automation.yaml")) if cloned else False
            ),
            "last_pull_at": project.last_pull_at.isoformat() if project.last_pull_at else None,
            "last_execution_at": (
                project.last_execution_at.isoformat() if project.last_execution_at else None
            ),
            "health": repository_manager.get_health(project_id),
        }


preparation_service = ProjectPreparationService()


# ── Background preparation ───────────────────────────────────────────────────

# ── Progress phases ─────────────────────────────────────────────────────────
#
# Deliberately a percentage of WORK, not of time. The same project prepares in
# 20 seconds or 20 minutes depending on what is already cached -- an npm/yarn
# install and an Xcode build are each capable of dominating the run -- so any
# ETA would be invented. What CAN be reported honestly is which stage the
# pipeline has reached, and the UI can say that a slow stage is expected rather
# than looking hung.
#
# Matched on the step text the pipeline already emits, so no call site has to
# pass a number and the two cannot drift apart.
PHASES = [
    (0, "Starting"),
    (1, "Fetching the repository"),
    (2, "Reading project configuration"),
    (3, "Validating"),
    (4, "Installing dependencies"),
    (5, "Preparing the simulator"),
    (6, "Building the app"),
    (7, "Installing on the device"),
    (8, "Ready"),
]

# Stages that routinely take minutes: a cold dependency install and an Xcode
# build. The UI uses this to explain the wait instead of implying a stall.
_LONG_PHASES = {4, 6}

_PHASE_PATTERNS = [
    (1, ("cloning", "clone complete", "already cloned", "pulling", "pull complete",
         "checking out")),
    (2, ("detected project type", "automation.yaml")),
    (3, ("running validation", "warning:")),
    (4, ("installing dependencies", "dependency install")),
    (5, ("simulator", "booted")),
    (6, ("building the ios app", "building the android", "app build")),
    (7, ("app install", "app launched", "installed on")),
    (8, ("project ready for execution",)),
]


def _phase_for(msg: str):
    """The pipeline stage a step message belongs to, or None if it names none.

    Ordered most-specific-last so a message matching several buckets lands on
    the furthest one; progress only ever moves forward (the caller enforces it),
    because a late warning from an earlier stage must not drag the bar back.
    """
    low = msg.lower()
    found = None
    for phase, needles in _PHASE_PATTERNS:
        if any(n in low for n in needles):
            found = phase
    return found


class PreparationTracker:
    """Runs prepare_for_execution() off the request thread.

    A cold xcodebuild takes minutes. Doing that inside an HTTP handler holds a
    worker thread for the whole build and starves other requests — which is what
    made the execution agent's heartbeat/poll time out. The dashboard now starts
    a task and polls its status instead.
    """

    def __init__(self):
        self._tasks: Dict[str, Dict[str, Any]] = {}  # project_id -> state
        self._lock = threading.Lock()

    def is_running(self, project_id: str) -> bool:
        with self._lock:
            t = self._tasks.get(project_id)
            return bool(t and t["status"] == "running")

    def start(
        self,
        project_id: str,
        device_id: Optional[str] = None,
        generate_yaml: bool = False,
        branch: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            existing = self._tasks.get(project_id)
            if existing and existing["status"] == "running":
                return dict(existing)  # already preparing — reuse it

            task_id = str(uuid.uuid4())
            state: Dict[str, Any] = {
                "task_id": task_id,
                "project_id": project_id,
                "status": "running",
                "steps": [],
                "result": None,
                "phase": 0,
                "phase_label": PHASES[0][1],
                "started_at": time.time(),
                "started_phase_at": time.time(),
            }
            self._tasks[project_id] = state

        def on_step(msg: str) -> None:
            with self._lock:
                t = self._tasks[project_id]
                t["steps"].append(msg)
                phase = _phase_for(msg)
                if phase is not None and phase >= t["phase"]:
                    t["phase"] = phase
                    t["phase_label"] = PHASES[phase][1]
                    t["started_phase_at"] = time.time()

        def worker() -> None:
            try:
                result = preparation_service.prepare_for_execution(
                    project_id,
                    device_id=device_id,
                    auto_generate_yaml=generate_yaml,
                    on_step=on_step,
                    branch=branch,
                )
                payload = result.to_dict()
                with self._lock:
                    self._tasks[project_id].update(
                        status="completed" if result.ok else "failed",
                        result=payload,
                        steps=payload["steps"] or self._tasks[project_id]["steps"],
                    )
            except Exception as e:
                logger.exception(f"[{project_id}] Preparation task crashed")
                with self._lock:
                    self._tasks[project_id].update(
                        status="failed",
                        result={
                            "ok": False,
                            "error": str(e),
                            "steps": self._tasks[project_id]["steps"],
                            "needs_automation_yaml": False,
                            "validation": None,
                            "project_type": "unknown",
                            "branch": None,
                            "clone_status": "cloned",
                        },
                    )

        threading.Thread(
            target=worker, daemon=True, name=f"prepare-{project_id[:8]}"
        ).start()

        with self._lock:
            return dict(self._tasks[project_id])

    def status(self, project_id: str) -> Dict[str, Any]:
        with self._lock:
            t = self._tasks.get(project_id)
            if not t:
                return {"status": "idle", "steps": [], "result": None}
            done = t["status"] in ("completed", "failed")
            phase = len(PHASES) - 1 if t["status"] == "completed" else t["phase"]
            return {
                "task_id": t["task_id"],
                "project_id": project_id,
                "status": t["status"],
                "steps": list(t["steps"]),
                "result": t["result"],
                # Phase progress, NOT a time estimate. How long an install or an
                # Xcode build takes depends entirely on what is already cached --
                # seconds to twenty minutes for the same project -- so a
                # percentage of TIME would be invented. This is a percentage of
                # WORK: which of the pipeline's stages has been reached.
                "phase": phase,
                "phase_count": len(PHASES),
                "phase_label": (
                    "Done" if t["status"] == "completed"
                    else "Failed" if t["status"] == "failed"
                    else t["phase_label"]
                ),
                "percent": 100 if t["status"] == "completed"
                           else int(round(100 * phase / (len(PHASES) - 1))),
                "elapsed_seconds": int(time.time() - t["started_at"]),
                "phase_elapsed_seconds": (
                    0 if done else int(time.time() - t["started_phase_at"])
                ),
                # The stages that typically dominate the wall clock, so the UI can
                # say "this one is slow" instead of looking stalled.
                "phase_is_long": phase in _LONG_PHASES and not done,
            }


preparation_tracker = PreparationTracker()
