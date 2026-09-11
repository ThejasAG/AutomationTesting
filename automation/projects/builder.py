"""Application build & install.

Produces an installable artifact from a checked-out repository and puts it on
the target device/simulator, so a run actually has an app to drive:

    iOS      → xcodebuild (iphonesimulator)      → <Name>.app  → xcrun simctl install
    Android  → ./gradlew assembleDebug           → app-debug.apk → adb install -r

The resulting artifact path is written back into automation.yaml as
``environment.app`` so Appium can install/launch it too.

Nothing here is React-Native specific: an RN repo simply has its native project
under ios/ and android/, which is exactly what the discovery below looks for.
"""

import glob
import json
import logging
import os
import plistlib
import re
import stat
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# A cold xcodebuild of a large RN app genuinely takes this long.
BUILD_TIMEOUT = 3600
INSTALL_TIMEOUT = 300
NPM_INSTALL_TIMEOUT = 1800
REGISTRY_TIMEOUT = 15

# Curated downgrades for React Native versions whose ecosystem has moved on.
#
# These exist because the npm registry alone is NOT sufficient: most React Native
# native modules do not declare a `react-native` peerDependency range at all
# (of vyaconsumer's 40 native modules, exactly ONE does). A registry-driven
# resolver therefore cannot see that e.g. gesture-handler 2.32 uses a Yoga API
# that RN 0.68 does not have — the incompatibility is real but undeclared, and
# only shows up as a C++ compile error 20 minutes into the build.
#
# So: the registry pass handles the packages that are honest about their peer
# range, and this table handles the ones that are not.
RN_KNOWN_FIXES: Dict[str, Dict[str, str]] = {
    "0.68": {
        # 8.x targets RN 0.7x's YGNodeConstRef measure-func signature.
        "@react-native-community/datetimepicker": "6.7.5",
        # 3.x declares peerDependencies react-native >= 0.71.0.
        "react-native-simple-toast": "1.1.1",
        # 2.32 compiles against a Yoga API that RN 0.68 does not ship.
        "react-native-gesture-handler": "2.9.0",
        # 6.2+ imports `react-native-svg/css` (LocalSvg), which only exists in
        # react-native-svg >= 13.7. Vya pins react-native-svg 12.5.1, so the JS
        # bundle fails to resolve. 6.1.2 is the last release whose peer is
        # react-native-svg ^12.1.0. NOTE: this conflict is a peer on
        # react-native-svg, not on react-native, so the registry pass (which only
        # reads the `react-native` peer range) can't see it — hence a curated pin.
        "react-native-qrcode-svg": "6.1.2",
    },
}

# Packages the app's SOURCE imports but its package.json never declares.
#
# RN_KNOWN_FIXES above only corrects versions of dependencies that are already
# declared ("if pkg in deps"), so an undeclared import slips straight through it.
# Metro resolves statically, so one of these takes down the WHOLE bundle: the app
# then serves a valid-looking but truncated bundle with no AppRegistry in it, and
# the device shows a blank screen or "Module AppRegistry is not a registered
# callable module". Nothing in that symptom points at a missing package, which is
# why this costs hours to diagnose by hand.
RN_REQUIRED_DEPS: Dict[str, Dict[str, str]] = {
    "0.68": {
        # App/Utils/videoUploadTracker.js requires it. The require is lazy and
        # wrapped in try/except, which protects the RUNTIME but not Metro — static
        # resolution still fails and 500s the bundle.
        "react-native-compressor": "1.10.3",
    },
}

# Only these are worth a registry round-trip — a compatibility conflict with
# React Native can only come from a package that touches React Native.
_RN_PKG_RE = re.compile(r"^(@react-native|react-native-|@react-navigation)")


@dataclass
class BuildResult:
    ok: bool
    artifact_path: Optional[str] = None   # .app bundle or .apk
    bundle_id: Optional[str] = None       # iOS only — used to launch it
    error: Optional[str] = None
    skipped: bool = False                 # nothing to build for this project type


def _build_env() -> dict:
    """Environment for build tools.

    CocoaPods hard-crashes with `Unicode Normalization not appropriate for
    ASCII-8BIT` when it inherits a non-UTF-8 locale — which is exactly what a
    daemonised API/agent process passes down. Forcing UTF-8 here is required,
    not cosmetic.
    """
    env = os.environ.copy()
    # setdefault is not enough: a daemon often passes LANG through as an EMPTY
    # string rather than omitting it, and setdefault keeps the empty value.
    # Treat empty as unset for all three — LC_ALL alone is enough for Ruby, but
    # leaving the others blank makes this fragile to reorder later.
    for var in ("LC_ALL", "LANG", "LC_CTYPE"):
        if not env.get(var):
            env[var] = "en_US.UTF-8"
    return env


def _script_phase_context(out: str, limit: int = 25) -> List[str]:
    """The lines a failed run-script phase actually printed.

    Xcode reports the phase that failed but not why: the reason is whatever the
    script wrote to stdout before it exited, which sits far above the summary
    among everything else the build emitted. Anything that looks like a real
    diagnostic is pulled out -- a bare "warning:" is not one, and neither are
    the deployment-target lines that dominate these logs.
    """
    keep, noise = [], (
        "IPHONEOS_DEPLOYMENT_TARGET", "deployment target", "Run script build phase",
    )
    for line in out.splitlines():
        s = line.strip()
        if not s or any(n in s for n in noise):
            continue
        low = s.lower()
        # Substring, not prefix: the shell prefixes its own diagnostics
        # ("env: node: No such file or directory", "sh: line 3: ..."), so
        # anchoring at the start of the line misses exactly the messages that
        # explain a failed script phase.
        if (any(low.startswith(pfx) for pfx in
                ("error", "fatal", "traceback", "throw ", "env:", "sh:",
                 "/bin/sh:", "node:"))
                or any(n in low for n in
                       ("no such file", "not found", "permission denied",
                        "command failed", "cannot find", "is not defined",
                        "nonzero exit code", ": line "))):
            if s not in keep:
                keep.append(s)
    return keep[-limit:] if keep else ["(the script produced no recognisable "
                                       "diagnostic — run the build directly to "
                                       "see its full output)"]


def _summarize_xcode_errors(out: str) -> str:
    """Pull the real errors out of an xcodebuild log.

    xcodebuild emits hundreds of `warning:` lines (deployment targets, etc.).
    Returning the raw tail buries the one line that matters — the compiler
    error — under noise, which is exactly what makes a build failure
    undiagnosable from the dashboard.
    """
    errors, failed_cmds, script_fails = [], [], []
    for line in out.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Skip warnings explicitly: "warning: ... error:" is not an error, and
        # the deployment-target ones outnumber everything else by thousands.
        # Whichever label comes FIRST is the line's severity: xcodebuild puts
        # it right after the source location, so a warning that merely quotes
        # the word "error:" in its text is still a warning.
        w, e = stripped.find("warning:"), stripped.find("error:")
        if w != -1 and (e == -1 or w < e):
            continue
        if "error:" in stripped and stripped not in errors:
            errors.append(stripped)
        elif stripped.startswith("The following build commands failed"):
            failed_cmds.append(stripped)
        elif stripped.startswith("PhaseScriptExecution") and stripped not in script_fails:
            # A run-script phase (React Native's codegen, CocoaPods' own hooks)
            # fails with no compiler error at all -- the reason is in the
            # script's own output, and naming the phase is what lets anyone
            # find it.
            script_fails.append(stripped)

    if not errors and script_fails:
        return "\n".join([
            "A build script phase failed (no compiler error):",
            *[f"  {s}" for s in script_fails[:6]],
            "",
            "The reason is in that script's own output, above the summary:",
            *[f"  {ln}" for ln in _script_phase_context(out)],
        ])

    if not errors:
        # Nothing recognisable — the tail is still better than nothing, but say
        # so rather than presenting warnings as if they were the failure.
        tail = out.strip()[-1500:]
        return f"No compiler error found in the build log. Last output:\n{tail}"

    summary = ["Build errors:", *[f"  {e}" for e in errors[:12]]]
    if len(errors) > 12:
        summary.append(f"  … and {len(errors) - 12} more error(s).")
    summary.extend(failed_cmds)
    return "\n".join(summary)


def _run(cmd, cwd=None, timeout=BUILD_TIMEOUT) -> Tuple[bool, str]:
    try:
        res = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            env=_build_env(),
        )
        out = (res.stdout or "") + (res.stderr or "")
        if res.returncode != 0:
            # Keep the WHOLE log. Truncating here happens before any caller can
            # read it, and an Xcode build emits megabytes of `warning:` lines --
            # so a tail-slice reliably cuts every `error:` line and leaves the
            # deployment-target warnings that mean nothing. That is what made
            # build failures unreadable from the dashboard: the summariser was
            # searching a window the errors had already been cut out of.
            # Callers summarise; trimming for display is their job, not this
            # function's.
            return False, out.strip()
        return True, out
    except FileNotFoundError:
        return False, f"Command not found: {cmd[0]}. Ensure it is on the platform's PATH."
    except subprocess.TimeoutExpired:
        return False, f"{' '.join(cmd[:3])}… timed out after {timeout}s."
    except Exception as e:
        return False, str(e)


# ── Minimal semver, enough for npm peerDependency ranges ─────────────────────

def _parse_ver(v: str) -> Optional[Tuple[int, int, int]]:
    """'0.68.7' / 'v0.68.7-rc.1' -> (0, 68, 7). Pre-release suffixes are dropped."""
    if not v:
        return None
    m = re.match(r"[v=\s]*(\d+)\.(\d+)(?:\.(\d+))?", str(v).strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def _is_prerelease(v: str) -> bool:
    return bool(re.search(r"\d-(alpha|beta|rc|next|canary|dev)", str(v), re.I))


def _satisfies(version: str, spec: str) -> bool:
    """Does *version* satisfy an npm range *spec*?

    Supports the forms that actually appear in React Native peerDependencies:
    ``*``, ``>=x``, ``>x``, ``<=x``, ``<x``, ``^x``, ``~x``, exact, ``A || B``
    and space-separated conjunctions (``>=0.64.0 <0.72.0``).

    Anything it cannot parse returns True — an unparseable range must NOT be
    treated as a conflict, or we would "fix" packages that were never broken.
    """
    ver = _parse_ver(version)
    if ver is None or not spec:
        return True

    spec = spec.strip()
    if spec in ("*", "x", ""):
        return True

    # npm ranges are commonly written with a space after the operator
    # (">= 0.46", "> 0.50.0"). Splitting on whitespace without gluing the
    # operator back on turns ">= 0.46" into [">=", "0.46"], and the bare "0.46"
    # is then compared as an EXACT match — so every such range reads as a
    # conflict. That produced false positives and a 13-version "fix" for
    # react-native-maps. Normalise before tokenising.
    spec = re.sub(r"(>=|<=|>|<|=|\^|~)\s+", r"\1", spec)

    for clause in spec.split("||"):                       # OR
        if all(_satisfies_one(ver, tok) for tok in clause.split() if tok):
            return True
    return False


def _satisfies_one(ver: Tuple[int, int, int], token: str) -> bool:
    token = token.strip()
    if not token or token in ("*", "x"):
        return True

    m = re.match(r"^(>=|<=|>|<|=|\^|~)?\s*(.+)$", token)
    if not m:
        return True
    op, raw = m.group(1) or "=", m.group(2)

    target = _parse_ver(raw)
    if target is None:
        return True

    if op == ">=":
        return ver >= target
    if op == ">":
        return ver > target
    if op == "<=":
        return ver <= target
    if op == "<":
        return ver < target
    if op == "=":
        return ver[:2] == target[:2] if raw.count(".") < 2 else ver == target
    if op == "~":
        # ~0.68.1 := >=0.68.1 <0.69.0
        return ver >= target and ver[:2] == target[:2]
    if op == "^":
        # For 0.x, caret pins the MINOR: ^0.68.1 := >=0.68.1 <0.69.0.
        # Getting this wrong would make every 0.x range look satisfiable.
        if target[0] == 0:
            return ver >= target and ver[:2] == target[:2]
        return ver >= target and ver[0] == target[0]
    return True


class AppBuilder:
    """Builds a debug/simulator artifact and installs it on a device."""

    # ── React Native dependency compatibility ────────────────────────────────

    _registry_cache: Dict[str, Optional[dict]] = {}
    _registry_failures: int = 0
    _unresolved: List[Dict[str, str]] = []

    def _read_json(self, path: str) -> Optional[dict]:
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def detect_package_manager(self, repo_path: str) -> List[str]:
        """The command that must be used to install this project's dependencies.

        Getting this wrong silently corrupts the dependency tree. A Yarn Berry
        (v2+) lockfile is a different FORMAT — `__metadata: version: N` — and
        both npm and Yarn Classic simply cannot read it. They do not error: they
        discard it and re-resolve every caret range to the newest published
        version. That is how a project pinned to axios 1.7.7 / apisauce 3.1.0
        silently became axios 1.18.1 / apisauce 3.2.2, which then failed to
        bundle. The lockfile had the right answer the whole time.
        """
        lock = os.path.join(repo_path, "yarn.lock")
        if not os.path.exists(lock):
            return ["npm"]

        try:
            with open(lock, "r", errors="replace") as f:
                head = f.read(600)
        except OSError:
            return ["yarn"]

        # Berry lockfiles carry a __metadata block; Classic ones never do.
        if "__metadata:" in head:
            # corepack ships the right Yarn per project and is bundled with Node.
            # It is NOT enough on its own: with no "packageManager" field in
            # package.json, corepack falls back to whatever yarn is installed
            # globally. On a machine where that is Yarn 1, `corepack yarn` runs
            # Yarn 1, which cannot read this lockfile and silently rewrites it.
            # The version is therefore pinned explicitly from the lockfile.
            # No "--" separator: corepack passes the rest through already, and
            # Yarn 3 parses the separator as part of the command line, which
            # makes `-- install` register the install command twice and abort
            # with "Ambiguous Syntax Error: Cannot find which to pick".
            return ["corepack", f"yarn@{self._berry_version(head)}"]
        return ["yarn"]

    # Berry's lockfile cacheKey identifies the Yarn major that wrote it. Pinning
    # the exact minor is neither possible nor needed — any Yarn of the right
    # major reads the lockfile without rewriting it.
    _BERRY_CACHEKEY_TO_YARN = {"8": "3", "9": "4", "10": "4"}

    def _berry_version(self, lock_head: str) -> str:
        """The Yarn major that wrote this Berry lockfile, from its cacheKey.

        Running the wrong MAJOR is not cosmetic. Yarn 1 discards a Berry
        lockfile outright — that is how a project pinned to axios 1.7.7 became
        axios 1.20.0 and stopped bundling. Even Yarn 4 against a Yarn 3 lockfile
        rewrites the fsevents patch protocol (`#~builtin` -> `#optional!builtin`)
        and fails `--immutable`, which is a CI failure on a machine that did
        nothing wrong.

        Falls back to "stable" when the cacheKey is unknown — a newer Berry we
        have not seen is still enormously better than Yarn 1.
        """
        m = re.search(r"cacheKey:\s*(\d+)", lock_head)
        if not m:
            return "stable"
        return self._BERRY_CACHEKEY_TO_YARN.get(m.group(1), "stable")

    def _installed_version(self, repo_path: str, pkg: str) -> Optional[str]:
        data = self._read_json(
            os.path.join(repo_path, "node_modules", *pkg.split("/"), "package.json")
        )
        return data.get("version") if data else None

    def _rn_version(self, repo_path: str) -> Optional[str]:
        """The React Native version ACTUALLY installed.

        Read from node_modules, not package.json: a caret range in package.json
        ('^0.68.0') is a request, not a fact, and the fixer must reason about
        what is really on disk.
        """
        installed = self._installed_version(repo_path, "react-native")
        if installed:
            return installed
        pj = self._read_json(os.path.join(repo_path, "package.json")) or {}
        raw = (pj.get("dependencies") or {}).get("react-native")
        ver = _parse_ver(raw) if raw else None
        return ".".join(str(x) for x in ver) if ver else None

    @staticmethod
    def _ssl_context():
        """TLS context that can actually verify registry.npmjs.org.

        Python's urllib does NOT use the macOS system trust store, so every
        request fails with CERTIFICATE_VERIFY_FAILED unless a CA bundle is
        supplied. Without this the entire registry pass silently no-ops and only
        the hard-coded table works — the feature would look present and do
        nothing.
        """
        import ssl

        try:
            import certifi

            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            return ssl.create_default_context()

    def _registry(self, pkg: str) -> Optional[dict]:
        """Fetch a package document from the npm registry (cached)."""
        if pkg in self._registry_cache:
            return self._registry_cache[pkg]

        url = f"https://registry.npmjs.org/{urllib.parse.quote(pkg, safe='@')}"
        try:
            req = urllib.request.Request(
                url,
                headers={
                    # Abbreviated metadata: same peerDependencies, a fraction of
                    # the bytes. The full doc for a popular package is megabytes.
                    "Accept": "application/vnd.npm.install-v1+json, application/json",
                },
            )
            with urllib.request.urlopen(
                req, timeout=REGISTRY_TIMEOUT, context=self._ssl_context()
            ) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.warning(f"npm registry lookup failed for {pkg}: {e}")
            doc = None

        self._registry_cache[pkg] = doc
        return doc

    def _best_compatible(self, pkg: str, rn_version: str) -> Optional[str]:
        """Highest published version of *pkg* whose react-native peer range
        accepts *rn_version*.

        Returns None when the package declares no react-native peer range at
        all — in that case the registry simply has nothing to say, and we must
        NOT invent a "fix" from silence.
        """
        doc = self._registry(pkg)
        if not doc or "versions" not in doc:
            return None

        declares_peer = False
        candidates: List[Tuple[Tuple[int, int, int], str]] = []

        for ver, meta in doc["versions"].items():
            if _is_prerelease(ver):
                continue
            peer = (meta.get("peerDependencies") or {}).get("react-native")
            if not peer:
                continue
            declares_peer = True
            if _satisfies(rn_version, peer):
                parsed = _parse_ver(ver)
                if parsed:
                    candidates.append((parsed, ver))

        if not declares_peer or not candidates:
            return None
        return max(candidates)[1]

    # How far back a REGISTRY-derived downgrade may reach. Curated fixes in
    # RN_KNOWN_FIXES are exempt: those are human-verified.
    MAX_MINOR_DOWNGRADE = 6

    def _downgrade_is_sane(self, installed: str, target: str) -> bool:
        """Guard against a wild registry-derived downgrade.

        Peer metadata on old packages is patchy, so "highest version whose peer
        range accepts RN 0.68" can resolve to something ancient (react-native-maps
        0.31 -> 0.18). Installing that would break the app far more than the
        conflict it claims to fix.
        """
        cur, tgt = _parse_ver(installed), _parse_ver(target)
        if not cur or not tgt:
            return False
        if tgt >= cur:
            return True                       # not a downgrade at all
        if tgt[0] != cur[0]:
            return False                      # crossing a major boundary
        return (cur[1] - tgt[1]) <= self.MAX_MINOR_DOWNGRADE

    def _conflicting_packages(self, repo_path: str, rn_version: str) -> Dict[str, str]:
        """pkg -> target version, for every dependency incompatible with *rn_version*."""
        pj = self._read_json(os.path.join(repo_path, "package.json")) or {}
        deps = {**(pj.get("dependencies") or {}), **(pj.get("devDependencies") or {})}

        targets: Dict[str, str] = {}

        # 1. Curated fixes for this RN line — these catch the undeclared conflicts.
        rn_key = ".".join(str(x) for x in (_parse_ver(rn_version) or (0, 0, 0))[:2])
        for pkg, target in RN_KNOWN_FIXES.get(rn_key, {}).items():
            if pkg in deps:
                targets[pkg] = target

        # 1b. Imported-but-undeclared packages. Deliberately NOT gated on
        # "pkg in deps" — being absent from package.json is the whole failure.
        for pkg, target in RN_REQUIRED_DEPS.get(rn_key, {}).items():
            targets.setdefault(pkg, target)

        # 2. Registry pass for packages honest enough to declare a peer range.
        self._registry_failures = 0
        self._unresolved = []
        for pkg in deps:
            if pkg in targets or not _RN_PKG_RE.match(pkg):
                continue
            installed = self._installed_version(repo_path, pkg)
            if not installed:
                continue

            doc = self._registry(pkg)
            if not doc:
                self._registry_failures += 1
                continue
            meta = (doc.get("versions") or {}).get(installed) or {}
            peer = (meta.get("peerDependencies") or {}).get("react-native")
            if not peer or _satisfies(rn_version, peer):
                continue  # no declared conflict

            best = self._best_compatible(pkg, rn_version)
            if best and best != installed and self._downgrade_is_sane(installed, best):
                logger.info(
                    f"{pkg}@{installed} requires react-native {peer} "
                    f"(project has {rn_version}) -> downgrading to {best}"
                )
                targets[pkg] = best
            elif best and best != installed:
                # A huge registry-derived jump is far more likely to be bad peer
                # metadata than a real fix — auto-installing a 5-year-old release
                # would break the app far worse than the original conflict.
                # Surface it; a human adds a curated pin if it is genuinely right.
                self._unresolved.append({
                    "package": pkg,
                    "installed": installed,
                    "requires_react_native": peer,
                    "note": (
                        f"registry suggests {best}, which is too large a downgrade "
                        f"to apply automatically"
                    ),
                })
                logger.warning(
                    f"{pkg}: refusing to auto-downgrade {installed} -> {best} "
                    f"(too large a jump). Add a curated pin if this is correct."
                )
            elif not best:
                # A real, declared conflict with NO resolvable replacement — the
                # compatible older releases publish no peer range, so the registry
                # cannot prove any of them safe. Silently dropping this would let
                # the fixer report ready_to_build while the native build is still
                # guaranteed to fail. Record it so a human can add a curated fix.
                self._unresolved.append({
                    "package": pkg,
                    "installed": installed,
                    "requires_react_native": peer,
                })
                logger.warning(
                    f"{pkg}@{installed} requires react-native {peer} but the project "
                    f"has {rn_version}, and no compatible version could be resolved "
                    f"from the registry. Add it to RN_KNOWN_FIXES."
                )

        return targets

    def fix_rn_compatibility(self, project_path: str) -> Dict[str, Any]:
        """Align React Native native modules with the installed RN version.

        A React Native app whose package.json pins RN 0.68 but carries 2025-era
        native modules cannot compile: the modules build against a newer Yoga /
        TurboModule API. npm's own ERESOLVE error reports this honestly, but
        --legacy-peer-deps (which RN apps generally need) suppresses it, so the
        conflict resurfaces much later as an inscrutable C++ error.

        This downgrades the offending packages to the newest version that is
        actually compatible, then reinstalls Pods (their source lives in
        node_modules, so a dependency change invalidates them).

        SAFE: package.json / package-lock.json are TRACKED files, so every edit
        here lives only in the platform's local clone and is reverted by
        `git checkout -- .` on the next pull. Nothing is committed or pushed.
        """
        repo_path = os.path.abspath(project_path)
        result: Dict[str, Any] = {"fixed": [], "failed": [], "ready_to_build": True}

        if not os.path.exists(os.path.join(repo_path, "package.json")):
            result["skipped"] = "Not a JavaScript project."
            return result

        rn_version = self._rn_version(repo_path)
        if not rn_version:
            result["skipped"] = "Not a React Native project (react-native not found)."
            return result

        result["react_native_version"] = rn_version

        if not os.path.isdir(os.path.join(repo_path, "node_modules")):
            result["failed"].append("node_modules is missing — install dependencies first.")
            result["ready_to_build"] = False
            return result

        targets = self._conflicting_packages(repo_path, rn_version)

        # If the registry could not be reached, only the curated table ran. Say
        # so — a partial check must never look like a clean bill of health.
        if self._unresolved:
            # Declared conflicts we could not auto-fix. Surfacing these is the
            # difference between "no problems" and "problems I cannot solve".
            result["unresolved"] = self._unresolved

        if self._registry_failures:
            result["registry_unavailable"] = self._registry_failures
            logger.warning(
                f"npm registry unreachable for {self._registry_failures} package(s) — "
                f"only the curated RN {rn_version} fixes were applied."
            )

        # Idempotent: this runs before EVERY build, so skip anything already at
        # the target rather than reinstalling (and invalidating Pods) each time.
        pending = {
            pkg: ver for pkg, ver in targets.items()
            if self._installed_version(repo_path, pkg) != ver
        }
        result["already_compatible"] = sorted(set(targets) - set(pending))

        if not pending:
            logger.info(f"RN compatibility: nothing to fix for {repo_path}")
            return result

        # Install with the manager that owns the lockfile. Running `npm install`
        # in a Yarn project rewrites the tree from scratch and re-resolves every
        # caret range — undoing the pinning that makes the project work at all.
        pm = self.detect_package_manager(repo_path)
        result["package_manager"] = " ".join(pm)

        for pkg, ver in pending.items():
            spec = f"{pkg}@{ver}"
            logger.info(f"RN compatibility: installing {spec} via {' '.join(pm)}")

            if pm[0] == "npm":
                cmd = ["npm", "install", spec, "--legacy-peer-deps"]
            else:
                cmd = pm + ["add", spec]

            ok, out = _run(cmd, cwd=repo_path, timeout=NPM_INSTALL_TIMEOUT)
            if ok and self._installed_version(repo_path, pkg) == ver:
                result["fixed"].append(spec)
            else:
                # Report the tool's own words — a swallowed error here is what
                # made this class of failure undiagnosable in the first place.
                result["failed"].append(f"{spec}: {(out or '').strip()[-300:]}")
                result["ready_to_build"] = False

        # Pods compile RN module source out of node_modules, so anything we just
        # changed must be re-podded or xcodebuild silently builds the old code.
        if result["fixed"]:
            pods_ok, pods_out = self._pod_install(repo_path)
            result["pod_install"] = "ok" if pods_ok else pods_out[-300:]
            if not pods_ok:
                result["ready_to_build"] = False

        return result

    # ── iOS ──────────────────────────────────────────────────────────────────

    def _find_ios_project(self, repo_path: str) -> Optional[Tuple[str, str, bool]]:
        """Locate the Xcode project.

        Returns ``(path, scheme, is_workspace)``. A workspace wins over a bare
        project — a CocoaPods app MUST be built from its .xcworkspace or the Pod
        targets are missing. Searches the repo root and ios/ (React Native).
        """
        for base in (os.path.join(repo_path, "ios"), repo_path):
            if not os.path.isdir(base):
                continue
            for pattern, is_ws in (("*.xcworkspace", True), ("*.xcodeproj", False)):
                for match in sorted(glob.glob(os.path.join(base, pattern))):
                    # Xcode nests a project.xcworkspace inside every .xcodeproj.
                    if os.path.basename(os.path.dirname(match)).endswith(".xcodeproj"):
                        continue
                    scheme = os.path.basename(match).rsplit(".", 1)[0]
                    return match, scheme, is_ws
        return None

    # Podspecs shipped by older React Native versions that point at hosts which
    # no longer exist. The replacement must serve the BYTE-IDENTICAL artifact:
    # we deliberately leave each podspec's :sha256 untouched, so CocoaPods still
    # verifies the download and a wrong mirror would fail loudly rather than
    # silently substituting different source.
    #
    #   boost 1.76.0 — RN <= 0.68 fetches from boostorg.jfrog.io, which JFrog
    #   shut down. It now returns an HTML error page, so the checksum fails.
    #   archives.boost.io is boost's official archive host and serves the same
    #   tarball (verified: sha256 f0397ba6…, identical to the podspec's).
    _DEAD_PODSPEC_URLS = {
        "https://boostorg.jfrog.io/artifactory/main/release/1.76.0/source/boost_1_76_0.tar.bz2":
            "https://archives.boost.io/release/1.76.0/source/boost_1_76_0.tar.bz2",
    }

    #: Marks our inserted line so re-running is a no-op and a human reading the app's
    #: entry file knows what put it there and why.
    _LOGBOX_MARK = "// platform: LogBox silenced for automated runs"

    def _silence_logbox(self, repo_path: str) -> Optional[str]:
        """Stop the app's debug LogBox toasts covering its own controls.

        Measured 2026-09-02 on the Vya Business build: the collapsed toasts stack
        along the bottom and are drawn OVER real controls — 'addNewEvent' (y=723),
        the lower half of 'saveBtn' (y=685..735) on the iPad, and the whole time-slot
        row on the phone (slot y=730, toast y=726..774). Tapping any of them opens the
        LogBox VIEWER instead of the control, and the viewer's own Dismiss button is
        itself covered, so there is no way out. A booking could not be created by hand
        OR by automation until this was silenced.

        JS-only: it needs a Metro reload, not a rebuild. Idempotent, and only ever
        ADDS a line — it never edits or removes existing app code.

        NOTE this touches TRACKED source, unlike the node_modules patches above, so it
        will show as a modified file in the app repo. That is deliberate and visible:
        the real fix belongs in the app, and a silent change would hide the fact that
        the shipped build has controls users cannot tap either.
        """
        import re as _re
        entry = os.path.join(repo_path, "index.js")
        if not os.path.isfile(entry):
            return None
        try:
            with open(entry) as f:
                content = f.read()
        except OSError as e:
            logger.warning(f"Could not read {entry}: {e}")
            return None

        if self._LOGBOX_MARK in content or "ignoreAllLogs" in content:
            return None                                  # already silenced

        m = _re.search(r"^import\s*\{([^}]*)\}\s*from\s*['\"]react-native['\"];",
                       content, _re.M)
        if not m:
            return None                                  # not an entry we understand
        names = [n.strip() for n in m.group(1).split(",") if n.strip()]
        if "LogBox" not in names:
            names.append("LogBox")
        new_import = "import {" + ", ".join(names) + "} from 'react-native';"

        content = (content[:m.start()] + new_import
                   + f"\n\n{self._LOGBOX_MARK} — the toasts cover addNewEvent/saveBtn\n"
                   + "// and the time-slot row, and their own Dismiss button is covered too.\n"
                   + "LogBox.ignoreAllLogs(true);"
                   + content[m.end():])
        try:
            with open(entry, "w") as f:
                f.write(content)
        except OSError as e:
            logger.warning(f"Could not patch {entry}: {e}")
            return None
        msg = "Silenced LogBox in index.js (its toasts cover the app's own controls)"
        logger.info(msg)
        return msg

    def _patch_dead_podspec_urls(self, repo_path: str) -> Optional[str]:
        """Repoint podspecs whose upstream download host is gone.

        These live under node_modules/, which npm regenerates — so this is
        re-applied on every install. It is idempotent and confined to the local
        clone (node_modules is gitignored and never committed or pushed).
        """
        specs_dir = os.path.join(
            repo_path, "node_modules", "react-native", "third-party-podspecs"
        )
        if not os.path.isdir(specs_dir):
            return None

        patched = []
        for name in os.listdir(specs_dir):
            if not name.endswith(".podspec"):
                continue
            path = os.path.join(specs_dir, name)
            try:
                with open(path) as f:
                    content = f.read()
                original = content
                for dead, live in self._DEAD_PODSPEC_URLS.items():
                    if dead in content:
                        content = content.replace(dead, live)
                if content != original:
                    with open(path, "w") as f:
                        f.write(content)
                    patched.append(name)
            except OSError as e:
                logger.warning(f"Could not patch {path}: {e}")

        if patched:
            msg = f"Repointed dead download URLs in: {', '.join(patched)}"
            logger.info(msg)
            return msg
        return None

    @staticmethod
    def _declared_entry_points(meta: Dict[str, Any]) -> List[str]:
        """The files a package promises for its PRIMARY entry point.

        Deliberately narrow. The first version of this checked `main` and
        `module` only and missed the reported failure entirely: real axios has
        `main: "index.js"`, which is present, while the file Metro could not
        resolve (`dist/browser/axios.cjs`) appears only under `exports["."]`.

        The correction is not "check every path a manifest names". Checking all
        of `exports` flagged three healthy packages in this platform's own
        clones — react-day-picker ships no `./examples`, lucide-react no
        `./dist/cjs/dynamic.js` — because a secondary subpath only has to
        resolve if something actually imports it. Publishers routinely strip
        those from the tarball.

        So: `main`/`module`/`react-native`, plus the conditions under
        `exports["."]` — the paths that decide whether `require('the-package')`
        works at all, which is the thing a build cannot survive missing.
        """
        out: List[str] = []

        def walk(node):
            if isinstance(node, str):
                # `*` marks a subpath PATTERN ("./lib/*"), a template Node expands
                # per import rather than a file that must exist.
                if node.startswith("./") and "*" not in node:
                    out.append(node)
            elif isinstance(node, dict):
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        for key in ("main", "module", "react-native"):
            v = meta.get(key)
            if isinstance(v, str) and v:
                out.append(v)

        exports = meta.get("exports")
        if isinstance(exports, str):
            walk(exports)
        elif isinstance(exports, dict):
            # Only the root subpath. "." is the package itself; everything else
            # ("./styles.css", "./examples") is optional until someone imports it.
            walk(exports.get("."))

        return out

    def _resolves(self, pkg_dir: str, rel: str) -> bool:
        """Whether one declared path resolves the way a bundler would.

        Node and Metro both accept an extensionless path, a directory with an
        index, and a handful of extensions, so a literal isfile() would report
        false failures on packages that are perfectly fine.
        """
        target = os.path.normpath(os.path.join(pkg_dir, rel))
        if os.path.isfile(target):
            return True
        # Metro's own extension list, including the platform variants React
        # Native resolves ('js/ToolbarAndroid' is shipped only as
        # ToolbarAndroid.android.js / .ios.js). The reported error printed this
        # very list; missing them flagged healthy packages as broken.
        for ext in (".js", ".cjs", ".mjs", ".json", ".ts", ".tsx", ".node",
                    ".native.js", ".ios.js", ".android.js",
                    ".native.ts", ".ios.ts", ".android.ts",
                    ".native.tsx", ".ios.tsx", ".android.tsx",
                    ".native.json", ".ios.json", ".android.json"):
            if os.path.isfile(target + ext):
                return True
        if os.path.isdir(target):
            for idx in ("index.js", "index.cjs", "index.mjs", "index.json"):
                if os.path.isfile(os.path.join(target, idx)):
                    return True
            return True          # a directory with no index is still a real dir
        return False

    def _unusable_node_modules(self, repo_path: str) -> List[str]:
        """Dependencies declared in package.json that node_modules cannot resolve.

        `os.path.isdir("node_modules")` is not a readiness check. An install that
        was interrupted, or a tree copied between machines, leaves the directory
        present and each package's package.json in place while the files those
        manifests point at are missing. Metro then fails deep inside module
        resolution with an error that names one package but is really about the
        tree:

            the package `.../node_modules/axios/package.json` was successful ...
            this package itself specifies a `main` module field that could not
            be resolved (`.../node_modules/axios/dist/browser/axios.cjs`)
            Indeed, none of these files exist

        Note what that says: Metro calls it a "main module field", but on real
        axios `main` is index.js and the unresolvable path comes from `exports`.
        Every entry point a manifest declares is therefore checked, not just the
        one Metro's wording blames.

        Returns the names that fail, [] when the tree is sound.
        """
        pkg_json = self._read_json(os.path.join(repo_path, "package.json")) or {}
        declared = list((pkg_json.get("dependencies") or {}).keys())
        if not declared:
            return []

        broken: List[str] = []
        for name in declared:
            pkg_dir = os.path.join(repo_path, "node_modules", *name.split("/"))
            meta = self._read_json(os.path.join(pkg_dir, "package.json"))
            if meta is None:
                broken.append(name)
                continue
            # A package that declares no entry point at all is legitimate
            # (types-only, or index.js resolved by convention).
            missing = [e for e in self._declared_entry_points(meta)
                       if not self._resolves(pkg_dir, e)]
            if missing:
                logger.debug("%s cannot resolve: %s", name, ", ".join(missing[:3]))
                broken.append(name)
        return broken

    # Settings a later Yarn writes into .yarnrc.yml that an earlier one rejects
    # OUTRIGHT — "Usage Error: Unrecognized or legacy configuration settings
    # found" — before it resolves anything. Pinning the Yarn major cannot help
    # here: this fails earlier than resolution.
    _YARN4_ONLY_SETTINGS = (
        "approvedGitRepositories",
        "enableGlobalCache",
        "compressionLevel",
        "enableHardenedMode",
        "injectEnvironmentFiles",
    )

    def _yarnrc_poisoned(self, repo_path: str, want_major: str) -> List[str]:
        """Settings in .yarnrc.yml that the Yarn we are about to run will reject.

        A machine that once ran Yarn 4 in a repo leaves configuration behind
        that Yarn 3 refuses to start against, and .yarnrc.yml is typically
        UNTRACKED — so `git checkout` cannot restore it and the damage outlives
        every other repair. The raw Yarn error names the setting but not the
        cause, which reads like a corrupt project rather than a tool-version
        mismatch.
        """
        if want_major not in ("3", "stable"):
            return []                      # only older Yarn rejects newer keys
        rc = os.path.join(repo_path, ".yarnrc.yml")
        try:
            with open(rc, "r", errors="replace") as f:
                text = f.read(4000)
        except OSError:
            return []
        return [k for k in self._YARN4_ONLY_SETTINGS
                if re.search(rf"^\s*{k}\s*:", text, re.M)]

    def ensure_node_modules(self, repo_path: str) -> Tuple[bool, str]:
        """Make node_modules usable before anything tries to bundle from it.

        node_modules is gitignored, so a fresh clone on another machine never has
        one -- and an interrupted install leaves a worse state than none at all,
        because every existence check passes while resolution fails. Both are
        repaired the same way: install with the manager that owns the lockfile,
        then re-verify rather than trusting the exit code.

        Returns ``(ok, message)``. A project that is not a JS project, or whose
        tree is already sound, is a no-op.
        """
        if not os.path.isfile(os.path.join(repo_path, "package.json")):
            return True, "not a JavaScript project."

        present = os.path.isdir(os.path.join(repo_path, "node_modules"))
        broken = self._unusable_node_modules(repo_path) if present else []
        if present and not broken:
            return True, "node_modules is usable."

        why = ("node_modules is missing" if not present else
               f"node_modules cannot resolve {len(broken)} dependencies "
               f"({', '.join(sorted(broken)[:5])}"
               f"{', …' if len(broken) > 5 else ''})")
        pm = self.detect_package_manager(repo_path)

        # A .yarnrc.yml left behind by a NEWER Yarn stops an older one before it
        # resolves anything, so this is checked ahead of the install rather than
        # diagnosed from its output.
        want = pm[1].split("@")[1] if len(pm) > 1 and pm[1].startswith("yarn@") else ""
        bad = self._yarnrc_poisoned(repo_path, want) if want else []
        if bad:
            return False, (
                f"This project's .yarnrc.yml carries settings that Yarn {want} "
                f"refuses to start against ({', '.join(bad)}). A newer Yarn was "
                f"run here and rewrote the file; because .yarnrc.yml is usually "
                f"untracked, `git checkout` will not bring it back.\n\n"
                f"Remove those lines, leaving the linker Metro needs:\n"
                f"  cd {repo_path}\n"
                f"  printf 'nodeLinker: node-modules\\n' > .yarnrc.yml\n"
                f"  {' '.join(pm)} install\n\n"
                f"Committing .yarnrc.yml would make this visible in git status "
                f"instead of a mystery failure.")

        logger.info("%s — installing with %s in %s", why, " ".join(pm), repo_path)

        # A Berry lockfile that a wrong-major Yarn has rewritten is the failure
        # this whole path exists to prevent, so it is checked rather than
        # assumed. Yarn 1 leaves no error behind — it just re-resolves every
        # caret range and writes a v1 lockfile — so the only evidence is the
        # lockfile's own format changing under us.
        lock = os.path.join(repo_path, "yarn.lock")
        before = None
        if os.path.isfile(lock):
            try:
                with open(lock, "r", errors="replace") as f:
                    before = "__metadata:" in f.read(600)
            except OSError:
                pass

        # KNOWN LIMIT: a package manager that considers the tree already
        # installed will no-op here (yarn finished a 99-dependency project in
        # 4.4s on the machine this was reported from) and only re-run
        # postinstall. That WAS the repair in the reported case -- the tree had
        # been left mid-postinstall, and patch-package completing fixed it -- but
        # it is not a rebuild. A tree that needs one gets a clear failure from
        # the re-verify below rather than a silent bad build; escalating to
        # `rm -rf node_modules` is deliberately not done here, because destroying
        # a working tree on a wrong guess costs more than the failure does.
        # ponytail: install-only repair, add a forced rebuild if a stuck tree recurs
        cmd = pm + ["install"]
        ok, out = _run(cmd, cwd=repo_path, timeout=NPM_INSTALL_TIMEOUT)
        if not ok and pm[0] == "npm" and "ERESOLVE" in (out or ""):
            # The peer-dependency conflicts every mature RN app carries. This is
            # how the ecosystem actually installs them; preparation.py takes the
            # same fallback for the same reason.
            logger.warning("npm ERESOLVE — retrying with --legacy-peer-deps")
            ok, out = _run(cmd + ["--legacy-peer-deps"], cwd=repo_path,
                           timeout=NPM_INSTALL_TIMEOUT)

        # Did the install just destroy a Berry lockfile? That means the wrong
        # Yarn major ran, every pinned version has been re-resolved, and the tree
        # is now WRONG rather than merely incomplete — a state no amount of
        # re-installing fixes, and one the caller must be told about plainly.
        if before and os.path.isfile(lock):
            try:
                with open(lock, "r", errors="replace") as f:
                    if "__metadata:" not in f.read(600):
                        return False, (
                            f"`{' '.join(cmd)}` rewrote this project's Yarn Berry "
                            f"lockfile into Yarn 1 format, discarding every pinned "
                            f"version. The tree is now resolved from scratch and "
                            f"will not match the one this project was built "
                            f"against.\n\nRestore it and install with the right "
                            f"Yarn:\n"
                            f"  cd {repo_path}\n"
                            f"  git checkout -- yarn.lock\n"
                            f"  corepack enable\n"
                            f"  {' '.join(cmd)}\n\n"
                            f"Add a \"packageManager\" field to this project's "
                            f"package.json to stop it recurring.")
            except OSError:
                pass

        # Never trust the exit code alone: a install can report success and still
        # leave the tree unresolvable, which is the failure this exists to catch.
        still_broken = self._unusable_node_modules(repo_path)
        if not os.path.isdir(os.path.join(repo_path, "node_modules")) or still_broken:
            detail = (f"still cannot resolve: {', '.join(sorted(still_broken)[:8])}"
                      if still_broken else "node_modules was not created")
            return False, (
                f"Dependency install did not produce a usable node_modules — {detail}. "
                f"Run `{' '.join(cmd)}` in {repo_path} and check its output.\n"
                f"{(out or '').strip()[-600:]}")
        return True, f"Installed dependencies ({' '.join(pm)})."

    def _ensure_ios_jsbundle(self, repo_path: str) -> Tuple[bool, str]:
        """Generate ios/main.jsbundle if the Xcode project needs it but it's absent.

        Some RN apps commit a reference to main.jsbundle in Copy Bundle Resources
        (to embed the JS for offline/Release). The file itself is generated, not
        committed — and git clean removes it — so a fresh build fails at
        'CpResource main.jsbundle' before any of our code runs. Regenerate it.

        No-op when the project does not reference the bundle (a normal Debug app
        loads JS from Metro and needs no embedded bundle).
        """
        ios_dir = os.path.join(repo_path, "ios")
        proj = self._find_ios_project(repo_path)
        pbxprojs = glob.glob(os.path.join(ios_dir, "*.xcodeproj", "project.pbxproj"))

        references = any(
            "main.jsbundle" in open(p, encoding="utf-8", errors="ignore").read()
            for p in pbxprojs
        )
        bundle_path = os.path.join(ios_dir, "main.jsbundle")
        if not references or os.path.isfile(bundle_path):
            return True, "jsbundle not required."

        # Metro bundles OUT OF node_modules, so it must be usable before we start.
        # Without this the bundler fails inside module resolution and reports the
        # first package it could not follow, which reads as a problem with that
        # package rather than with the tree.
        deps_ok, deps_msg = self.ensure_node_modules(repo_path)
        if not deps_ok:
            return False, deps_msg

        entry = next(
            (e for e in ("index.js", "index.ts", "index.tsx")
             if os.path.isfile(os.path.join(repo_path, e))),
            "index.js",
        )
        logger.info(f"Generating ios/main.jsbundle (entry={entry}) — project embeds it")
        ok, out = _run(
            ["npx", "react-native", "bundle",
             "--platform", "ios",
             "--dev", "true",
             "--entry-file", entry,
             "--bundle-output", bundle_path,
             "--assets-dest", ios_dir],
            cwd=repo_path,
            timeout=900,
        )
        if not ok or not os.path.isfile(bundle_path):
            return False, f"Failed to generate ios/main.jsbundle:\n{(out or '')[-1000:]}"
        return True, "Generated ios/main.jsbundle."

    def _manifest_out_of_sync(self, pod_dir: str) -> bool:
        """True when Podfile.lock != Pods/Manifest.lock.

        Xcode's '[CP] Check Pods Manifest.lock' build phase fails HARD when these
        two differ. They diverge when a git reset restores the committed
        Podfile.lock over a newer `pod install` (which had rewritten both) — e.g.
        the committed lock predates a native module like react-native-blur that
        node_modules actually pulls in. node_modules-mtime staleness misses this
        completely, so without mirroring Xcode's own check the build fails with no
        auto-recovery. pod install rewrites both files and re-syncs them.
        """
        podfile_lock = os.path.join(pod_dir, "Podfile.lock")
        manifest = os.path.join(pod_dir, "Pods", "Manifest.lock")
        if not (os.path.isfile(podfile_lock) and os.path.isfile(manifest)):
            return False
        try:
            with open(podfile_lock) as a, open(manifest) as b:
                return a.read() != b.read()
        except OSError:
            return False

    def _pods_are_stale(self, repo_path: str, pod_dir: str) -> bool:
        """True when node_modules is newer than the installed Pods.

        React Native pods compile source out of node_modules, so an npm install
        (or a dependency downgrade) invalidates them. Without this check the
        build would happily reuse Pods generated against the OLD dependency
        versions and produce an app that does not match package.json.
        """
        manifest = os.path.join(pod_dir, "Pods", "Manifest.lock")
        node_modules = os.path.join(repo_path, "node_modules")

        if not os.path.exists(manifest) or not os.path.isdir(node_modules):
            return False
        try:
            return os.path.getmtime(node_modules) > os.path.getmtime(manifest)
        except OSError:
            return False

    def _verify_pods(self, pod_dir: str, out: str) -> str:
        """Checks that run after a SUCCESSFUL pod install.

        CocoaPods exiting 0 does not mean the Podfile's post_install hooks did
        what they were written to do -- they are shelled-out commands whose
        failures it does not surface. Each check here is a case where that gap
        turned into a build error thousands of lines later, naming a dependency
        instead of the hook.
        """
        if self._folly_clockid_conflict(pod_dir):
            if self._repair_folly_clockid(pod_dir):
                out += ("\n[platform] RCT-Folly Time.h was left unpatched by the "
                        "Podfile's post_install hook and has been repaired: its "
                        "clockid_t typedef conflicts with the iOS 26 SDK.")
            else:
                out += ("\n[platform] WARNING: RCT-Folly Time.h still declares "
                        "clockid_t, which the iOS 26 SDK also defines. The build "
                        "will fail compiling NetOps.cpp. The Podfile's "
                        "post_install hook did not apply and could not be "
                        "repaired automatically.")
        return out

    def _folly_clockid_conflict(self, pod_dir: str) -> bool:
        """True when RCT-Folly still declares its own clockid_t.

        RCT-Folly 2021.06.28 (React Native 0.68) carries

            #if !FOLLY_HAVE_CLOCK_GETTIME && (defined(__MACH__) || defined(_WIN32))
            typedef uint8_t clockid_t;

        and the iOS 26 SDK now defines clockid_t as an enum, so compiling
        NetOps.cpp fails with "typedef redefinition with different types".

        Projects of this era solve it in their own Podfile post_install, which
        rewrites the guard above so TARGET_OS_IPHONE sets
        FOLLY_HAVE_CLOCK_GETTIME and the typedef is never reached. That hook is
        a shelled-out `sed`, and CocoaPods reports success whether or not it
        did anything — so a header that silently went unpatched surfaces
        thousands of lines later as a compile error that names folly rather
        than the hook that was supposed to fix it.
        """
        header = os.path.join(pod_dir, "Pods", "RCT-Folly", "folly",
                              "portability", "Time.h")
        try:
            with open(header, "r", errors="replace") as f:
                text = f.read()
        except OSError:
            return False                     # not an RN 0.68-era project
        if "typedef uint8_t clockid_t" not in text:
            return False                     # newer folly: nothing to guard
        # Patched headers force the macro on for iOS, which makes the guard
        # around the typedef false.
        return not re.search(r"TARGET_OS_IPHONE", text)

    def _repair_folly_clockid(self, pod_dir: str) -> bool:
        """Apply the guard the project's own post_install hook meant to apply.

        Not a new workaround: RN 0.68-era Podfiles already carry

            sed -i -e $'s/__IPHONE_13_0/__IPHONE_14_0/' .../folly/portability/Time.h

        whose effect is to leave `(TARGET_OS_IPHONE)` in the condition that sets
        FOLLY_HAVE_CLOCK_GETTIME, so folly's own clockid_t typedef is skipped on
        iOS. The hook is a shelled-out sed and fails silently -- a different
        shell, a read-only Pods tree, a BSD/GNU sed difference -- and CocoaPods
        reports success regardless. This re-applies the same edit to the same
        line, and is a no-op when the hook worked.

        Pods/ is generated, gitignored and rewritten by every `pod install`, so
        nothing tracked is touched.
        """
        header = os.path.join(pod_dir, "Pods", "RCT-Folly", "folly",
                              "portability", "Time.h")
        try:
            with open(header, "r", errors="replace") as f:
                text = f.read()
        except OSError:
            return False

        # The project's sed: __IPHONE_13_0 -> __IPHONE_14_0.
        patched = text.replace("__IPHONE_13_0", "__IPHONE_14_0")

        # Some copies differ enough that the rename alone leaves the typedef
        # reachable. Force the macro directly in that case -- same outcome, and
        # it is what the rename exists to achieve.
        if "TARGET_OS_IPHONE" not in patched:
            patched = patched.replace(
                "#if !FOLLY_HAVE_CLOCK_GETTIME && (defined(__MACH__) || defined(_WIN32))",
                "#if !FOLLY_HAVE_CLOCK_GETTIME && !TARGET_OS_IPHONE && "
                "(defined(__MACH__) || defined(_WIN32))",
                1)

        if patched == text:
            return False
        try:
            # CocoaPods installs pod sources read-only (0444), so writing needs
            # the mode restored first -- and put back afterwards, because a
            # later `pod install` compares what it finds against its manifest.
            mode = os.stat(header).st_mode
            os.chmod(header, mode | stat.S_IWUSR)
            try:
                with open(header, "w") as f:
                    f.write(patched)
            finally:
                os.chmod(header, mode)
        except OSError as e:
            logger.warning("could not patch folly Time.h: %s", e)
            return False
        logger.info("Patched RCT-Folly Time.h: clockid_t conflicts with the iOS "
                    "26 SDK and the Podfile's post_install had not applied")
        return True

    def _pod_install(self, repo_path: str) -> Tuple[bool, str]:
        """Run `pod install` in whichever directory actually holds the Podfile.

        For React Native this is ios/, NOT the repository root.
        """
        repo_path = os.path.abspath(repo_path)
        for candidate in (os.path.join(repo_path, "ios"), repo_path):
            if not os.path.exists(os.path.join(candidate, "Podfile")):
                continue

            # A *failed* install still leaves a partial Pods/ directory behind, so
            # the directory alone is not proof of success — Pods.xcodeproj is what
            # xcodebuild actually needs. Checking only isdir() made a broken
            # install look complete and pushed the failure into xcodebuild.
            #
            # Pods are also only reusable while they are NEWER than node_modules:
            # a React Native pod's source lives under node_modules, so any
            # dependency change invalidates them. Reusing stale Pods after an
            # npm install silently compiles the previous versions.
            pods_proj = os.path.join(candidate, "Pods", "Pods.xcodeproj")
            if (
                os.path.isdir(pods_proj)
                and not self._pods_are_stale(repo_path, candidate)
                and not self._manifest_out_of_sync(candidate)
            ):
                # Still verify. The folly/clockid_t conflict lives in the
                # INSTALLED pod source, so a tree that is "already installed"
                # is exactly the one that carries it -- skipping the check here
                # meant it only ever ran on a first install, never on the
                # rebuild where the broken header was already sitting on disk.
                return True, self._verify_pods(candidate, "Pods already installed.")

            # Older React Native versions ship podspecs pointing at dead hosts;
            # CocoaPods would download an HTML error page and fail the checksum.
            self._patch_dead_podspec_urls(repo_path)
            self._silence_logbox(repo_path)

            logger.info(f"Running pod install in {candidate}")
            ok, out = _run(["pod", "install"], cwd=candidate, timeout=1800)
            if ok:
                return True, self._verify_pods(candidate, out)

            # A stale local spec repo (or a Podfile whose constraints moved) makes
            # a plain `pod install` fail — CocoaPods itself tells you to retry with
            # --repo-update. Do it automatically instead of dead-ending the build.
            if "repo update" in out or "out-of-date source repos" in out:
                logger.warning("pod install failed on stale specs — retrying with --repo-update")
                ok, out = _run(
                    ["pod", "install", "--repo-update"], cwd=candidate, timeout=2700
                )
                if ok:
                    return True, self._verify_pods(candidate, out)

            # A Podfile.lock that has drifted from the Podfile makes CocoaPods
            # demand `pod update <pod>` — and fixing one pod just surfaces the
            # next, so chasing them individually never converges. Regenerate the
            # lock instead.
            #
            # SAFE: Podfile.lock is a *tracked* file, so this edit lives only in
            # the platform's local clone and is reverted by `git checkout -- .`
            # on the next pull. Nothing is ever committed or pushed.
            if "pod update" in out or "changed the constraints" in out:
                lock = os.path.join(candidate, "Podfile.lock")
                logger.warning(
                    "Podfile.lock has drifted from the Podfile — regenerating it "
                    "locally (this is never committed or pushed)."
                )
                try:
                    if os.path.exists(lock):
                        os.remove(lock)
                except OSError as e:
                    return False, f"Could not remove stale Podfile.lock: {e}"

                ok, out = _run(
                    ["pod", "install", "--repo-update"], cwd=candidate, timeout=2700
                )
                return ok, out

            return False, out

        return True, "No Podfile — CocoaPods not used."

    def build_ios(
        self, repo_path: str, force: bool = False, device_id: Optional[str] = None
    ) -> BuildResult:
        """Build a simulator .app via xcodebuild.

        *device_id* is the target simulator's UDID. When supplied we build for
        that destination and the active architecture only. Building the
        `generic/platform=iOS Simulator` destination instead forces xcodebuild to
        produce *every* simulator slice — including x86_64, which is useless on
        Apple Silicon, doubles the build, and is the slice where older React
        Native dependencies tend to fail to compile.
        """
        # xcodebuild runs with cwd=repo_path, so a repo-relative project path
        # would be resolved *twice* and not be found. Always work in absolutes.
        repo_path = os.path.abspath(repo_path)

        found = self._find_ios_project(repo_path)
        if not found:
            return BuildResult(
                ok=False,
                error="No .xcworkspace or .xcodeproj found in the repository root or ios/.",
            )
        proj_path, scheme, is_workspace = found

        # Align React Native native modules with the installed RN version BEFORE
        # anything is compiled. A module built against a newer Yoga/TurboModule
        # API fails deep inside clang after ~20 minutes; catching it here turns
        # that into a fast, named fix. No-op for non-RN projects, and idempotent
        # (skips packages already at the compatible version).
        compat = self.fix_rn_compatibility(repo_path)
        if compat.get("fixed"):
            logger.info(f"RN compatibility fixes applied: {compat['fixed']}")
            # The on-disk fix is invisible to a Metro that is already running against
            # the old module graph — kill it so ensure_metro cold-starts fresh and the
            # app no longer red-boxes the module the fix just removed.
            self._kill_metro_for_repo(repo_path)
        if not compat.get("ready_to_build", True):
            return BuildResult(
                ok=False,
                error=(
                    "React Native dependency compatibility could not be resolved:\n  "
                    + "\n  ".join(compat.get("failed", []))
                ),
            )

        # CocoaPods must be resolved before the workspace will build.
        ok, out = self._pod_install(repo_path)
        if not ok:
            return BuildResult(ok=False, error=f"pod install failed:\n{out[-1200:]}")

        # Some RN apps list ios/main.jsbundle in Copy Bundle Resources (an embedded
        # offline bundle). If the project references it but the file is absent, the
        # 'CpResource main.jsbundle' phase fails the whole build. Generate it.
        ok, msg = self._ensure_ios_jsbundle(repo_path)
        if not ok:
            return BuildResult(ok=False, error=msg)

        derived = os.path.join(repo_path, "build", "ios")
        head_file = os.path.join(derived, ".built_head")
        cur_head = self._git_head(repo_path)

        # Reuse a previous build ONLY if the checkout hasn't changed since it was
        # built. Reusing blindly (the old behaviour) silently shipped the stale
        # binary after a `git pull` or a code edit — a re-prepare looked done but
        # ran the previous version. Comparing the built commit to HEAD fixes that.
        if not force:
            existing = self._find_built_app(derived)
            built_head = None
            try:
                if os.path.exists(head_file):
                    built_head = open(head_file).read().strip()
            except Exception:
                pass
            if existing and cur_head and built_head == cur_head:
                logger.info(f"Reusing iOS build (HEAD unchanged @ {cur_head[:8]})")
                return BuildResult(
                    ok=True, artifact_path=existing, bundle_id=self._bundle_id(existing)
                )
            if existing:
                logger.info(f"Rebuilding: checkout changed since last build "
                            f"(built {str(built_head)[:8]} → now {str(cur_head)[:8]})")

        flag = "-workspace" if is_workspace else "-project"

        # Build for the exact simulator we are going to install onto, and only
        # for its architecture. The `generic/platform=iOS Simulator` destination
        # builds every slice, which is slower and compiles archs we will never
        # install.
        if device_id:
            destination = f"id={device_id}"
            only_active_arch = "ONLY_ACTIVE_ARCH=YES"
        else:
            destination = "generic/platform=iOS Simulator"
            only_active_arch = "ONLY_ACTIVE_ARCH=NO"

        cmd = [
            "xcodebuild",
            flag, proj_path,
            "-scheme", scheme,
            "-configuration", "Debug",
            "-sdk", "iphonesimulator",
            "-derivedDataPath", derived,
            "-destination", destination,
            only_active_arch,
            # Simulator builds are never signed; without this a repo configured
            # for a real signing identity fails on a machine without the certs.
            "CODE_SIGNING_ALLOWED=NO",
            "CODE_SIGNING_REQUIRED=NO",
            # boost (vendored by React Native <= 0.68 via RCT-Folly) uses
            # std::unary_function, which C++17 removed and Apple's libc++ has now
            # dropped. Without this the build cannot get past boost on a modern
            # Xcode. It only restores removed *declarations*; it changes no
            # behaviour for code that does not use them.
            "OTHER_CPLUSPLUSFLAGS=$(inherited) -D_LIBCPP_ENABLE_CXX17_REMOVED_UNARY_BINARY_FUNCTION",
            "build",
        ]
        logger.info(f"Building iOS: {' '.join(cmd)}")
        ok, out = _run(cmd, cwd=repo_path)
        if not ok:
            return BuildResult(
                ok=False, error=f"xcodebuild failed.\n{_summarize_xcode_errors(out)}"
            )

        app = self._find_built_app(derived)
        if not app:
            return BuildResult(
                ok=False,
                error="xcodebuild reported success but no .app was produced under "
                      f"{derived}/Build/Products/Debug-iphonesimulator.",
            )
        # Record the commit this build was made from, so the next prepare can tell
        # whether a rebuild is needed (see the reuse check above).
        try:
            if cur_head:
                with open(head_file, "w") as f:
                    f.write(cur_head)
        except Exception:
            pass
        return BuildResult(ok=True, artifact_path=app, bundle_id=self._bundle_id(app))

    def _git_head(self, repo_path: str) -> Optional[str]:
        """Current git commit SHA of the checkout, or None if not a git repo."""
        try:
            import subprocess
            r = subprocess.run(["git", "-C", repo_path, "rev-parse", "HEAD"],
                               capture_output=True, text=True, timeout=10)
            return (r.stdout or "").strip() or None
        except Exception:
            return None

    def _is_complete_app(self, app: str) -> bool:
        """True only if *app* is installable — has an Info.plist with a bundle id
        AND its executable.

        An interrupted build (e.g. the agent restarted mid-compile) leaves an
        EMPTY `.app` directory behind: the Pods all built, but the app target
        never populated its bundle. Treating that as a finished build makes every
        later run 'reuse' it, report success, then fail to install with
        'Missing bundle ID'. So an incomplete bundle must count as no build.
        """
        try:
            with open(os.path.join(app, "Info.plist"), "rb") as f:
                info = plistlib.load(f)
        except Exception:
            return False
        bundle_id = info.get("CFBundleIdentifier")
        executable = info.get("CFBundleExecutable")
        if not bundle_id or not executable:
            return False
        return os.path.exists(os.path.join(app, executable))

    def _find_built_app(self, derived: str) -> Optional[str]:
        pattern = os.path.join(derived, "Build", "Products", "Debug-iphonesimulator", "*.app")
        complete = [m for m in sorted(glob.glob(pattern)) if self._is_complete_app(m)]
        return complete[0] if complete else None

    def _bundle_id(self, app_path: str) -> Optional[str]:
        """Read CFBundleIdentifier so the app can be launched after install."""
        try:
            with open(os.path.join(app_path, "Info.plist"), "rb") as f:
                return plistlib.load(f).get("CFBundleIdentifier")
        except Exception as e:
            logger.warning(f"Could not read bundle id from {app_path}: {e}")
            return None

    # ── Android ──────────────────────────────────────────────────────────────

    def build_android(self, repo_path: str, force: bool = False) -> BuildResult:
        """Build a debug APK via the Gradle wrapper."""
        repo_path = os.path.abspath(repo_path)

        # React Native keeps the Gradle project under android/.
        base = repo_path
        if os.path.exists(os.path.join(repo_path, "android", "gradlew")):
            base = os.path.join(repo_path, "android")
        elif not os.path.exists(os.path.join(repo_path, "gradlew")):
            return BuildResult(ok=False, error="No Gradle wrapper (gradlew) found.")

        if not force:
            existing = self._find_built_apk(base)
            if existing:
                logger.info(f"Reusing existing APK: {existing}")
                return BuildResult(ok=True, artifact_path=existing)

        gradlew = os.path.join(base, "gradlew")
        os.chmod(gradlew, 0o755)  # git does not always preserve the exec bit

        logger.info(f"Building Android: {gradlew} assembleDebug")
        ok, out = _run([gradlew, "assembleDebug"], cwd=base)
        if not ok:
            return BuildResult(
                ok=False,
                error=f"gradlew assembleDebug failed.\n{_summarize_xcode_errors(out)}",
            )

        apk = self._find_built_apk(base)
        if not apk:
            return BuildResult(ok=False, error="Gradle succeeded but no debug APK was found.")
        return BuildResult(ok=True, artifact_path=apk)

    def _find_built_apk(self, base: str) -> Optional[str]:
        matches = sorted(glob.glob(
            os.path.join(base, "app", "build", "outputs", "apk", "debug", "*.apk")
        ))
        return matches[0] if matches else None

    # ── Entry point ──────────────────────────────────────────────────────────

    def build(
        self,
        repo_path: str,
        platform: str,
        force: bool = False,
        device_id: Optional[str] = None,
    ) -> BuildResult:
        """Build the app for *platform* ("ios" | "android")."""
        if platform == "ios":
            return self.build_ios(repo_path, force, device_id=device_id)
        if platform == "android":
            return self.build_android(repo_path, force)
        return BuildResult(ok=True, skipped=True)

    # ── Metro (React Native JS bundler) ──────────────────────────────────────

    METRO_PORT = 8081

    # Apps that run their OWN Metro on a non-default port — they can't share 8081,
    # which serves the Consumer bundle. Each is pointed at its packager via the
    # RCT_jsLocation user-default. Extend as more RN apps are onboarded.
    _APP_METRO_PORTS = {
        "org.vyapy.sarls.vyabusinessipad": 8082,          # Business app (iPad, prod)
        "org.vyapy.sarls.vyabusinessipadstaging": 8083,   # Business app (iPad, staging)
        "org.vyapy.sarls.vyaconsumerstaging": 8084,       # Consumer app (staging) — its
        # own port so it never collides with the prod Consumer on 8081. Without this both
        # resolve to 8081 and the Metro watchdog serves whichever it reaches first, so the
        # staging app could load the prod bundle (and vice versa).
    }

    @classmethod
    def metro_port_for(cls, bundle_id: Optional[str]) -> int:
        """The Metro port an app's Debug build expects (8081 unless it runs its own)."""
        return cls._APP_METRO_PORTS.get(bundle_id or "", cls.METRO_PORT)

    def _kill_metro_for_repo(self, repo_path: str) -> int:
        """Kill any Metro bundler running out of *repo_path*.

        Metro is a long-lived dev server that caches the module graph in memory. When
        a build downgrades a dependency (e.g. react-native-qrcode-svg 6.3.x → 6.1.2 to
        drop the `react-native-svg/css` import), a Metro started against the OLD tree
        keeps resolving the removed module and the app red-boxes 'Unable to resolve
        module react-native-svg/css' — the fix is applied on disk but the running
        packager never sees it. Killing it forces the next ensure_metro() to cold-start
        against the corrected node_modules. Returns how many processes were killed.
        """
        import signal as _signal
        repo_path = os.path.abspath(repo_path)
        killed = 0
        try:
            pids = subprocess.run(
                ["pgrep", "-f", "react-native start"],
                capture_output=True, text=True, timeout=10,
            ).stdout.split()
        except Exception:
            return 0
        for pid in pids:
            try:
                cwd = subprocess.run(
                    ["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                    capture_output=True, text=True, timeout=10,
                ).stdout
                if repo_path in cwd:
                    os.kill(int(pid), _signal.SIGTERM)
                    killed += 1
                    logger.info(f"Reset Metro (pid {pid}) for {repo_path} after a dependency fix")
            except Exception:
                continue
        return killed

    def _metro_running(self, port: int = METRO_PORT) -> bool:
        """True only once Metro on *port* can actually SERVE, not merely once it has
        bound the port.

        Metro opens its socket several seconds before it is ready to answer for a
        bundle. A plain TCP connect therefore succeeds too early: the app gets
        launched against a packager that cannot answer yet, RCTBundleURLProvider
        returns a nil URL, and React Native dies on "No bundle URL present" — the
        red screen that looks exactly like a broken build. /status is the packager's
        own readiness signal, so ask it.
        """
        from urllib.request import urlopen

        try:
            with urlopen(f"http://127.0.0.1:{port}/status", timeout=2) as resp:
                return b"packager-status:running" in resp.read(64)
        except Exception:
            return False

    def ensure_metro(self, repo_path: str, port: Optional[int] = None,
                     udid: Optional[str] = None, bundle_id: Optional[str] = None) -> Tuple[bool, str]:
        """Start the Metro bundler for this app if it is not already running.

        A DEBUG React Native build does not embed its JavaScript — it fetches the
        bundle from Metro when the app launches. Without it the app shows the red
        "No bundle URL present" screen. Apps that run their own packager (e.g. the
        Business app on 8082) need Metro on *their* port AND the app pointed at it
        via RCT_jsLocation — pass udid + bundle_id and this sets that user-default.

        Metro is left running: it is a long-lived dev server shared by every run.
        """
        repo_path = os.path.abspath(repo_path)
        port = port or self.metro_port_for(bundle_id)

        if not os.path.exists(os.path.join(repo_path, "package.json")):
            return True, "Not a JS project — Metro not needed."

        # Point the app at this packager (harmless on 8081; essential off it).
        if udid and bundle_id:
            try:
                _run(["xcrun", "simctl", "spawn", udid, "defaults", "write", bundle_id,
                      "RCT_jsLocation", f"localhost:{port}"], timeout=15)
            except Exception as e:
                logger.warning("Could not set RCT_jsLocation for %s: %s", bundle_id, e)

        if self._metro_running(port):
            self._warm_metro_bundle(port)   # ensure the JS bundle is actually servable
            return True, f"Metro already running on :{port}."

        logger.info(f"Starting Metro bundler in {repo_path} on :{port}")
        try:
            subprocess.Popen(
                ["npx", "react-native", "start", "--port", str(port)],
                cwd=repo_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                # Vya's JS bundle is ~18MB; Metro's default ~2GB Node heap OOMs while
                # building it ("FATAL ERROR: Reached heap limit"), leaving the port up
                # but serving 0 bytes → the app red-screens. Give Node a bigger heap.
                env=dict(_build_env(), RCT_METRO_PORT=str(port),
                         NODE_OPTIONS="--max-old-space-size=8192"),
                start_new_session=True,  # survive the request/agent that spawned it
            )
        except FileNotFoundError:
            return False, "npx not found — cannot start the Metro bundler."
        except Exception as e:
            return False, f"Could not start Metro: {e}"

        # Metro takes a few seconds to bind the port on a cold start.
        import time as _time
        for _ in range(60):
            if self._metro_running(port):
                # Port is up, but a DEBUG build fetches the JS bundle on launch —
                # if we launch the app before Metro has BUILT the bundle it shows
                # the red "Could not connect to development server" screen. Wait
                # for (and warm) the bundle so the app loads the real UI first try.
                self._warm_metro_bundle(port)
                return True, f"Metro started on :{port}."
            _time.sleep(1)

        return False, (
            f"Metro did not come up on :{port} within 60s. A Debug build "
            f"cannot load its JS bundle without it."
        )

    def _warm_metro_bundle(self, port: int, timeout: int = 120) -> bool:
        """Block until Metro can actually serve the JS bundle (HTTP 200), building
        it if needed. Prevents the app from launching before the bundle is ready
        (the red 'Could not connect to development server' screen)."""
        import time as _time
        import urllib.request
        url = (f"http://localhost:{port}/index.bundle"
               f"?platform=ios&dev=true&minify=false")
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=timeout) as r:
                    if r.status == 200:
                        logger.info("Metro bundle is servable on :%s", port)
                        return True
            except Exception:
                _time.sleep(2)
        logger.warning("Metro bundle not confirmed servable on :%s within %ss", port, timeout)
        return False

    # ── Install / launch ─────────────────────────────────────────────────────

    def resolve_ios_device(
        self, device_id: Optional[str], prefer: Optional[str] = None,
        prefer_requested: bool = False,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Pick which simulator this run should use, so it always starts.

        *prefer* is an optional device-family hint ("iPhone" / "iPad") derived from
        the app under test, used to break ties toward the intended device.

        *prefer_requested* honours an explicit user choice: if the requested UDID is
        a real available simulator, use exactly it (the caller boots it if needed)
        instead of falling back to whatever is already booted. Set this for runs
        where the user picked the device from a dropdown; leave it off for
        auto-selected runs (PR/webhook), where booted-wins is desired.

        Policy (in order):
          1. If simulators are already booted, run on one — no need to boot anything.
             Among booted sims prefer one matching *prefer* (the app's intended
             device family); then an explicitly-requested booted sim; then any
             booted one.
          2. Otherwise nothing is booted: keep the requested device if it exists on
             this machine (the caller boots it next), else fall back to a *prefer*-
             matching sim / an iPhone / any available sim.
          3. If the machine has no available iOS simulator at all, return None.

        Returns (resolved_udid, note) where note describes any substitution (None
        when the requested device is used unchanged); (None, reason) if unusable.
        """
        try:
            out = _run(
                ["xcrun", "simctl", "list", "devices", "available", "-j"], timeout=15,
            )[1]
            data = json.loads(out)
        except Exception as e:
            return device_id, f"could not list simulators ({e}); using requested device as-is"

        sims = []
        for runtime, devs in data.get("devices", {}).items():
            if "iOS" not in runtime:
                continue
            for d in devs:
                if d.get("isAvailable"):
                    sims.append({"udid": d["udid"], "name": d.get("name", ""),
                                 "state": d.get("state", "Shutdown")})

        if not sims:
            return None, "no available iOS simulators on this machine"

        def matches(s):
            return bool(prefer) and prefer.lower() in s["name"].lower()

        requested = next((s for s in sims if s["udid"] == device_id), None)
        booted = [s for s in sims if s["state"] == "Booted"]

        # 0. Explicit user choice wins: use exactly the requested sim if it exists.
        if prefer_requested and requested:
            return requested["udid"], None

        # 1. Prefer an already-booted simulator — run with what's up. Within the
        #    booted set, prefer the app's intended family, then the explicit request.
        if booted:
            # A booted, explicitly-requested sim always wins; only then fall back
            # to the app's device family, then any booted sim.
            pick = ((requested if requested in booted else None)
                    or next((s for s in booted if matches(s)), None)
                    or booted[0])
            if pick is requested:
                return pick["udid"], None
            note = f"using already-booted simulator {pick['name']}"
            if device_id and device_id != pick["udid"]:
                note += f" (requested {device_id} is not booted)"
            return pick["udid"], note

        # 2. Nothing booted — keep the requested device (booted next), else fall back.
        if requested:
            return requested["udid"], None
        pick = (next((s for s in sims if matches(s)), None)
                or next((s for s in sims if "iPhone" in s["name"]), None)
                or sims[0])
        return pick["udid"], (
            f"requested device {device_id or '(none)'} is not available here; "
            f"falling back to {pick['name']}"
        )

    APPIUM_URL = "http://127.0.0.1:4723"

    def _appium_healthy(self, url: Optional[str] = None, timeout: int = 4) -> bool:
        """True only if Appium answers /status promptly. A wedged WebDriverAgent
        leaves the server unresponsive, which is what makes 'the device stop
        working' — so a plain timeout here is the signal to recover."""
        from urllib.request import urlopen
        try:
            with urlopen(f"{(url or self.APPIUM_URL).rstrip('/')}/status", timeout=timeout) as r:
                return r.status == 200
        except Exception:
            return False

    def _clear_stuck_appium(self) -> None:
        """Kill a wedged Appium + its WebDriverAgent so a fresh one can start."""
        try:
            subprocess.run(["pkill", "-9", "-f", "appium"], timeout=10)
        except Exception:
            pass
        # WDA runs xcodebuild + serves on 8100; clear both.
        for cmd in (["pkill", "-9", "-f", "WebDriverAgent"],
                    ["pkill", "-9", "-f", "xcodebuild.*WebDriverAgent"]):
            try:
                subprocess.run(cmd, timeout=10)
            except Exception:
                pass

    def ensure_appium(self, url: Optional[str] = None) -> Tuple[bool, str]:
        """Make sure a healthy Appium is running — restarting a wedged one.

        If Appium already answers, use it (no-op). If it is unresponsive (a stuck
        WDA), clear it and start a fresh server, then wait for it. This is what
        makes runs self-heal instead of hanging on 'device not working'.
        """
        url = url or self.APPIUM_URL
        if self._appium_healthy(url):
            return True, "Appium is running."

        logger.warning("Appium unresponsive at %s — clearing the stuck server/WDA and restarting.", url)
        self._clear_stuck_appium()
        import time as _t
        _t.sleep(2)
        try:
            subprocess.Popen(["appium"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, start_new_session=True, env=_build_env())
        except FileNotFoundError:
            try:
                subprocess.Popen(["npx", "appium"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL, start_new_session=True, env=_build_env())
            except Exception:
                return False, "Appium is not installed / not on PATH — run 'npm i -g appium'."
        except Exception as e:
            return False, f"Could not start Appium: {e}"

        for _ in range(60):
            if self._appium_healthy(url):
                return True, "Appium was stuck — restarted it, now healthy."
            _t.sleep(1)
        return False, "Appium did not come back up within 60s."

    def ensure_ios_booted(self, device_id: str) -> Tuple[bool, str]:
        """Boot the simulator if it is shut down.

        `xcodebuild` happily builds against a shut-down sim, but `simctl install`
        and `launch` fail with 'Unable to lookup in current state: Shutdown'
        (SimError 405). Booting is idempotent here: a device that is already
        Booted returns 'current state: Booted', which we treat as success.
        """
        ok, out = _run(["xcrun", "simctl", "bootstatus", device_id, "-b"], timeout=180)
        if ok or "current state: Booted" in out or "already booted" in out.lower():
            self._open_simulator_ui()
            return True, f"Simulator {device_id[:8]} is booted."
        # bootstatus -b boots then waits; if it refused, try a plain boot.
        ok2, out2 = _run(["xcrun", "simctl", "boot", device_id], timeout=120)
        if ok2 or "current state: Booted" in out2:
            _run(["xcrun", "simctl", "bootstatus", device_id], timeout=180)
            self._open_simulator_ui()
            return True, f"Simulator {device_id[:8]} booted."
        return False, f"Could not boot simulator {device_id[:8]}: {(out2 or out)[:200]}"

    def _open_simulator_ui(self) -> None:
        """Bring up the Simulator.app window. `simctl boot` runs the sim headless;
        without this the run is invisible even though it is executing."""
        try:
            _run(["open", "-a", "Simulator"], timeout=15)
        except Exception:
            pass

    def app_version(self, app_path: str) -> Dict[str, Optional[str]]:
        """Version as the ARTIFACT declares it — read from its own Info.plist.

        Reported so a deploy states what it actually put on the device, rather than
        leaving you to trust that "installed" meant the build you expected.
        """
        out: Dict[str, Optional[str]] = {"version": None, "build": None, "name": None}
        try:
            with open(os.path.join(app_path, "Info.plist"), "rb") as f:
                p = plistlib.load(f)
            out["version"] = p.get("CFBundleShortVersionString")
            out["build"] = p.get("CFBundleVersion")
            out["name"] = p.get("CFBundleDisplayName") or p.get("CFBundleName")
        except Exception as e:
            logger.warning("Could not read version from %s: %s", app_path, e)
        return out

    def installed_bundles(self, device_id: str) -> List[str]:
        """Every app bundle id installed on this simulator.

        Used to turn Appium's "App with bundle identifier '…' unknown" — which says
        nothing about WHY — into a message naming what is actually on the device.
        """
        try:
            ok, out = _run(["xcrun", "simctl", "listapps", device_id], timeout=30)
            if not ok or not out:
                return []
            return sorted(set(re.findall(r'CFBundleIdentifier\s*=\s*"([^"]+)"', out)))
        except Exception as e:
            logger.debug("installed_bundles(%s): %s", device_id, e)
            return []

    def is_installed(self, device_id: str, bundle_id: str) -> bool:
        """Is this exact app on this simulator? (cheap, no plist parsing)"""
        try:
            ok, _ = _run(["xcrun", "simctl", "get_app_container",
                          device_id, bundle_id], timeout=30)
            return bool(ok)
        except Exception:
            return False

    def installed_version(self, device_id: str, bundle_id: str) -> Dict[str, Optional[str]]:
        """Version currently ON the device, so a deploy can report old -> new."""
        try:
            ok, out = _run(["xcrun", "simctl", "get_app_container",
                            device_id, bundle_id, "app"], timeout=30)
            path = (out or "").strip().splitlines()[-1] if ok and out else ""
            if path and os.path.exists(path):
                return self.app_version(path)
        except Exception as e:
            logger.debug("installed_version(%s): %s", bundle_id, e)
        return {"version": None, "build": None, "name": None}

    def uninstall(self, device_id: str, bundle_id: str, platform: str = "ios") -> Tuple[bool, str]:
        """Remove the app (and ALL its data) from the device.

        Used to isolate one test run from the next: without it every run inherits the
        previous run's login, cache and half-finished screens — which is how a leftover
        'Select A Table' modal silently broke every later run for hours.

        NOTE the trade-off: a wiped app comes back at FIRST-RUN — onboarding carousel,
        signed out. Scenarios that assume a signed-in app cannot pass after this, so it
        is opt-in (see FRESH_INSTALL_PER_JOB) until a first-run preamble exists.
        """
        if not bundle_id:
            return False, "No bundle id — nothing to uninstall."
        if platform == "ios":
            ok, out = _run(["xcrun", "simctl", "uninstall", device_id, bundle_id], timeout=120)
        else:
            ok, out = _run(["adb", "-s", device_id, "uninstall", bundle_id], timeout=120)
        # Uninstalling something that is not installed is success, not an error.
        if not ok and ("not installed" in (out or "").lower()
                       or "unknown package" in (out or "").lower()):
            return True, f"{bundle_id} was not installed"
        return ok, (out if not ok else f"Uninstalled {bundle_id}")

    def install(self, device_id: str, artifact: str, platform: str) -> Tuple[bool, str]:
        """Install the built artifact onto the simulator/device."""
        if not artifact or not os.path.exists(artifact):
            return False, f"Artifact not found: {artifact}"

        if platform == "ios":
            ok, out = _run(
                ["xcrun", "simctl", "install", device_id, artifact],
                timeout=INSTALL_TIMEOUT,
            )
            return ok, out if not ok else f"Installed {os.path.basename(artifact)}"

        ok, out = _run(
            ["adb", "-s", device_id, "install", "-r", artifact], timeout=INSTALL_TIMEOUT
        )
        # adb exits 0 even on some failures — check the output.
        if ok and "Failure" in out:
            return False, out
        return ok, out if not ok else f"Installed {os.path.basename(artifact)}"

    def _configure_ios_location(self, device_id: str, bundle_id: str) -> None:
        """Grant location permission + pin a fixed simulator location before launch.

        The Vya apps gate their home list on device GPS (restaurants are shown by
        distance). A fresh install with no location permission — or a sim set to a
        moving 'City Run' route — yields empty coordinates, so the app sends a null
        location and the backend returns ZERO restaurants (looks broken but isn't).
        Pinning coordinates near the test data (Bangalore by default; override with
        VYA_SIM_LAT / VYA_SIM_LON) makes location-gated screens populate. Best-effort
        and never fatal to the launch.
        """
        lat = os.getenv("VYA_SIM_LAT", "12.9987")   # Bangalore Palace — where the
        lon = os.getenv("VYA_SIM_LON", "77.5920")   # Vya test restaurants live
        try:
            _run(["xcrun", "simctl", "privacy", device_id, "grant", "location", bundle_id],
                 timeout=20)
        except Exception as e:
            logger.debug(f"grant location failed (non-fatal): {e}")
        try:
            _run(["xcrun", "simctl", "location", device_id, "set", f"{lat},{lon}"], timeout=20)
            logger.info(f"Pinned sim {device_id[:8]} location to {lat},{lon} for {bundle_id}")
        except Exception as e:
            logger.debug(f"set location failed (non-fatal): {e}")

    def launch(self, device_id: str, bundle_id: str, platform: str) -> Tuple[bool, str]:
        """Launch the installed app so it is visible on screen / in the stream."""
        if not bundle_id:
            return False, "No bundle id available to launch."
        if platform == "ios":
            # Ensure location-gated screens (restaurant lists) have real coordinates.
            self._configure_ios_location(device_id, bundle_id)
            ok, out = _run(
                ["xcrun", "simctl", "launch", device_id, bundle_id], timeout=INSTALL_TIMEOUT
            )
            return ok, out
        ok, out = _run(
            ["adb", "-s", device_id, "shell", "monkey", "-p", bundle_id,
             "-c", "android.intent.category.LAUNCHER", "1"],
            timeout=INSTALL_TIMEOUT,
        )
        return ok, out


app_builder = AppBuilder()


def start_metro_watchdog(interval: int = 25) -> None:
    """Keep each RN app's Metro packager alive so the app never shows the red
    'No bundle URL present' / 'Could not connect' screen after Metro dies.

    Every *interval* seconds it checks every cloned JS project's Metro port and
    restarts it if down — so the user never has to start Metro by hand.
    """
    import threading
    import time as _time

    def _loop():
        # Small delay so the DB / repos are ready after startup.
        _time.sleep(8)
        while True:
            try:
                from automation.database.config import SessionLocal
                from automation.database.models import TestProject
                from automation.projects.repository import repository_manager
                with SessionLocal() as db:
                    projects = db.query(TestProject).all()
                for p in projects:
                    try:
                        repo = repository_manager.get_repo_path(p.id)
                        if not repo or not os.path.exists(os.path.join(repo, "package.json")):
                            continue
                        # Only manage RN apps we actually test (a bundle id set).
                        if not p.app_bundle_id:
                            continue
                        port = app_builder.metro_port_for(p.app_bundle_id)
                        if not app_builder._metro_running(port):
                            logger.info("Metro watchdog: :%s down for '%s' — restarting", port, p.name)
                            app_builder.ensure_metro(repo, bundle_id=p.app_bundle_id)
                    except Exception as e:
                        logger.debug("metro watchdog (project %s): %s", getattr(p, "id", "?"), e)
            except Exception as e:
                logger.debug("metro watchdog cycle failed: %s", e)
            _time.sleep(interval)

    threading.Thread(target=_loop, daemon=True).start()
    logger.info("Metro watchdog started — keeps RN packagers alive every %ss.", interval)
