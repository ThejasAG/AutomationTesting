"""Single source of truth for the WebDriverAgent lifecycle.

Every XCUITest session on this platform gets its WDA configuration from here.
Before this module there were five independent ones, and they disagreed:

    scenario.py             hardcoded ~/.appium/.../node_modules, useNewWDA=False,
                            no wdaLocalPort (so Appium defaulted it to 8100)
    cross_app_orchestrator  usePrebuiltWDA=True but NO derivedDataPath at all,
                            explicit wdaLocalPort 8100/8101, no useNewWDA
    intelligence.py         usePrebuiltWDA=True, no derivedDataPath
    recorder.py             usePrebuiltWDA=True, no derivedDataPath
    manager.py              glob over ~/Library/Developer/Xcode/DerivedData

That last one is why this module validates rather than just globs. On this
machine the glob's only hit is a DerivedData dir whose runner .app contains a
PlugIns/ directory and nothing else — no Info.plist, no Mach-O binary. It looks
like a build to `os.path.isdir` and is unusable to Xcode, so the "discovery"
handed Appium a broken WDA while the working build sat at the path only
scenario.py knew about. Existence is not validity; see _is_usable_build.

Resolution precedence (Phase 8):

    1. WDA_DERIVED_DATA           explicit override, still validated
    2. cached path                if the toolchain fingerprint still matches
    3. dynamic discovery          driver's own build dir, then Xcode DerivedData
    4. build once                 `appium driver run xcuitest build-wda`

WDA compatibility tracks the AUTOMATION TOOLCHAIN (xcuitest driver +
appium-webdriveragent versions), never the application under test. Shipping a
new build of the Vya app does not invalidate WDA.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Appium's own default. Kept here so it is stated exactly once.
DEFAULT_WDA_PORT = int(os.getenv("WDA_LOCAL_PORT", "8100"))

# A cold `build-wda` measured ~188s on this Intel Mac, which is why the launch
# ceiling is 360s rather than Appium's 60s default — a build that overruns makes
# Appium drop the session-creation connection and the client sees
# "RemoteDisconnected" with no explanation.
WDA_LAUNCH_TIMEOUT_MS = int(os.getenv("WDA_LAUNCH_TIMEOUT_MS", "360000"))

_APPIUM_HOME = os.path.expanduser(os.getenv("APPIUM_HOME", "~/.appium"))
_XCUITEST_ROOT = os.path.join(_APPIUM_HOME, "node_modules", "appium-xcuitest-driver")
_CACHE_FILE = os.path.expanduser("~/.appium/.vya-wda-cache.json")

_RUNNER_REL = os.path.join("Build", "Products", "Debug-iphonesimulator",
                           "WebDriverAgentRunner-Runner.app")


@dataclass
class WdaBuild:
    derived_data: str          # the -derivedDataPath root, what Appium wants
    source: str                # how it was found, for the log line
    fingerprint: str           # toolchain this build belongs to


def _pkg_version(*parts: str) -> str:
    """Version from a package.json, or "" — never raises."""
    try:
        with open(os.path.join(*parts, "package.json")) as f:
            return str(json.load(f).get("version") or "")
    except Exception:
        return ""


def toolchain_fingerprint() -> str:
    """What a WDA build must match to be reusable.

    Read from package.json rather than shelling out to `appium --version`:
    same answer, no subprocess, and this runs on every session creation.
    Deliberately excludes the app under test — see the module docstring.
    """
    xcuitest = _pkg_version(_XCUITEST_ROOT) or "unknown"
    wda_src = _pkg_version(_XCUITEST_ROOT, "node_modules", "appium-webdriveragent") or "unknown"
    return f"xcuitest={xcuitest};webdriveragent={wda_src}"


def _is_usable_build(derived_data: str) -> bool:
    """True only if this DerivedData holds a COMPLETE runner.

    isdir() on the .app is not enough — an interrupted xcodebuild leaves the
    bundle behind with only PlugIns/ inside. Appium accepts the path, then the
    launch fails with no useful error. Check the pieces Xcode actually needs:
    the Info.plist, the runner's Mach-O, and the xctest bundle's own binary.
    """
    if not derived_data:
        return False
    app = os.path.join(derived_data, _RUNNER_REL)
    needed = (
        os.path.join(app, "Info.plist"),
        os.path.join(app, "WebDriverAgentRunner-Runner"),
        os.path.join(app, "PlugIns", "WebDriverAgentRunner.xctest",
                     "WebDriverAgentRunner"),
    )
    return all(os.path.exists(p) for p in needed)


def _discover() -> list:
    """Candidate DerivedData roots, best first. Order matters: the driver's own
    build dir is the one `appium driver run xcuitest build-wda` writes to."""
    out = [os.path.join(_XCUITEST_ROOT, "node_modules")]
    try:
        import glob
        hits = glob.glob(os.path.expanduser(
            "~/Library/Developer/Xcode/DerivedData/WebDriverAgent-*"))
        out += sorted(hits, key=os.path.getmtime, reverse=True)
    except Exception:
        pass
    return out


def _read_cache() -> Optional[dict]:
    try:
        with open(_CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(build: WdaBuild) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        with open(_CACHE_FILE, "w") as f:
            json.dump({"derived_data": build.derived_data,
                       "fingerprint": build.fingerprint,
                       "stamped_at": time.strftime("%Y-%m-%dT%H:%M:%S")}, f)
    except Exception as e:
        logger.debug("[WDA] could not write cache: %s", e)


def build_wda(timeout: int = 900) -> bool:
    """Compile WDA once, through the installed driver so it lands in the right
    place with the right toolchain. Returns whether a usable build resulted."""
    logger.info("[WDA] No compatible WDA build found")
    logger.info("[WDA] Building WDA (this takes minutes on a cold machine)...")
    try:
        p = subprocess.run(["appium", "driver", "run", "xcuitest", "build-wda"],
                           capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        logger.error("[WDA] Build failed: `appium` is not on PATH")
        return False
    except subprocess.TimeoutExpired:
        logger.error("[WDA] Build timed out after %ss", timeout)
        return False
    if p.returncode != 0:
        logger.error("[WDA] Build failed (exit %s): %s",
                     p.returncode, (p.stderr or p.stdout or "")[-400:])
        return False
    logger.info("[WDA] Build completed successfully")
    return True


_resolved: Optional[WdaBuild] = None


def resolve(allow_build: bool = True, refresh: bool = False) -> Optional[WdaBuild]:
    """The compatible WDA build for the installed toolchain, or None.

    Cached per process AND on disk. A toolchain upgrade changes the fingerprint,
    which invalidates the disk cache — that is what stops a stale build from
    being reused after `appium driver update`.
    """
    global _resolved
    if _resolved and not refresh:
        return _resolved

    fp = toolchain_fingerprint()
    logger.info("[WDA] Resolving compatible WDA build...")
    logger.info("[WDA] Installed toolchain: %s", fp)

    # 1. explicit override — still validated, never trusted blindly
    override = os.getenv("WDA_DERIVED_DATA")
    if override:
        override = os.path.expanduser(override)
        if _is_usable_build(override):
            _resolved = WdaBuild(override, "WDA_DERIVED_DATA", fp)
            logger.info("[WDA] Compatible prebuilt WDA found: %s (WDA_DERIVED_DATA)", override)
            _write_cache(_resolved)
            return _resolved
        logger.warning("[WDA] WDA_DERIVED_DATA is set but holds no usable build: %s", override)

    # 2. cached path, only if the toolchain has not moved under it
    cached = _read_cache()
    if cached and cached.get("fingerprint") == fp and _is_usable_build(cached.get("derived_data", "")):
        _resolved = WdaBuild(cached["derived_data"], "cache", fp)
        logger.info("[WDA] Compatible prebuilt WDA found: %s (cached)", _resolved.derived_data)
        return _resolved
    if cached and cached.get("fingerprint") != fp:
        logger.info("[WDA] Cached build was for %s — toolchain is now %s, rebuilding",
                    cached.get("fingerprint"), fp)

    # 3. dynamic discovery
    logger.info("[WDA] Searching for compatible DerivedData...")
    for cand in _discover():
        if _is_usable_build(cand):
            _resolved = WdaBuild(cand, "discovered", fp)
            logger.info("[WDA] Compatible prebuilt WDA found: %s", cand)
            _write_cache(_resolved)
            return _resolved
        if os.path.isdir(os.path.join(cand, _RUNNER_REL)):
            logger.warning("[WDA] Ignoring INCOMPLETE build at %s "
                           "(runner bundle present but missing its binary)", cand)

    # 4. build once
    if allow_build and build_wda():
        for cand in _discover():
            if _is_usable_build(cand):
                _resolved = WdaBuild(cand, "built", fp)
                logger.info("[WDA] DerivedData cached at %s", cand)
                _write_cache(_resolved)
                return _resolved

    logger.error("[WDA] No usable WebDriverAgent build could be resolved. "
                 "Build one with: appium driver run xcuitest build-wda")
    return None


# ── ports ────────────────────────────────────────────────────────────────────
# One WDA per simulator, so the port must be per-device. Cross-app already had
# this right (8100 consumer / 8101 business); this keeps those exact numbers so
# the working demo is unaffected, while giving every other path the same rule
# instead of silently falling through to Appium's 8100 default.
_ports: Dict[str, int] = {}


def port_for(udid: str, preferred: Optional[int] = None) -> int:
    """A stable WDA port for *udid*. Same device always gets the same port."""
    if not udid:
        return preferred or DEFAULT_WDA_PORT
    if preferred is not None:
        _ports[udid] = preferred
        return preferred
    if udid not in _ports:
        _ports[udid] = DEFAULT_WDA_PORT + len(_ports)
    return _ports[udid]


def is_healthy(port: int, timeout: float = 3.0) -> bool:
    """Does a live WDA answer /status on this port?

    Only a well-formed WDA reply counts. A port that merely accepts connections
    proves nothing — something unrelated may hold it, and treating that as
    "WDA is up" is how a run ends up talking to the wrong process.
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=timeout) as r:
            if r.status != 200:
                return False
            body = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return False
    value = body.get("value") or {}
    return bool(value.get("build") or value.get("os") or value.get("ios"))


def apply(opts, udid: str = "", wda_port: Optional[int] = None,
          launch_timeout_ms: int = WDA_LAUNCH_TIMEOUT_MS):
    """Put the resolved WDA configuration onto an XCUITestOptions.

    The one function every execution path calls. Sets derivedDataPath only when
    a VALIDATED build exists — setting it to a broken path is worse than leaving
    it unset, because Appium then trusts it instead of building.
    """
    port = port_for(udid, wda_port)
    opts.set_capability("wdaLocalPort", port)
    opts.set_capability("wdaLaunchTimeout", launch_timeout_ms)

    build = resolve()
    if build:
        opts.set_capability("derivedDataPath", build.derived_data)
        opts.set_capability("usePrebuiltWDA", True)
        # Reuse a healthy running WDA rather than tearing it down and paying the
        # launch again. Only claimed when the endpoint actually answers.
        if is_healthy(port):
            logger.info("[WDA] Existing WDA health check passed on port %s", port)
            logger.info("[WDA] Reusing existing WDA")
            opts.set_capability("useNewWDA", False)
        else:
            logger.info("[WDA] No healthy WDA on port %s — launching prebuilt build", port)
            opts.set_capability("useNewWDA", False)
    else:
        # No validated build: let Appium do its own thing rather than point it
        # at something that does not work.
        logger.warning("[WDA] Proceeding without a resolved derivedDataPath — "
                       "Appium will build WebDriverAgent itself")
    return opts
