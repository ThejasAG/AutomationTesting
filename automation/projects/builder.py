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

import contextlib
import glob
import json
import logging
import os
import plistlib
import re
import shutil
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# A cold xcodebuild of a large RN app genuinely takes this long.
BUILD_TIMEOUT = 3600
INSTALL_TIMEOUT = 300
# `pod install` on a machine with a COLD CocoaPods cache downloads the whole spec
# repo before it installs anything -- measured here at >30 min for a 108-pod project,
# which is exactly what a new Mac does on its first build of every project. The old
# 1800s ceiling killed it a few seconds after the pods had actually landed
# ("Pods installed: 108" in the same failure), turning a slow first run into a failed
# one. --repo-update gets longer still: it re-fetches specs by definition.
POD_TIMEOUT = 5400              # 90 min: first run on a cold cache
POD_REPO_UPDATE_TIMEOUT = 7200  # 2 h: the same, plus a full spec-repo refresh
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

# Transitive packages that must be pinned even though the app never declares them.
#
# RN_KNOWN_FIXES cannot express these: it only rewrites versions already present in
# package.json ("if pkg in deps"), and a dependency-of-a-dependency is not. npm and
# yarn resolve those ranges to the newest release, which is how a package the app
# pins indirectly still moves underneath it.
#
# Each entry names the RUNTIME failure it prevents, because the symptom never points
# at the package: the build succeeds, the bundle serves, and the app red-screens on
# the one screen that uses it.
RN_TRANSITIVE_PINS: Dict[str, Dict[str, str]] = {
    "0.68": {
        # qrcode 1.5.4 rewrote ByteData to `new TextEncoder().encode(data)`.
        # TextEncoder is a browser/Node global that React Native's JS runtime
        # (JSC and Hermes) does not provide, so rendering ANY <QRCode> throws
        # "Can't find variable: TextEncoder" and takes the screen down with a red
        # box -- seen on the booking confirmation screen at the end of a preorder.
        # 1.5.3 uses the `encode-utf8` package instead and needs no polyfill.
        #
        # Why it is reachable at all: react-native-qrcode-svg 6.3.x depends on
        # qrcode ^1.5.1 AND ships text-encoding as a polyfill, but RN_KNOWN_FIXES
        # downgrades that package to 6.1.2 (for the react-native-svg 12.x peer),
        # and 6.1.2 predates the polyfill while its own ^1.5.0 range still admits
        # 1.5.4. The downgrade is correct; this pin covers what it leaves exposed.
        "qrcode": "1.5.3",
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
# Each entry is injected ONLY when the branch's own source imports it — see
# _source_imports. These requirements are branch-specific, so a global injection
# is wrong: it adds a dependency the branch never uses, and an unused native
# dependency can still break the build it was added to protect.
RN_REQUIRED_DEPS: Dict[str, Dict[str, str]] = {
    "0.68": {
        # App/Utils/videoUploadTracker.js requires it on Thai-filter and
        # preprod-2-May18. The require is lazy and wrapped in try/except, which
        # protects the RUNTIME but not Metro — static resolution still fails and
        # 500s the bundle.
        #
        # 1.10.3 was the pinned version until react-native-compressor's
        # ios/Video/VideoMain.swift `import AssetsLibrary` stopped building
        # against the iOS 26 SDK, where Apple removed the framework. 1.13.0 is
        # the first release that uses Photos instead; its exported API is
        # identical to 1.10.3's and its peers are unconstrained, so the bump
        # changes nothing for callers.
        "react-native-compressor": "1.13.0",
    },
}

# Only these are worth a registry round-trip — a compatibility conflict with
# React Native can only come from a package that touches React Native.
_RN_PKG_RE = re.compile(r"^(@react-native|react-native-|@react-navigation)")


# Inserted by AppBuilder._expose_table_sheet(). Kept verbatim from the verified
# app-side change so the patched file matches what was measured on the device
# (sheet went 4 -> 19 accessibility elements). See that method for the why.
_ACCESSIBLE_OVERLAY_SRC = '''/**
 * Drop-in replacement for react-native-magnus <Overlay>, for sheets whose
 * contents must be individually reachable by accessibility tooling.
 *
 * magnus renders its Overlay as:
 *   Modal > TouchableWithoutFeedback(onBackdropPress) > View
 *         > TouchableWithoutFeedback(null) > View > children
 *
 * TouchableWithoutFeedback defaults to accessible={true}, and on iOS an
 * accessible ancestor collapses its entire subtree into ONE accessibility
 * element with the descendants' labels concatenated. The "Select A Table" sheet
 * therefore surfaced as a single element labelled
 * "Select A Table I1 I2 O1 O2 O3 ..." spanning the whole screen — so neither the
 * table chips nor Apply could be reached individually, by any tool. Labelling
 * the children does not help: a child cannot escape an accessible ancestor.
 *
 * Same visuals (centred sheet, 50% black scrim, fixed width, rounded corners),
 * no grouping ancestor. Used ONLY by the two table sheets; the other Overlay
 * call sites in this file are deliberately left alone.
 */
const AccessibleOverlay = ({visible, w = 550, rounded = 43, onBackdropPress, children}) => (
  <RNModal
    transparent
    visible={!!visible}
    animationType="fade"
    onRequestClose={onBackdropPress}>
    <View
      accessible={false}
      style={{
        flex: 1,
        alignItems: 'center',
        justifyContent: 'center',
        backgroundColor: 'rgba(0,0,0,0.5)',
      }}>
      {/* Backdrop. accessible={false} so it does not become a screen-sized
          accessibility element sitting over the sheet. */}
      <Pressable
        accessible={false}
        style={StyleSheet.absoluteFill}
        onPress={onBackdropPress}
      />
      <View
        accessible={false}
        style={{
          backgroundColor: '#ffffff',
          width: w,
          borderRadius: rounded,
          overflow: 'hidden',
        }}>
        {children}
      </View>
    </View>
  </RNModal>
);

'''


def _is_build_input(path: str) -> bool:
    """True for files only the BUILD reads, never the running app.

    Such an edit must always be reverted, even for a Debug build whose JS edits are
    deliberately left in place for Metro.
    """
    return path.endswith((".pbxproj", ".xcconfig", ".plist", ".entitlements"))


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
    # RN's "Start Packager" build phase launches its own Metro on 8081 unless this
    # is set. That stray server outlives the build, serves whatever the checkout
    # held at that moment, and catches any app not explicitly pointed elsewhere
    # ("Could not get BatchedBridge"). The platform starts Metro itself, on the
    # environment's port, in ensure_metro.
    env["RCT_NO_LAUNCH_PACKAGER"] = "1"
    # Tools the platform installs for itself (machine_setup) and the usual
    # install prefixes -- a daemon launched outside a login shell often has a
    # PATH without them, and then `pod` "is not installed" when it is.
    import glob as _glob
    extra = sorted(_glob.glob(os.path.expanduser("~/.gem/ruby/*/bin"))) + [
        "/opt/homebrew/bin", "/usr/local/bin"]
    parts = env.get("PATH", "").split(os.pathsep)
    env["PATH"] = os.pathsep.join(
        [d for d in extra if os.path.isdir(d) and d not in parts] + parts)
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
                        "nonzero exit code", ": line ",
                        # RN's find-node.sh runs `nvm use default`; an nvm with
                        # no default alias aborts every script phase with this.
                        "is not yet installed", "nvm install"))):
            if s not in keep:
                keep.append(s)
    return keep[-limit:] if keep else ["(the script produced no recognisable "
                                       "diagnostic — run the build directly to "
                                       "see its full output)"]


_ERROR_LABEL = re.compile(r"(^|\s)(fatal )?error:")


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
        # `error:` must be a severity label, not a substring: libc++'s header is
        # literally named `system_error`, so "In file included from
        # .../system_error:152:" would otherwise pass as the compiler error and
        # hide a failed script phase (the branch below) behind a bogus line.
        if _ERROR_LABEL.search(stripped) and stripped not in errors:
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

    def _nested_copies(self, repo_path: str, pkg: str) -> List[Tuple[str, str]]:
        """(directory, version) for every copy of *pkg* NESTED under another package.

        npm and yarn hoist a shared dependency to the top of node_modules, but when
        two dependents want incompatible ranges they also leave a private copy inside
        the dependent -- and Node resolves that one first. A top-level check then
        reports the pinned version while the app actually loads the nested one.

        This is not theoretical: pinning qrcode to 1.5.3 at the top level left
        react-native-qrcode-svg/node_modules/qrcode at 1.5.4, so every QR render
        still threw "Can't find variable: TextEncoder" with the pin apparently
        applied.
        """
        root = os.path.join(repo_path, "node_modules")
        out: List[Tuple[str, str]] = []
        if not os.path.isdir(root):
            return out
        for dep in os.listdir(root):
            nested = os.path.join(root, dep, "node_modules", *pkg.split("/"))
            data = self._read_json(os.path.join(nested, "package.json"))
            if data and data.get("version"):
                out.append((nested, data["version"]))
        return out

    def _prune_shadowing_copies(self, repo_path: str, rn_version: str,
                                targets: Dict[str, str], result: Dict[str, Any]) -> None:
        """Delete nested copies that shadow a pinned version.

        Covers the curated pins as well as anything fixed this run: a pin whose top
        level is already correct produces no target, yet a private copy inside a
        dependent still wins Node's resolution and the app loads it instead.
        """
        rn_key = ".".join(str(x) for x in (_parse_ver(rn_version) or (0, 0, 0))[:2])
        sweep = dict(RN_TRANSITIVE_PINS.get(rn_key, {}))
        sweep.update(targets)
        for pkg, target in sweep.items():
            for nested_dir, nested_ver in self._nested_copies(repo_path, pkg):
                if nested_ver == target:
                    continue
                logger.warning("removing nested %s@%s at %s — it shadows the pinned "
                               "%s", pkg, nested_ver, nested_dir, target)
                try:
                    shutil.rmtree(nested_dir)
                    note = f"{pkg}@{nested_ver} (nested, removed)"
                    if note not in result["fixed"]:
                        result["fixed"].append(note)
                except OSError as e:
                    result["failed"].append(
                        f"could not remove nested {pkg}@{nested_ver}: {e}")
                    result["ready_to_build"] = False

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
        # Retry before giving up. A single dropped request used to decide that a
        # package had no compatible version, which fails the whole build -- a 3.7-hour
        # prepare died this way on a lookup that succeeded seconds later. Network
        # blips are expected on a laptop (sleep, VPN, flaky wifi) and are not an
        # answer about the package.
        doc = None
        last_err = None
        for attempt in range(3):
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
                break
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))    # 2s, then 4s
        if doc is None:
            logger.warning("npm registry lookup failed for %s after 3 attempts: %s",
                           pkg, last_err)
            # Do NOT cache a failure. Caching None made one blip permanent for the
            # rest of the process, so every later question about this package got the
            # same wrong answer.
            return None

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

    def platform_dependency_plan(self, repo_path: str) -> Dict[str, Dict[str, str]]:
        """What the PLATFORM would inject here, and what it deliberately skips.

        Read-only: it asks the same questions _conflicting_packages does (same
        RN_REQUIRED_DEPS table, same _source_imports gate) and installs nothing.
        It exists so the setup report can state the injection decision up front —
        "injected 0, skipped react-native-compressor, no source import detected"
        — instead of that decision only ever appearing in a log line.
        """
        rn_version = self._rn_version(repo_path)
        if not rn_version:
            return {"injected": {}, "skipped": {}}
        rn_key = ".".join(str(x) for x in (_parse_ver(rn_version) or (0, 0, 0))[:2])

        injected: Dict[str, str] = {}
        skipped: Dict[str, str] = {}
        for pkg, target in RN_REQUIRED_DEPS.get(rn_key, {}).items():
            if self._source_imports(repo_path, pkg):
                injected[pkg] = target
            else:
                skipped[pkg] = "no source import detected"
        return {"injected": injected, "skipped": skipped}

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
        #
        # It IS gated on the source actually importing the package, because the
        # import is branch-specific: App/Utils/videoUploadTracker.js requires
        # react-native-compressor on Thai-filter and preprod-2-May18, but on main
        # the same file is a three-line Set with no requires. Injecting it there
        # installed a dependency nothing used, and that dependency's
        # `import AssetsLibrary` is unbuildable against the iOS 26 SDK — so an
        # unconditional injection turned a branch that had no problem into one
        # that could not build at all.
        # 1c. Transitive pins. Gated on the package being INSTALLED rather than
        # declared or imported: the app never names it and never imports it
        # directly, but it is in the tree because something else pulled it in, and
        # the resolver is free to move it. Pinning it in package.json is what makes
        # the version deterministic -- without that, a range on a dependency of a
        # dependency re-resolves to the newest release on every clean install.
        for pkg, target in RN_TRANSITIVE_PINS.get(rn_key, {}).items():
            installed = self._installed_version(repo_path, pkg)
            if installed and installed != target:
                logger.info("transitive pin: %s %s -> %s", pkg, installed, target)
                targets.setdefault(pkg, target)

        for pkg, target in RN_REQUIRED_DEPS.get(rn_key, {}).items():
            if self._source_imports(repo_path, pkg):
                targets.setdefault(pkg, target)
            else:
                logger.info(
                    "%s is not imported by this branch's source — not injecting it.",
                    pkg)

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

        # Nested shadowing copies are checked even when nothing needs installing: a
        # pin whose top level is already correct still loads the WRONG code if a
        # dependent keeps a private copy (Node resolves that one first). Returning
        # early here is what let qrcode read as pinned at 1.5.3 while every QR render
        # threw "Can't find variable: TextEncoder" from a nested 1.5.4.
        self._prune_shadowing_copies(repo_path, rn_version, targets, result)

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

        # Every `npm install <pkg>` PRUNES packages that are in node_modules but not
        # in package.json. Injected dependencies are exactly that shape, so
        # installing one fix can silently delete another -- and the loss only
        # surfaces at run time, as Metro's "Unable to resolve module <x>" red box on
        # a device, long after the build reported success. Verify the whole set is
        # still present and reinstate anything that went missing.
        for pkg, target in list(targets.items()):
            if self._installed_version(repo_path, pkg):
                continue
            logger.warning("%s disappeared during dependency fixes (pruned as "
                           "undeclared) — reinstating it.", pkg)
            spec = f"{pkg}@{target}"
            if os.path.exists(os.path.join(repo_path, "yarn.lock")):
                cmd = self.detect_package_manager(repo_path) + ["add", spec]
            else:
                cmd = ["npm", "install", spec, "--legacy-peer-deps"]
            ok, out = _run(cmd, cwd=repo_path, timeout=NPM_INSTALL_TIMEOUT)
            if ok and self._installed_version(repo_path, pkg):
                if spec not in result["fixed"]:
                    result["fixed"].append(spec)
            else:
                result["failed"].append(
                    f"{spec} (reinstate): {(out or '').strip()[-300:]}")
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

    _A11Y_TABLE_MARK = "// platform: table sheet exposed to accessibility"

    def _expose_table_sheet(self, repo_path: str) -> Optional[str]:
        """Make the "Select A Table" sheet's chips and Apply button reachable.

        WITHOUT THIS THE WAITER SEGMENT CANNOT COMPLETE. Measured on the Vya
        Business iPad build: the whole sheet surfaced as ONE accessibility element
        labelled 'Select A Table I1 I2 O1 ...' spanning the screen — idb saw 4
        elements, Appium saw 0 buttons. @assign_table could tap a table by
        COORDINATE (which needs no accessibility) but then found no Apply/Confirm
        to commit it, so every run failed at 'assign table + send to kitchen'.

        The cause is the ANCESTOR, not missing labels. react-native-magnus
        <Overlay> wraps its children in TouchableWithoutFeedback, which defaults to
        accessible={true}, and on iOS an accessible ancestor collapses its entire
        subtree into one element with the descendants' labels concatenated. A child
        cannot escape that, so labelling the chips alone has no effect — which is
        why the ids were in the source and still unreachable.

        The repair swaps <Overlay> for a local AccessibleOverlay (same visuals, no
        grouping ancestor) for the TWO table sheets only, and labels the chips
        (tableChip<Name> + accessibilityState.selected) and the commit button
        (applyTableBtn). Verified on the iPad: 4 -> 19 elements, tableChipI1..O12
        and applyTableBtn individually discoverable, selected flips false->true.

        This lived as a LOCAL, UNPUSHED commit in one clone ('do not push'), so it
        existed on exactly one machine: every other Mac cloned the app clean, built
        it without the fix, and hit the same unfixable 'no Apply/Confirm' failure.
        That is what this table is for (see MACHINE_SETUP.md §7) — add the repair
        here rather than to one clone by hand, or the next machine repeats the whole
        diagnosis.

        The REAL fix belongs in the app: an element no tool can reach is also an
        element VoiceOver users cannot reach, so this is an accessibility bug, not
        just an automation one. Until that ships, this keeps every machine running.

        JS-only (needs a Metro reload, not a rebuild), idempotent, and additive —
        it only inserts the helper and attributes, never edits existing app logic.
        Like _silence_logbox it touches TRACKED source, so the file shows as
        modified in the app repo. That is deliberate and visible.
        """
        import re as _re

        target = os.path.join(repo_path, "App", "Components", "Modal", "index.js")
        if not os.path.isfile(target):
            return None                                  # not the Business app
        try:
            with open(target) as f:
                content = f.read()
        except OSError as e:
            logger.warning(f"Could not read {target}: {e}")
            return None

        if self._A11Y_TABLE_MARK in content or "AccessibleOverlay" in content:
            return None                                  # already patched

        # Only the two table sheets use this Overlay shape. If the app has been
        # restructured, do nothing rather than guess — a wrong edit here would
        # break the sheet for real users, not just for the automation.
        if "Select A Table" not in content:
            return None

        original = content

        # 1. RNModal import — AccessibleOverlay renders a plain react-native Modal.
        m = _re.search(r"^import\s*\{([^}]*)\}\s*from\s*['\"]react-native['\"];",
                       content, _re.M)
        if not m:
            logger.warning("table-sheet a11y: no react-native import block in %s", target)
            return None
        if "Modal as RNModal" not in m.group(1):
            # Insert in place rather than rebuilding the block: this file's import
            # is one-name-per-line, and collapsing it to a single line would show
            # up as a 16-line reformat in the app team's diff for no reason.
            inner = m.group(1)
            if "\n" in inner:
                indent = _re.search(r"\n(\s*)\S", inner)
                pad = indent.group(1) if indent else "  "
                new_inner = inner.rstrip()
                if not new_inner.endswith(","):
                    new_inner += ","
                new_inner += f"\n{pad}Modal as RNModal,\n"
            else:
                new_inner = inner.rstrip().rstrip(",") + ", Modal as RNModal"
            content = (content[:m.start(1)] + new_inner + content[m.end(1):])

        # 2. The helper, inserted before the first table sheet class that uses it.
        anchor = _re.search(r"^export class Table2 extends Component", content, _re.M)
        if not anchor:
            logger.warning("table-sheet a11y: no Table2 class in %s", target)
            return None
        content = content[:anchor.start()] + _ACCESSIBLE_OVERLAY_SRC + content[anchor.start():]

        # 3. Swap Overlay -> AccessibleOverlay for the table sheets ONLY. Both are
        #    the distinctive w={550} rounded={43} shape; the file's other 7 Overlay
        #    call sites do not match and are deliberately left alone.
        #    The closing tag must be matched PER SHEET, not globally: </Overlay>
        #    appears 9 times in this file and 7 of them belong to other sheets.
        #    Rewriting all of them would mismatch every untouched <Overlay> and the
        #    bundle would not compile. So for each table sheet, rewrite the opening
        #    tag and then the FIRST </Overlay> that follows it.
        n_open = n_close = 0
        pos = 0
        open_re = _re.compile(r"<Overlay\s*\n(\s*)w=\{550\}\s*\n(\s*)rounded=\{43\}")
        while True:
            mo = open_re.search(content, pos)
            if not mo:
                break
            content = (content[:mo.start()]
                       + f"<AccessibleOverlay\n{mo.group(1)}w={{550}}\n{mo.group(2)}rounded={{43}}"
                       + content[mo.end():])
            n_open += 1
            close_at = content.find("</Overlay>", mo.start())
            if close_at == -1:
                break
            content = (content[:close_at] + "</AccessibleOverlay>"
                       + content[close_at + len("</Overlay>"):])
            n_close += 1
            pos = close_at
        if n_open != 2 or n_close != n_open:
            # Refuse a half-applied edit: mismatched tags would not compile, and a
            # build that fails at Metro is far harder to diagnose than this warning.
            logger.warning("table-sheet a11y: expected 2 table Overlays, matched "
                           "%d open / %d close in %s — leaving the file untouched",
                           n_open, n_close, target)
            return None

        # 4. Label the chips and the commit button.
        #    Both anchors must be specific to the TABLE sheets. 'key={idx}' alone
        #    matches 5 unrelated list items, and Styles.cancelButton is shared by
        #    several buttons in this file (food voucher, filter, ...) — labelling
        #    those would put applyTableBtn on the wrong control, which is worse
        #    than not patching at all. So: chips are the Pressables whose selected
        #    state is driven by activeTables, and the commit button is the one
        #    whose onPress calls table(activeTables).
        content, n_chip = _re.subn(
            r"(<Pressable\n(\s*)key=\{idx\}\n)(?=(?:\s*\n)*\s*style=\s*\n?\s*"
            r"\{?\s*\n?\s*!activeTables\.includes\(el\.name\))",
            r"\1\2accessible={true}\n"
            r"\2accessibilityLabel={`tableChip${el.name}`}\n"
            r"\2accessibilityState={{selected: activeTables.includes(el.name)}}\n",
            content)
        content, n_btn = _re.subn(
            r"(<Pressable\n(\s*))(style=\{\[Styles\.cancelButton[^\n]*\n"
            r"\s*onPress=\{\s*\(\s*\)\s*=>\s*\{?\s*table\s*\(\s*activeTables\s*\))",
            r"\1accessible={true}" + "\n" + r"\2" + 'accessibilityLabel="applyTableBtn"'
            + "\n" + r"\2\3",
            content)
        if n_chip != 2 or n_btn != 2:
            # Exactly two of each — one per table sheet. Anything else means the
            # anchors matched something they should not, and a misplaced
            # applyTableBtn would make the automation tap the WRONG control.
            logger.warning("table-sheet a11y: labelled %d chip block(s) and %d button(s) "
                           "(expected 2 and 2) in %s — leaving the file untouched",
                           n_chip, n_btn, target)
            return None

        content = content.replace(
            _ACCESSIBLE_OVERLAY_SRC,
            f"{self._A11Y_TABLE_MARK}\n{_ACCESSIBLE_OVERLAY_SRC}", 1)

        try:
            with open(target, "w") as f:
                f.write(content)
        except OSError as e:
            logger.warning(f"Could not patch {target}: {e}")
            return None
        _ = original                                     # kept for clarity of intent
        msg = (f"Exposed the Select A Table sheet to accessibility "
               f"({n_chip} chip block(s), {n_btn} commit button(s)) — without this "
               f"@assign_table cannot reach Apply")
        logger.info(msg)
        return msg

    _A11Y_ASSIGN_MARK = "// platform: assign-table sheet exposed to accessibility"

    def _expose_assign_table_sheet(self, repo_path: str) -> Optional[str]:
        """Label the waiter's "Please assign a table" sheet (class ``TableView``).

        THIS IS THE SHEET THE WAITER ACTUALLY GETS. It is a DIFFERENT component
        from the "Select A Table" sheet handled by _expose_table_sheet: that one
        is ``Table``/``Table2``; this one is ``TableView`` (Modal/index.js), and
        it is what ``EventDetails`` renders on the assign-a-table flow.

        Measured on staging run 5843f6d9 (2026-09-17): @open_reservation opened
        the booking correctly, then @assign_table reported

            · @assign_table — tapped table 'I3'
            [FAIL] @assign_table — opened the table modal but found no
                   Apply/Confirm to commit it

        Both halves of that are explained by this component:

        * The CHIPS are ``TouchableOpacity`` with no accessibilityLabel. Their
          table name is only readable because the child ``<Subheading>`` renders
          ``el.name``, so the coordinate fallback can see 'I3' by text while the
          modern ``tableChip<Name>`` lookup finds nothing — which is exactly the
          "tapped table 'I3'" line above, i.e. the LEGACY path.
        * The COMMIT button is a ``TouchableOpacity`` whose caption 'Confirm'
          lives in a nested ``<Text>``. With no label on the touchable, neither
          an accessibility-id lookup nor a text match on the button itself can
          reach it — so there is no way to commit the assignment.

        Unlike the "Select A Table" sheet, ``TableView`` wraps in
        ``react-native-modal``, NOT magnus ``Overlay``, so there is no
        accessible-ancestor collapse here and no AccessibleOverlay is needed.
        Labels alone are sufficient, which is why the chips were already
        tappable by coordinate.

        Adds ``tableChip<Name>`` + accessibilityState.selected to each chip and
        ``applyTableBtn`` to the commit button — the SAME ids the other sheet
        uses, so _assign_table needs no new special case. Applied to both the
        iPad (``Components``) and phone (``MobileComponents``) copies.

        JS-only, idempotent, additive. Touches tracked source, like
        _silence_logbox — deliberately visible, because the real fix belongs in
        the app: a control no tool can reach is a control VoiceOver cannot reach.
        """
        import re as _re

        done = []
        for variant in ("Components", "MobileComponents"):
            target = os.path.join(repo_path, "App", variant, "Modal", "index.js")
            if not os.path.isfile(target):
                continue
            try:
                with open(target) as f:
                    content = f.read()
            except OSError as e:
                logger.warning(f"Could not read {target}: {e}")
                continue

            if self._A11Y_ASSIGN_MARK in content:
                continue                                 # already patched
            if "Please assign a table" not in content:
                continue                                 # not this app / restructured

            # CHIPS. Anchored on the state setter that is unique to TableView's
            # table chips, so no other TouchableOpacity in this large file can
            # match. `el` is the table object; `el.name` is what the chip shows.
            content, n_chip = _re.subn(
                r"(<TouchableOpacity\n(\s*)key=\{idx\}\n)"
                r"(?=(?:[^\n]*\n){0,20}?\s*this\.setState\(\{activeTblInfo: el, activeTbl: el\.id)",
                r"\1\2accessible={true}\n"
                r"\2accessibilityLabel={`tableChip${el.name}`}\n"
                r"\2accessibilityState={{selected: el.id === activeTbl && !radioKey}}\n",
                content, count=1)

            # COMMIT BUTTON. Anchored on the onPress that calls _assignTable —
            # the only control in the file that commits a table assignment.
            # Bounded lookahead: the commit button is the TouchableOpacity that
            # opens with activeOpacity={1.0} and reaches _assignTable within the
            # next ~25 lines. An unbounded (?:.|\n)*? would scan the whole 6300-
            # line file and could pair the tag with a far-away match.
            content, n_btn = _re.subn(
                r"(<TouchableOpacity\n(\s*))(activeOpacity=\{1\.0\}\n"
                r"(?:[^\n]*\n){0,25}?\s*\?\s*this\._assignTable\(activeTblInfo, radioKey\))",
                r"\1accessible={true}" + "\n" + r"\2" + 'accessibilityLabel="applyTableBtn"'
                + "\n" + r"\2\3",
                content, count=1)

            if n_chip != 1 or n_btn != 1:
                # Refuse rather than half-apply: a misplaced applyTableBtn would
                # make the automation tap the WRONG control, which is worse than
                # not patching at all.
                logger.warning("assign-table a11y: matched %d chip / %d button in %s "
                               "(expected 1 and 1) — leaving the file untouched",
                               n_chip, n_btn, target)
                continue

            content = content.replace("export class TableView extends Component",
                                      f"{self._A11Y_ASSIGN_MARK}\n"
                                      "export class TableView extends Component", 1)
            try:
                with open(target, "w") as f:
                    f.write(content)
            except OSError as e:
                logger.warning(f"Could not patch {target}: {e}")
                continue
            done.append(variant)

        if not done:
            return None
        msg = (f"Exposed the assign-table sheet (TableView) to accessibility in "
               f"{', '.join(done)} — without this @assign_table cannot reach Confirm")
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

    # Source directories worth scanning for an import. node_modules is excluded:
    # a dependency importing the package says nothing about whether THIS app does.
    _SOURCE_DIRS = ("App", "src", "app", "js")
    _SOURCE_EXTS = (".js", ".jsx", ".ts", ".tsx")

    def _source_imports(self, repo_path: str, pkg: str) -> bool:
        """Does this branch's own source reference *pkg*?

        Matches `import ... from 'pkg'`, `require('pkg')` and the subpath forms.
        The lazy `require('react-native-compressor')?.Video` in
        videoUploadTracker.js is exactly the shape that has to be caught: it is
        wrapped in try/catch, which protects the runtime but not Metro — static
        resolution still fails and 500s the whole bundle.

        Errs toward True on an unreadable tree: a missed import breaks the bundle
        with a symptom that points nowhere near the missing package, whereas an
        unnecessary install merely wastes time.
        """
        roots = [os.path.join(repo_path, d) for d in self._SOURCE_DIRS]
        roots = [r for r in roots if os.path.isdir(r)]
        if not roots:
            # No recognised source directory — cannot prove absence, so keep the
            # old unconditional behaviour rather than risk a broken bundle.
            logger.debug("no source directory under %s; assuming %s is needed",
                         repo_path, pkg)
            return True

        # Quoted, and allowing a subpath: 'pkg', "pkg/lib/x".
        rx = re.compile(rf"""['"]{re.escape(pkg)}(?:/[^'"]*)?['"]""")
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames
                               if d not in ("node_modules", "__tests__", ".git")]
                for name in filenames:
                    if not name.endswith(self._SOURCE_EXTS):
                        continue
                    try:
                        with open(os.path.join(dirpath, name), "r",
                                  errors="replace") as f:
                            if rx.search(f.read()):
                                return True
                    except OSError:
                        continue
        return False

    def _folly_cxx_standard(self, pod_dir: str) -> List[str]:
        """RCT-Folly xcconfigs that pin a C++ standard folly cannot compile at.

        React Native 0.68 ships RCT-Folly.podspec with

            pod_target_xcconfig = { "CLANG_CXX_LANGUAGE_STANDARD" => "c++17" }

        and folly pulls in boost's container_hash/hash.hpp, whose hash_base
        derives from std::unary_function. That template was deprecated in C++11
        and REMOVED in C++17, and the iOS 26 SDK's libc++ no longer provides it:

            boost/container_hash/hash.hpp:131:33: error: no template named
            'unary_function' in namespace 'std'

        The error names boost, but boost's own target compiles fine -- the
        project-level setting gives it gnu++14. It is the RCT-Folly target,
        overriding that to c++17, which drags boost's headers into a compile
        where the symbol is gone.

        Measured, not assumed: folly 2021.06 builds clean at gnu++14 -- all 22
        objects including json.cpp, zero errors. The c++17 pin is aspirational
        for that vintage rather than required, so compiling it at the standard
        it was written for is the fix; patching boost would work around the
        symptom, and std::unary_function supplies argument_type/result_type
        typedefs that downstream code expects.
        """
        cfg_dir = os.path.join(pod_dir, "Pods", "Target Support Files", "RCT-Folly")
        bad = []
        for name in ("RCT-Folly.debug.xcconfig", "RCT-Folly.release.xcconfig"):
            path = os.path.join(cfg_dir, name)
            try:
                with open(path, "r", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            m = re.search(r"^CLANG_CXX_LANGUAGE_STANDARD\s*=\s*(\S+)", text, re.M)
            if m and re.search(r"(?:c|gnu)\+\+(1[7-9]|2\d)", m.group(1)):
                bad.append(path)
        return bad

    def _repair_folly_cxx_standard(self, pod_dir: str) -> int:
        """Pin the RCT-Folly target back to gnu++14. Returns files changed.

        Target-level, not project-level: the podspec's own xcconfig overrides
        anything set on the project, exactly as it already overrides the
        gnu++14 CocoaPods puts there.

        Pods/ is generated and gitignored, so nothing tracked is touched.
        """
        changed = 0
        for path in self._folly_cxx_standard(pod_dir):
            try:
                with open(path, "r", errors="replace") as f:
                    text = f.read()
                patched = re.sub(r"^(CLANG_CXX_LANGUAGE_STANDARD\s*=\s*)\S+",
                                 r"\1gnu++14", text, count=1, flags=re.M)
                if patched == text:
                    continue
                mode = os.stat(path).st_mode
                os.chmod(path, mode | stat.S_IWUSR)
                try:
                    with open(path, "w") as f:
                        f.write(patched)
                finally:
                    os.chmod(path, mode)
                changed += 1
            except OSError as e:
                logger.warning("could not pin folly's C++ standard in %s: %s", path, e)
        if changed:
            logger.info("Pinned RCT-Folly to gnu++14 in %d xcconfig(s): boost's "
                        "std::unary_function is gone from the iOS 26 SDK's C++17 "
                        "library", changed)
        return changed

    @staticmethod
    def path_has_space(repo_path: str) -> Optional[str]:
        """The message to show when this checkout sits under a path with a space.

        Not a style objection. React Native 0.68's own tooling shells out with
        unquoted paths, so a space silently truncates them and the command fails
        against a directory that does not exist:

            sed:  /Users/x/Desktop/automation: No such file or directory
            find: /Users/x/Desktop/automation: No such file or directory

        Three separate failures on one machine traced to this — RN's folly
        patch, the Podfile's own sed, and FBReactNativeSpec's codegen phase.
        The first two the platform can repair afterwards. The codegen one it
        cannot: that script is generated by RN and run by Xcode, and nothing
        here owns the build phases Xcode executes.

        So this is reported up front rather than discovered twenty minutes into
        a build, as an error naming a dependency instead of the directory name.
        """
        if " " not in os.path.abspath(repo_path):
            return None
        root = os.path.abspath(repo_path)
        # Name the offending component, not the whole path -- that is the part
        # someone has to change.
        culprit = next((part for part in root.split(os.sep) if " " in part), "")
        return (
            f"This project is checked out under a path containing a space "
            f"({culprit!r} in {root}).\n\n"
            f"React Native 0.68's build tooling shells out with unquoted paths, "
            f"so the space truncates them: its own RCT-Folly patch, the "
            f"Podfile's post_install, and FBReactNativeSpec's codegen phase all "
            f"fail against a directory that does not exist, and the errors name "
            f"dependencies rather than the path.\n\n"
            f"The platform repairs what it can afterwards, but it cannot fix the "
            f"codegen phase — Xcode runs that script, not us. Move the platform "
            f"to a path with no spaces (for example {culprit.replace(' ', '-')!r}) "
            f"and re-run.")

    @staticmethod
    def _obsoleted_frameworks() -> Dict[str, str]:
        """Frameworks this SDK has REMOVED, read from the SDK itself.

        Apple records the fact in each framework's Swift interface:

            @available(iOS, introduced: 9.0, deprecated: 9.0, obsoleted: 26.0)

        Deriving it beats a hardcoded list, which would be wrong the day the
        next SDK ships. Objective-C is deliberately not consulted: the ObjC
        headers still declare these symbols and compile with a deprecation
        warning -- it is the SWIFT interface that turns them into a hard error,
        which is why a pod with even one Swift file fails while three pure-ObjC
        pods using the same framework build fine.

        Returns {framework: "26.0"}; empty when the SDK cannot be read, because
        a missing warning must never become a failed build.
        """
        try:
            sdk = subprocess.run(["xcrun", "--sdk", "iphonesimulator",
                                  "--show-sdk-path"], capture_output=True,
                                 text=True, timeout=20).stdout.strip()
            ver = subprocess.run(["xcrun", "--sdk", "iphonesimulator",
                                  "--show-sdk-version"], capture_output=True,
                                 text=True, timeout=20).stdout.strip()
        except Exception:
            return {}
        if not sdk or not ver:
            return {}
        major = ver.split(".")[0]
        out: Dict[str, str] = {}
        for mod in glob.glob(os.path.join(sdk, "usr", "lib", "swift",
                                          "*.swiftmodule")):
            fw = os.path.basename(mod)[: -len(".swiftmodule")]
            for iface in glob.glob(os.path.join(mod, "*.swiftinterface")):
                try:
                    with open(iface, "r", errors="replace") as f:
                        if re.search(rf"obsoleted:\s*{re.escape(major)}\.", f.read()):
                            out[fw] = ver
                            break
                except OSError:
                    continue
        return out

    def removed_framework_users(self, repo_path: str) -> List[str]:
        """Pods that LINK a framework this SDK has removed.

        Linkage, not mention. The first version of this searched pod sources for
        the framework name and blamed FBSDKCoreKit, which was wrong twice over:
        its Swift files never import AssetsLibrary, and the only reference is a
        dynamic lookup by name (fbsdkdfl_ALAssetsLibraryClass) that the linker
        never sees. Acting on that would have meant removing a dependency for a
        problem it does not cause.

        A podspec's `frameworks` field is what actually puts -framework on the
        link line, so that is what is read.

        Nothing here is repairable -- the symbol is gone from the SDK -- and in
        practice a removed framework is usually NOT what fails the build: the
        Objective-C headers still compile with a deprecation warning, and the
        real blocker on this project turned out to be boost's std::unary_function
        instead. So this is advisory, and says so.
        """
        removed = self._obsoleted_frameworks()
        if not removed:
            return []

        specs = glob.glob(os.path.join(repo_path, "ios", "Pods",
                                       "Local Podspecs", "*.podspec.json"))
        specs += glob.glob(os.path.join(repo_path, "node_modules", "*",
                                        "*.podspec"))
        warnings = []
        seen = set()
        for spec in specs:
            try:
                with open(spec, "r", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            pod = os.path.basename(spec).split(".podspec")[0]
            for fw, sdkver in sorted(removed.items()):
                # Only a declared framework dependency reaches the link line.
                if not re.search(rf'(?:"frameworks"\s*:|\.frameworks?\s*=)[^\n]*'
                                 rf'{re.escape(fw)}', text):
                    continue
                if (pod, fw) in seen:
                    continue
                seen.add((pod, fw))
                warnings.append(
                    f"{pod} links {fw}, which Apple removed in iOS {sdkver}. "
                    f"This is advisory: the Objective-C headers still compile "
                    f"with a deprecation warning, so it may not be what fails "
                    f"the build. If a Swift file imports {fw} the build cannot "
                    f"succeed and the dependency has to change -- nothing can "
                    f"patch a symbol the SDK no longer ships.")

        # The advisory above has always said a Swift import is the fatal case;
        # it just never looked for one. It did not, and so blamed
        # react-native-blob-util -- which ships no Swift at all -- for a
        # SwiftExplicitDependencyCompileModuleFromInterface failure that
        # react-native-compressor caused with `import AssetsLibrary`.
        warnings.extend(self._swift_importers_of_removed_frameworks(repo_path, removed))
        return warnings

    def _swift_importers_of_removed_frameworks(
        self, repo_path: str, removed: Dict[str, str]) -> List[str]:
        """Dependencies whose SWIFT sources import a framework the SDK dropped.

        This is the unrecoverable case. A Swift `import X` needs a module to
        compile against; when the SDK no longer ships one the build fails at
        SwiftExplicitDependencyCompileModuleFromInterface and no patch can help.
        An Objective-C `#import <X/X.h>` is not equivalent -- those headers are
        still present and compile with a deprecation warning.
        """
        found: List[str] = []
        node_modules = os.path.join(repo_path, "node_modules")
        if not os.path.isdir(node_modules):
            return found

        patterns = {fw: re.compile(rf"^\s*import\s+{re.escape(fw)}\s*$", re.M)
                    for fw in removed}
        for pkg in sorted(os.listdir(node_modules)):
            ios_dir = os.path.join(node_modules, pkg, "ios")
            if not os.path.isdir(ios_dir):
                continue
            for root, _dirs, files in os.walk(ios_dir):
                for name in files:
                    if not name.endswith(".swift"):
                        continue
                    try:
                        with open(os.path.join(root, name), "r", errors="replace") as f:
                            text = f.read()
                    except OSError:
                        continue
                    for fw, rx in patterns.items():
                        if rx.search(text):
                            rel = os.path.relpath(os.path.join(root, name),
                                                  os.path.join(node_modules, pkg))
                            found.append(
                                f"BUILD BLOCKED [APPLICATION DEPENDENCY] {pkg}: "
                                f"{rel} has `import {fw}`, and Apple removed {fw} "
                                f"in iOS {removed[fw]}. A Swift import needs a "
                                f"module that no longer exists, so this cannot be "
                                f"patched or worked around -- {pkg} must be "
                                f"upgraded or replaced.")
                            break
        return found

    def _verify_pods(self, pod_dir: str, out: str) -> str:
        """Checks that run after a SUCCESSFUL pod install.

        CocoaPods exiting 0 does not mean the Podfile's post_install hooks did
        what they were written to do -- they are shelled-out commands whose
        failures it does not surface. Each check here is a case where that gap
        turned into a build error thousands of lines later, naming a dependency
        instead of the hook.
        """
        pinned = self._repair_folly_cxx_standard(pod_dir)
        if pinned:
            out += ("\n[platform] RCT-Folly was pinned to gnu++14 in "
                    f"{pinned} xcconfig(s). React Native 0.68's podspec asks for "
                    "c++17, where std::unary_function -- which boost's hash.hpp "
                    "derives from -- no longer exists in the iOS 26 SDK. folly "
                    "2021.06 compiles clean at gnu++14.")

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

        # Discriminate on the SHAPE of the iOS clause, not on whether
        # TARGET_OS_IPHONE appears -- it appears in BOTH forms, which is why an
        # earlier version of this check reported every unpatched header as
        # already fixed:
        #
        #   unpatched  (TARGET_OS_IPHONE && (__IPHONE_OS_VERSION_MIN_REQUIRED
        #                                     < __IPHONE_10_0))
        #   patched    (TARGET_OS_IPHONE)
        #
        # The version gate is false on any modern deployment target, so
        # FOLLY_HAVE_CLOCK_GETTIME is never set, the guard below stays true, and
        # the typedef compiles straight into the SDK's enum.
        if re.search(r"TARGET_OS_IPHONE\s*&&\s*\(?\s*__IPHONE_OS_VERSION_MIN_REQUIRED",
                     text):
            return True
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

        # Drop the version gate, leaving a bare (TARGET_OS_IPHONE) -- which is
        # exactly what React Native's own react_native_pods.rb does, and what
        # produces the header shape found on machines where this builds.
        #
        # Note the Podfile's __IPHONE_13_0 -> __IPHONE_14_0 sed is a no-op on
        # this folly version: that string appears zero times in
        # RCT-Folly 2021.06.28. RN's patch is the one that does the work, and it
        # is the one that silently fails -- an unquoted path in its shell
        # command breaks on a directory name containing a space.
        patched = re.sub(
            r"TARGET_OS_IPHONE\s*&&\s*\(?\s*__IPHONE_OS_VERSION_MIN_REQUIRED\s*<\s*"
            r"__IPHONE_\d+_\d+\s*\)?",
            "TARGET_OS_IPHONE", text)
        patched = patched.replace("__IPHONE_13_0", "__IPHONE_14_0")

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

    # Committed files whose content decides what `pod install` produces.
    _POD_INPUTS = ("ios/Podfile", "ios/Podfile.lock", "Podfile", "Podfile.lock",
                   "package.json", "yarn.lock", "package-lock.json", "patches")
    _POD_STAMP = ".platform_pod_inputs"

    def _pod_inputs_fingerprint(self, repo_path: str) -> Optional[str]:
        """What the Pods were generated from, or None when there is nothing to key on.

        Podfile.lock is keyed by its COMMITTED content: pod install rewrites it
        (main's committed lock pins AppAuth 1.7.5, the Podfile resolves 1.7.6)
        and every checkout restores it, so the on-disk file differs on every
        Execute even though the Pods would come out identical.

        Everything else is keyed by its ON-DISK content: fix_rn_compatibility
        pins package.json locally (gesture-handler ^2.20.2 -> 2.9.0 for RN 0.68)
        and reinstalls node_modules. Committed ids cannot see that, and Pods
        generated for 2.20.2 then point at files 2.9.0 does not have ("Build
        input file cannot be found: .../react-native-gesture-handler/apple/...").
        """
        import hashlib
        parts = []
        for rel in self._POD_INPUTS:
            path = os.path.join(repo_path, rel)
            if rel.endswith("Podfile.lock"):
                r = subprocess.run(["git", "-C", repo_path, "rev-parse", f"HEAD:{rel}"],
                                   capture_output=True, text=True, timeout=10)
                parts.append(f"{rel}@HEAD="
                             f"{(r.stdout or '').strip() if r.returncode == 0 else '-'}")
                continue
            files = ([os.path.join(path, n) for n in sorted(os.listdir(path))]
                     if os.path.isdir(path) else [path])
            h = hashlib.sha256()
            seen = False
            for f in files:
                try:
                    with open(f, "rb") as fh:
                        h.update(os.path.basename(f).encode() + b"\0" + fh.read())
                    seen = True
                except OSError:
                    continue
            parts.append(f"{rel}={h.hexdigest() if seen else '-'}")
        return "\n".join(parts) if any(not p.endswith("=-") for p in parts) else None

    def _pod_install(self, repo_path: str) -> Tuple[bool, str]:
        """`pod install`, skipped when the Pods were built from the same inputs
        (see _pod_inputs_fingerprint). The source tweaks that normally ride along with an install are
        still applied, because a checkout has just reverted them."""
        repo_path = os.path.abspath(repo_path)
        fp = self._pod_inputs_fingerprint(repo_path)
        for pod_dir in (os.path.join(repo_path, "ios"), repo_path):
            if os.path.exists(os.path.join(pod_dir, "Podfile")):
                break
        else:
            return self._pod_install_uncached(repo_path)
        stamp = os.path.join(pod_dir, "Pods", self._POD_STAMP)
        manifest = os.path.join(pod_dir, "Pods", "Manifest.lock")
        try:
            stamped = open(stamp).read() if os.path.isfile(stamp) else None
        except OSError:
            stamped = None
        if (fp and stamped == fp and os.path.isfile(manifest)
                and os.path.isdir(os.path.join(pod_dir, "Pods", "Pods.xcodeproj"))):
            # Xcode's '[CP] Check Pods Manifest.lock' phase requires the two to be
            # identical; the checkout restored the committed lock, so re-sync it
            # locally (a tracked file, reverted by the next checkout -- never pushed).
            shutil.copyfile(manifest, os.path.join(pod_dir, "Podfile.lock"))
            self._silence_logbox(repo_path)
            self._expose_table_sheet(repo_path)
            self._expose_assign_table_sheet(repo_path)
            logger.info("Pods unchanged since last install (same inputs) "
                        "— skipping pod install")
            return True, self._verify_pods(pod_dir, "Pods already installed (same inputs).")

        ok, out = self._pod_install_uncached(repo_path)
        if ok and fp:
            try:
                with open(stamp, "w") as f:
                    f.write(fp)
            except OSError as e:
                logger.warning("could not record pod inputs: %s", e)
        return ok, out

    def _pod_install_uncached(self, repo_path: str) -> Tuple[bool, str]:
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
            # Without these the waiter segment cannot commit a table — neither
            # sheet's commit button is reachable by any tool. Two DIFFERENT
            # components: "Select A Table" (Table/Table2) and the one the waiter
            # actually gets, "Please assign a table" (TableView).
            self._expose_table_sheet(repo_path)
            self._expose_assign_table_sheet(repo_path)

            logger.info(f"Running pod install in {candidate}")
            ok, out = _run(["pod", "install"], cwd=candidate, timeout=POD_TIMEOUT)
            if ok:
                return True, self._verify_pods(candidate, out)

            # A stale local spec repo (or a Podfile whose constraints moved) makes
            # a plain `pod install` fail — CocoaPods itself tells you to retry with
            # --repo-update. Do it automatically instead of dead-ending the build.
            if "repo update" in out or "out-of-date source repos" in out:
                logger.warning("pod install failed on stale specs — retrying with --repo-update")
                ok, out = _run(
                    ["pod", "install", "--repo-update"], cwd=candidate, timeout=POD_REPO_UPDATE_TIMEOUT
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
                    ["pod", "install", "--repo-update"], cwd=candidate, timeout=POD_REPO_UPDATE_TIMEOUT
                )
                return ok, out

            return False, out

        return True, "No Podfile — CocoaPods not used."

    def build_ios(
        self, repo_path: str, force: bool = False, device_id: Optional[str] = None,
        bundle_id: Optional[str] = None, env_config=None,
    ) -> BuildResult:
        """Build a simulator .app via xcodebuild.

        *device_id* is the target simulator's UDID. When supplied we build for
        that destination and the active architecture only. Building the
        `generic/platform=iOS Simulator` destination instead forces xcodebuild to
        produce *every* simulator slice — including x86_64, which is useless on
        Apple Silicon, doubles the build, and is the slice where older React
        Native dependencies tend to fail to compile.

        *bundle_id* is the bundle id the finished .app MUST carry. *env_config* is an
        environments.EnvironmentConfig carrying the whole variant (bundle id, product
        name, configuration, extra build settings); it supersedes *bundle_id*, which
        stays for callers that only know the id.

        Both exist because the prod/staging difference lived in local commits that
        were never pushed — so a re-clone silently built a *prod* artifact from the
        staging branch, which then failed the staging scenario's preflight as
        "…staging is not installed". Passing the variant here as xcodebuild SETTING=
        VALUE overrides makes it a reproducible property of configuration, and leaves
        the application checkout untouched: Info.plist already reads
        $(PRODUCT_BUNDLE_IDENTIFIER), so no file in the repo is modified.
        """
        # env_config wins; bundle_id is the narrower, older way to say the same thing.
        env_settings: List[str] = []
        configuration = "Debug"
        env_name = None
        if env_config is not None:
            bundle_id = env_config.bundle_id or bundle_id
            configuration = env_config.configuration or "Debug"
            env_name = env_config.environment
            env_settings = env_config.xcodebuild_settings()
        elif bundle_id:
            # Resolve the full variant from config rather than passing the id as a
            # global xcodebuild override: that override reaches EVERY target,
            # including the CocoaPods resource bundles, which is what crashed
            # PaymentSheet on launch (see EnvironmentConfig.xcodebuild_settings).
            try:
                from automation.projects import environments as _env
                _envname = _env.environment_for_bundle(bundle_id)
                env_config = (_env.resolve(_envname, bundle_id=bundle_id)
                              if _envname else None)
            except Exception as e:
                logger.debug("no environment config for %s: %s", bundle_id, e)
                env_config = None
            if env_config is not None:
                configuration = env_config.configuration or "Debug"
                env_name = env_config.environment
                env_settings = env_config.xcodebuild_settings()
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

        # A space anywhere above this checkout breaks React Native's own build
        # tooling in ways this platform cannot repair. Say so now rather than
        # twenty minutes into a build, in an error naming a dependency.
        space = self.path_has_space(repo_path)
        if space:
            return BuildResult(ok=False, error=space)

        # Frameworks this SDK has removed cannot be patched, so this only
        # names the pod -- but naming it is the whole point: the compiler error
        # points at Apple's own header and mentions no dependency.
        for warning in self.removed_framework_users(repo_path):
            logger.warning("[removed framework] %s", warning)

        # RN's script phases run `nvm use default`; an nvm without one fails them.
        from automation.projects.macos_environment import ensure_nvm_default
        nvm_msg = ensure_nvm_default()
        if nvm_msg:
            logger.info(nvm_msg)

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

        # One derived-data dir PER ENVIRONMENT. A single shared build/ios meant a
        # staging build overwrote the production Products dir (and vice versa), so
        # whichever ran last was the only artifact that existed -- and the next
        # request for the other variant silently "reused" it. Keying the path by
        # environment lets both exist side by side and be matched on identity.
        # Unspecified environment keeps the historical path, so nothing that already
        # points at build/ios breaks.
        derived = os.path.join(repo_path, "build",
                               f"ios-{env_name}" if env_name else "ios")
        head_file = os.path.join(derived, ".built_head")
        cur_head = self._git_head(repo_path)

        # Reuse a previous build ONLY if the checkout hasn't changed since it was
        # built. Reusing blindly (the old behaviour) silently shipped the stale
        # binary after a `git pull` or a code edit — a re-prepare looked done but
        # ran the previous version. Comparing the built commit to HEAD fixes that.
        if not force:
            existing = self._find_built_app(derived, configuration=configuration,
                                            bundle_id=bundle_id)
            built_head = None
            try:
                if os.path.exists(head_file):
                    built_head = open(head_file).read().strip()
            except Exception:
                pass
            existing_bid = self._bundle_id(existing) if existing else None
            # A matching HEAD is NOT enough when a variant was asked for: the same
            # commit builds prod or staging depending only on the bundle-id override,
            # so reusing on HEAD alone hands back a prod .app for a staging request --
            # which is exactly the silent mismatch that failed the staging preflight.
            # JS-only reuse needs a Debug build: Release embeds the JS bundle.
            same_native = (built_head == cur_head) or (
                configuration.lower() == "debug"
                and self._only_js_changed(repo_path, built_head, cur_head))
            if (existing and cur_head and built_head and same_native
                    and (not bundle_id or existing_bid == bundle_id)):
                if built_head == cur_head:
                    logger.info(f"Reusing iOS build (HEAD unchanged @ {cur_head[:8]})")
                else:
                    logger.info(f"Reusing iOS build: {built_head[:8]} → {cur_head[:8]} "
                                f"changes JavaScript only; Metro serves it")
                # A Debug app reads its JS from Metro at run time, so the
                # environment's JS edits (staging API host) must be on disk even
                # though nothing is compiled.
                with self._env_source_edits(repo_path, env_config):
                    pass
                return BuildResult(
                    ok=True, artifact_path=existing, bundle_id=existing_bid
                )
            if existing and bundle_id and existing_bid != bundle_id:
                logger.info("Rebuilding: existing artifact is %s but %s was requested",
                            existing_bid, bundle_id)
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
            "-scheme", env_config.scheme if (env_config and env_config.scheme) else scheme,
            "-configuration", configuration,
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
        # Apply the environment's build settings (see the docstring). Inserted before
        # the `build` action -- xcodebuild only accepts SETTING=VALUE overrides ahead
        # of the action, and silently ignores anything after it.
        for setting in env_settings:
            cmd.insert(-1, setting)
        logger.info(f"Building iOS: {' '.join(cmd)}")
        with self._env_source_edits(repo_path, env_config):
            ok, out = _run(cmd, cwd=repo_path)
        if not ok:
            return BuildResult(
                ok=False,
                error=(f"xcodebuild failed.\n{_summarize_xcode_errors(out)}"
                       + ("".join(f"\n\n[platform] {w}" for w in
                                  self.removed_framework_users(repo_path))))
            )

        app = self._find_built_app(derived, configuration=configuration)
        if not app:
            return BuildResult(
                ok=False,
                error="xcodebuild reported success but no .app was produced under "
                      f"{derived}/Build/Products/{configuration}-iphonesimulator.",
            )
        # Record the commit this build was made from, so the next prepare can tell
        # whether a rebuild is needed (see the reuse check above).
        try:
            if cur_head:
                with open(head_file, "w") as f:
                    f.write(cur_head)
        except Exception:
            pass

        # ── Validate the artifact BEFORE anyone calls this a success ──────────
        # "BUILD SUCCESS" must mean the artifact is the one that was asked for, not
        # merely that xcodebuild exited 0. Deploying a production .app in answer to a
        # staging request is the failure this whole module exists to prevent, so the
        # identity check is part of the build, not of the caller.
        valid, why = self.validate_artifact(app, expected_bundle_id=bundle_id,
                                            environment=env_name)
        if not valid:
            return BuildResult(ok=False, error=why)

        got = self._bundle_id(app)
        self._write_artifact_metadata(app, repo_path=repo_path, commit=cur_head,
                                      environment=env_name, bundle_id=got,
                                      configuration=configuration)
        return BuildResult(ok=True, artifact_path=app, bundle_id=got)

    @contextlib.contextmanager
    def _env_source_edits(self, repo_path: str, env_config):
        """Apply an environment's source_replacements for the build, then revert.

        Needed because not every environment difference is an xcodebuild setting.
        This app picks its API host by which line of a JS config file is commented
        out, so a staging build with only a bundle-id override still talks to
        PRODUCTION -- it installs and runs, then shows an empty home screen because
        the test data is on the staging server. That reads as a broken app.

        The edits live in the build workspace only: the original bytes are restored
        in a finally block, so the application checkout is identical afterwards even
        if the build raises or is killed.

        EXCEPT for a Debug build, which embeds no JS bundle and fetches it from Metro
        at RUN time -- reverting before the run would hand the app the production
        config the edit exists to replace, and the change would never take effect.
        For those the edit is left in place (the file is restored by the next
        _reset_worktree, which every checkout/pull already performs).
        """
        edits = list(getattr(env_config, "source_replacements", None) or [])
        keep = self._runtime_bundled_from_metro(repo_path, env_config)
        originals: Dict[str, str] = {}
        try:
            for e in edits:
                path = os.path.join(repo_path, e["file"])
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                if e["find"] not in text:
                    # Already applied is fine; genuinely absent is not -- silently
                    # building the wrong variant is the failure being prevented.
                    if e["replace"] in text:
                        continue
                    raise RuntimeError(
                        f"{e['file']} does not contain the expected line for this "
                        f"environment:\n  {e['find']}\nThe upstream file changed; "
                        f"update source_replacements in project-environments.json.")
                originals[path] = text
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text.replace(e["find"], e["replace"]))
                logger.info("env edit: %s -> %s", e["file"], e["replace"][:80])
            yield
        finally:
            for path, text in originals.items():
                # `keep` covers files the RUNNING app reads (JS fetched from Metro on
                # launch). Build inputs are not those: a .pbxproj is consumed by
                # xcodebuild and never read again, so keeping it edited only leaves
                # the checkout dirty -- and the next PRODUCTION build then finds no
                # production line to replace and silently builds the wrong variant.
                if keep and not _is_build_input(path):
                    logger.info("env edit kept in the workspace (%s): this Debug "
                                "build loads its JS from Metro at run time, so "
                                "reverting now would serve the app the un-replaced "
                                "config.", os.path.basename(path))
                    continue
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(text)
                except OSError as err:
                    logger.error("could not restore %s: %s", path, err)

    @staticmethod
    def _runtime_bundled_from_metro(repo_path: str, env_config) -> bool:
        """True when the app will fetch its JS at run time instead of embedding it.

        A Debug React Native build ships no main.jsbundle: the app asks Metro for it
        on launch, so any source edit must still be on disk THEN, not only during
        xcodebuild.
        """
        cfg = (getattr(env_config, "configuration", None) or "Debug").lower()
        if cfg != "debug":
            return False
        return os.path.isfile(os.path.join(repo_path, "package.json"))

    # ── artifact identity ────────────────────────────────────────────────────

    METADATA_NAME = ".artifact.json"

    def _write_artifact_metadata(self, app_path: str, *, repo_path: str,
                                 commit: Optional[str], environment: Optional[str],
                                 bundle_id: Optional[str],
                                 configuration: str) -> Optional[str]:
        """Record WHAT this artifact is, next to the artifact itself.

        Without this an .app is anonymous: the platform could only ask "is there a
        .app?" and "what bundle id does it carry?", never "which branch/commit/
        environment produced it?". Stored beside the bundle (not inside it) so it
        cannot alter the app's own contents or signature.
        """
        meta = {
            "bundle_id": bundle_id,
            "environment": environment,
            "configuration": configuration,
            "branch": self._git_branch(repo_path),
            "commit": commit,
            "built_at": datetime.utcnow().isoformat() + "Z",
            "artifact": os.path.basename(app_path),
            "xcode": self._xcode_version(),
            "version": self.app_version(app_path),
        }
        path = os.path.join(os.path.dirname(app_path),
                            os.path.basename(app_path) + self.METADATA_NAME)
        try:
            with open(path, "w") as f:
                json.dump(meta, f, indent=2)
            return path
        except OSError as e:
            # Metadata is an aid, not a gate: a build that produced a valid artifact
            # must not fail because a sidecar could not be written.
            logger.warning("Could not write artifact metadata %s: %s", path, e)
            return None

    def artifact_metadata(self, app_path: str) -> Dict[str, Any]:
        """Recorded identity for an artifact, or {} when it predates metadata."""
        path = os.path.join(os.path.dirname(app_path),
                            os.path.basename(app_path) + self.METADATA_NAME)
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _git_branch(self, repo_path: str) -> Optional[str]:
        try:
            import subprocess
            r = subprocess.run(["git", "-C", repo_path, "rev-parse",
                                "--abbrev-ref", "HEAD"],
                               capture_output=True, text=True, timeout=10)
            return (r.stdout or "").strip() or None
        except Exception:
            return None

    def _xcode_version(self) -> Optional[str]:
        try:
            ok, out = _run(["xcodebuild", "-version"], timeout=30)
            return (out or "").splitlines()[0].strip() if ok and out else None
        except Exception:
            return None

    def validate_artifact(self, app_path: str, *,
                          expected_bundle_id: Optional[str] = None,
                          environment: Optional[str] = None) -> Tuple[bool, str]:
        """Independently verify an artifact before it is trusted or deployed.

        Checks structure (bundle, Info.plist, executable), that the executable is a
        real Mach-O, and — the point of the exercise — that its identity matches the
        environment that was requested.
        """
        if not app_path or not os.path.isdir(app_path):
            return False, f"Artifact is missing: {app_path}"
        plist = os.path.join(app_path, "Info.plist")
        if not os.path.isfile(plist):
            return False, f"Artifact has no Info.plist: {app_path}"
        if not self._is_complete_app(app_path):
            return False, (f"Artifact is incomplete (no bundle id, or its executable "
                           f"is missing): {app_path}")

        got = self._bundle_id(app_path)
        if expected_bundle_id and got != expected_bundle_id:
            return False, (
                "BUILD VALIDATION FAILED\n\n"
                f"Requested environment:\n    {environment or '?'}\n\n"
                f"Expected bundle ID:\n    {expected_bundle_id}\n\n"
                f"Actual bundle ID:\n    {got}\n\n"
                "A different variant was produced than the one requested.\n\n"
                "Deployment blocked.")

        # Mach-O check. A truncated or non-binary executable installs and then fails
        # at launch with nothing useful in the log, so catch it here.
        try:
            with open(plist, "rb") as f:
                executable = plistlib.load(f).get("CFBundleExecutable")
            exe = os.path.join(app_path, executable or "")
            with open(exe, "rb") as f:
                magic = f.read(4)
            # Mach-O 32/64, either endianness, or a fat/universal archive.
            if magic not in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
                             b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce",
                             b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
                return False, (f"Artifact executable is not a Mach-O binary "
                               f"(magic {magic!r}): {exe}")
        except Exception as e:
            return False, f"Could not read the artifact executable: {e}"

        return True, "Artifact validated."

    # Anything here changes the compiled binary; everything else in a React
    # Native checkout is JavaScript/assets that a Debug app fetches from Metro.
    _NATIVE_PREFIXES = ("ios/", "android/", "patches/")
    _NATIVE_FILES = ("package.json", "yarn.lock", "package-lock.json",
                     ".yarnrc.yml", "app.json", "react-native.config.js")

    def _only_js_changed(self, repo_path: str, old: Optional[str],
                         new: Optional[str]) -> bool:
        """True when old..new touches no native input, so the built .app still fits.

        Switching to an already-built branch used to recompile the whole app
        (15+ min on Intel) even when the branches differ only in JS. Anything
        uncertain -- unknown commit, git error, a Release build that embeds its
        JS -- answers False and rebuilds.
        """
        if not old or not new or old == new:
            return False
        r = subprocess.run(["git", "-C", repo_path, "diff", "--name-only", old, new],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return False
        for path in filter(None, r.stdout.splitlines()):
            if (path.startswith(self._NATIVE_PREFIXES)
                    or os.path.basename(path) in self._NATIVE_FILES
                    or path.endswith(".podspec")):
                return False
        return True

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

    def _find_built_app(self, derived: str, configuration: str = "Debug",
                        bundle_id: Optional[str] = None) -> Optional[str]:
        """The built .app under *derived*, optionally the one carrying *bundle_id*.

        Selection used to be `complete[0]` over a sorted glob -- i.e. alphabetical,
        which is neither "the newest" nor "the right variant". With two .apps in one
        Products dir that silently returned whichever sorted first, so a staging
        request could be answered with a production artifact. When a bundle id is
        known, match on it; otherwise prefer the most recently built.
        """
        pattern = os.path.join(derived, "Build", "Products",
                               f"{configuration}-iphonesimulator", "*.app")
        complete = [m for m in glob.glob(pattern) if self._is_complete_app(m)]
        if not complete:
            return None
        if bundle_id:
            matching = [m for m in complete if self._bundle_id(m) == bundle_id]
            if not matching:
                return None
            complete = matching
        return max(complete, key=lambda p: os.path.getmtime(p))

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
        bundle_id: Optional[str] = None,
        env_config=None,
    ) -> BuildResult:
        """Build the app for *platform* ("ios" | "android").

        *bundle_id* / *env_config* pin the variant the artifact must be (iOS only);
        see build_ios.
        """
        if platform == "ios":
            return self.build_ios(repo_path, force, device_id=device_id,
                                  bundle_id=bundle_id, env_config=env_config)
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

    def reap_orphaned_metros(self) -> int:
        """Kill Metro bundlers whose repo directory no longer exists.

        Metro holds its project root open at startup, so deleting (or re-cloning)
        a repo underneath a running packager does NOT stop it: the process keeps
        serving and keeps answering /status with 'packager-status:running'. Every
        bundle request then fails with 'Unable to resolve module ./index from
        <deleted path>' — a red screen that reads like a broken app but is really
        a stale process. _metro_running() cannot catch this: liveness and
        correctness look identical over /status, so the repo path is checked here
        instead, against the process's own command line.

        Returns the number of bundlers killed.
        """
        import signal as _signal
        killed = 0
        try:
            pids = subprocess.run(
                ["pgrep", "-f", "react-native start"],
                capture_output=True, text=True, timeout=10,
            ).stdout.split()
        except Exception as e:
            logger.debug("orphan reaper: could not list packagers: %s", e)
            return 0

        for pid in pids:
            try:
                # The process's own cwd IS its project root — the same authority
                # _kill_metro_for_repo uses, and it stays correct for a deleted
                # directory (lsof reports the path with a ' (deleted)' suffix).
                out = subprocess.run(
                    ["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                    capture_output=True, text=True, timeout=10,
                ).stdout
            except Exception:
                continue

            path = next((l[1:].strip() for l in out.splitlines() if l.startswith("n")), "")
            if not path or "/repos/" not in path:
                continue  # not one of our app packagers
            gone = path.endswith("(deleted)")
            path = path.replace(" (deleted)", "").strip()
            if not gone and os.path.isdir(path):
                continue  # repo still there — healthy, leave it alone

            try:
                os.kill(int(pid), _signal.SIGKILL)
                killed += 1
                logger.warning(
                    "Metro watchdog: killed orphaned bundler pid=%s — its repo %s no "
                    "longer exists (it was still answering /status and serving red "
                    "screens). ensure_metro() will cold-start a correct one.", pid, path,
                )
            except Exception as e:
                logger.debug("orphan reaper: could not kill %s: %s", pid, e)
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
                # First clear packagers whose repo was deleted or re-cloned. They
                # still answer /status, so the liveness check below would treat
                # one as healthy and never restart it, while the app red-screens
                # on every bundle request.
                try:
                    app_builder.reap_orphaned_metros()
                except Exception as e:
                    logger.debug("metro watchdog: orphan reap failed: %s", e)

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
