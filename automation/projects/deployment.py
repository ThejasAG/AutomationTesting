"""Make a scenario's required apps actually BE on the devices it will drive.

A cross-app flow declares what it needs implicitly: a bundle id per role (from
ENV_BUNDLES) and a device per role (from cross_app_config.json). Preflight used to
only *check* that pairing and then tell the user to go deploy by hand -- even though
the platform already knows how to build and install. Everything here is resolution
plus the builder's existing primitives; nothing about any specific app, device or
Mac is written down.

The pipeline per required app:

    bundle id -> project row carrying that bundle id -> its checkout
              -> build artifact (built if absent/stale/wrong-variant)
              -> assigned device -> install -> read the id back off the device

The last step is the point. `simctl install` exits 0 in cases where the app is not
usable afterwards, so an install is only believed once the device itself reports the
bundle id present.
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from automation.database.config import SessionLocal
from automation.database.models import TestProject
from automation.projects.builder import app_builder
from automation.projects.repository import repository_manager

logger = logging.getLogger("deployment")


class DeploymentBlocked(RuntimeError):
    """A required app cannot be put on its device, and no retry will change that.

    Carries the whole picture (app, bundle, device, reason, action) because the
    failure this replaces -- "… is not installed on <udid>" -- named the symptom and
    left the user to work out which project, artifact or device was at fault.
    """


@dataclass
class AppRequirement:
    """One app a scenario needs, and where it needs it."""
    role: str                 # "consumer" | "business" | …  (label only)
    bundle_id: str            # what must be installed
    device_id: str            # the simulator it must be installed ON
    platform: str = "ios"


@dataclass
class DeploymentResult:
    role: str
    bundle_id: str
    device_id: str
    installed: bool = False
    action: str = "none"      # none | installed | reinstalled | already-present
    version: Optional[str] = None
    build: Optional[str] = None
    previous_version: Optional[str] = None
    detail: str = ""

    @property
    def summary(self) -> str:
        ver = f"{self.version or '?'} ({self.build or '?'})"
        return (f"{self.role}: {self.bundle_id} on {self.device_id[:8]}… "
                f"{'OK' if self.installed else 'MISSING'} {ver} [{self.action}]")


def _blocked(req: AppRequirement, reason: str, action: str) -> DeploymentBlocked:
    return DeploymentBlocked(
        "DEPLOYMENT BLOCKED\n\n"
        f"App:\n    {req.role}\n\n"
        f"Bundle ID:\n    {req.bundle_id}\n\n"
        f"Target device:\n    {req.device_id}\n\n"
        f"Reason:\n    {reason}\n\n"
        f"Action:\n    {action}"
    )


def project_for_bundle(bundle_id: str) -> Optional[dict]:
    """The project row configured to produce *bundle_id*.

    Matched on the project's own `app_bundle_id`, so which checkout builds which
    variant stays a property of project configuration. Exact match only: the prod
    bundle is a strict prefix of the staging one ('…vyaconsumer' vs
    '…vyaconsumerstaging'), so a prefix/`startswith` match would happily hand back
    the wrong project -- the same trap purge.py documents.
    """
    if not bundle_id:
        return None
    try:
        with SessionLocal() as db:
            p = (db.query(TestProject)
                   .filter(TestProject.app_bundle_id == bundle_id)
                   .first())
            if not p:
                return None
            return {"id": p.id, "name": p.name,
                    "platform": (p.platform or "ios").lower(),
                    "branch": p.default_branch or p.current_branch or "main",
                    "app_path": p.app_path}
    except Exception as e:
        # An unreachable project table must not surface as a SQLAlchemy traceback in
        # the middle of preflight. "No project configured" is the honest answer and
        # produces the DEPLOYMENT BLOCKED message, which names what to do.
        logger.warning("Could not look up the project for %s: %s", bundle_id, e)
        return None


def find_artifact(project: dict, bundle_id: str) -> Optional[str]:
    """An existing built .app for *bundle_id*, or None.

    Checks the recorded app_path first, then the project's derived-data tree. A
    recorded path is not trusted on its own: it can point at a previous machine's
    filesystem (the DB outlives a move) or at a prod build from before the variant
    was configured, so the bundle id is read out of whatever is found.
    """
    candidates: List[str] = []
    if project.get("app_path"):
        candidates.append(project["app_path"])
    try:
        import glob as _glob
        repo = repository_manager.get_repo_path(project["id"])
        # EVERY derived-data tree, not just build/ios. Builds are keyed by
        # environment (build/ios-staging, build/ios-production, …) so that a staging
        # build cannot overwrite a production one; a scan hardcoded to build/ios
        # therefore finds nothing for any variant, and the caller reports "no
        # artifact" for one that was just built successfully.
        for base in sorted(_glob.glob(os.path.join(repo, "build", "ios*"))):
            for root, dirs, _files in os.walk(os.path.join(base, "Build", "Products")):
                for d in dirs:
                    if d.endswith(".app"):
                        candidates.append(os.path.join(root, d))
    except Exception as e:
        logger.debug("artifact scan failed for %s: %s", project.get("id"), e)

    for path in candidates:
        if not path or not os.path.isdir(path):
            continue
        if app_builder._bundle_id(path) != bundle_id:
            continue
        # Bundle id is necessary but not sufficient. Validate the artifact before
        # offering it for deployment, so a truncated or half-built .app is rejected
        # here rather than installing and failing at launch.
        ok, _why = app_builder.validate_artifact(path, expected_bundle_id=bundle_id)
        if ok:
            return path
    return None


def artifact_identity(path: str) -> Dict[str, Optional[str]]:
    """What an on-disk artifact says it is (branch, commit, environment, …).

    Empty for artifacts built before metadata existed; callers must treat "unknown"
    as "cannot prove it matches", never as "matches".
    """
    meta = app_builder.artifact_metadata(path)
    return {
        "bundle_id": meta.get("bundle_id") or app_builder._bundle_id(path),
        "environment": meta.get("environment"),
        "branch": meta.get("branch"),
        "commit": meta.get("commit"),
        "built_at": meta.get("built_at"),
    }


def _probe_installed(device_id: str, bundle_id: str) -> bool:
    """Is *bundle_id* on *device_id*? Lets subprocess.TimeoutExpired PROPAGATE.

    The distinction matters: "simctl says no" means deploy, but "simctl did not
    answer" means the machine is busy and the caller should decide, not be told the
    app is missing. app_builder.is_installed() cannot express that — it returns a
    bare bool — so the probe is done here.
    """
    import subprocess
    got = subprocess.run(["xcrun", "simctl", "get_app_container", device_id, bundle_id],
                         capture_output=True, text=True, timeout=30)
    return got.returncode == 0 and bool((got.stdout or "").strip())


def _artifact_is_newer_than_install(artifact: str, req: "AppRequirement") -> bool:
    """Was *artifact* built after the copy currently on the device was installed?

    Catches the rebuild that keeps the same version but changes what the app does.
    Compares the artifact's executable mtime with the installed bundle's, both of
    which the filesystem gives us for free -- no extra tooling, and it works for any
    app whether or not it records build metadata.
    """
    import subprocess
    try:
        got = subprocess.run(["xcrun", "simctl", "get_app_container",
                              req.device_id, req.bundle_id, "app"],
                             capture_output=True, text=True, timeout=30)
        installed = (got.stdout or "").strip().splitlines()[-1] if got.returncode == 0 else ""
        if not installed or not os.path.isdir(installed):
            return False
        return os.path.getmtime(artifact) > os.path.getmtime(installed) + 1
    except Exception as e:
        # Unknown means "do not churn": an unnecessary reinstall wipes app state.
        logger.debug("build-time comparison failed for %s: %s", req.bundle_id, e)
        return False


def _grant_device_permissions(req: "AppRequirement", say=None) -> None:
    """Give a freshly-deployed app the device permissions its screens depend on.

    The Vya home list is GPS-gated: with no location permission the app sends a null
    location and the backend returns ZERO restaurants, so Home renders "NO DATA
    FOUND" and every step that looks for a restaurant card fails against a screen
    that is working exactly as designed.

    app_builder already knows how to do this (_configure_ios_location), but only
    calls it from launch() -- which the deploy path never goes through, so an
    auto-deployed app never got it. Permissions are per BUNDLE ID, so a new variant
    starts with none even where its production twin is fully granted.

    Best-effort: a simulator that refuses a grant must not fail the deployment.
    """
    if req.platform != "ios":
        return
    try:
        app_builder._configure_ios_location(req.device_id, req.bundle_id)
        if say:
            say(f"{req.role}: granted location + pinned simulator coordinates")
    except Exception as e:                      # never fatal
        logger.debug("permission grant failed for %s: %s", req.bundle_id, e)
    # Every other privacy prompt too. Each one is a SYSTEM sheet (a separate
    # process): the app's accessibility tree disappears behind it, so a flow sees
    # only the status bar and stalls. Signing the consumer in on a fresh install
    # raised iOS's "How do you want to share contacts?" and blocked @consumer_home.
    # A test simulator answering "allow" up front is the intended setup.
    try:
        # `all` did not cover contacts on the iOS 26.5 runtime: the share-contacts
        # sheet still appeared, so contacts is granted explicitly (FULL access --
        # granted after `all` so nothing leaves it at "limited").
        for service in ("all", "contacts"):
            subprocess.run(["xcrun", "simctl", "privacy", req.device_id, "grant",
                            service, req.bundle_id],
                           capture_output=True, text=True, timeout=60)
        if say:
            say(f"{req.role}: pre-granted all privacy permissions")
    except Exception as e:                      # never fatal
        logger.debug("grant all failed for %s: %s", req.bundle_id, e)


def device_is_available(device_id: str) -> tuple[bool, str]:
    """Can we install onto this simulator at all?"""
    if not device_id:
        return False, "no device assigned"
    ok, msg = app_builder.ensure_ios_booted(device_id)
    return ok, msg


def ensure_app_on_device(req: AppRequirement, *, allow_build: bool = True,
                         on_log=None) -> DeploymentResult:
    """Guarantee *req.bundle_id* is installed on *req.device_id*, or raise.

    Skips the install when the right bundle is already there (the common case --
    reinstalling costs ~30s and wipes app state, which breaks scenarios that expect
    a signed-in app). Rebuilds only when there is no artifact carrying the required
    bundle id.
    """
    say = on_log or (lambda _m: None)
    res = DeploymentResult(role=req.role, bundle_id=req.bundle_id,
                           device_id=req.device_id)

    ok_dev, dev_msg = device_is_available(req.device_id)
    if not ok_dev:
        raise _blocked(req, f"Target device is not available — {dev_msg}",
                       "Check the device id in the scenario/project configuration, "
                       "or create/boot that simulator.")

    # Ask simctl DIRECTLY whether the app is there, so a stalled simctl raises
    # TimeoutExpired instead of being flattened into "not installed". app_builder's
    # helpers swallow the timeout into a False, which is indistinguishable from a
    # genuinely missing app -- and a busy machine (measured >120s here under a
    # concurrent build) then failed runs that should simply have proceeded.
    _probe_installed(req.device_id, req.bundle_id)

    before = app_builder.installed_version(req.device_id, req.bundle_id)
    res.previous_version = before.get("version")

    project = project_for_bundle(req.bundle_id)
    if app_builder.is_installed(req.device_id, req.bundle_id):
        # Installed -- but is it the build we have? Compare against the artifact on
        # disk and replace only on a real mismatch. Reinstalling unconditionally
        # costs ~30s per app and wipes app data (login, cached state), which is its
        # own class of scenario failure, so "same version" must mean "leave it".
        artifact = find_artifact(project, req.bundle_id) if project else None
        want = app_builder.app_version(artifact) if artifact else {}
        stale = bool(want.get("version")) and (
            want.get("version") != before.get("version")
            or want.get("build") != before.get("build"))
        # Version is not the only way an artifact can differ. A rebuild that changes
        # configuration (a different API host, say) keeps the SAME version and build
        # number, so a version-only check reports "already-present" and the device
        # quietly keeps running the previous binary -- the rebuild appears to have had
        # no effect. Compare build time against install time as well.
        if not stale and artifact:
            stale = _artifact_is_newer_than_install(artifact, req)
        if not stale:
            res.installed, res.action = True, "already-present"
            res.version, res.build = before.get("version"), before.get("build")
            # Cheap and idempotent, and NOT implied by "already installed": permission
            # is per bundle id, so a newly deployed variant starts with none even on a
            # simulator where its production twin has it. See _grant_device_permissions.
            _grant_device_permissions(req, say)
            say(f"{req.role}: {req.bundle_id} already installed "
                f"{before.get('version')} ({before.get('build')}) — leaving it alone")
            return res
        say(f"{req.role}: stale — installed {before.get('version')} "
            f"({before.get('build')}), required {want.get('version')} "
            f"({want.get('build')}) — updating")

    if not project:
        raise _blocked(req, f"No project is configured to build {req.bundle_id}.",
                       "Add/point a project at that bundle id (its app_bundle_id), "
                       "then deploy it.")

    artifact = find_artifact(project, req.bundle_id)
    if not artifact and allow_build:
        if not repository_manager.is_cloned(project["id"]):
            raise _blocked(req, f"No matching build artifact available, and the "
                                f"checkout for '{project['name']}' is missing.",
                           f"Clone/prepare '{project['name']}' and build it.")
        say(f"{req.role}: no {req.bundle_id} artifact — building '{project['name']}' …")
        repo = repository_manager.get_repo_path(project["id"])
        built = app_builder.build(repo, project["platform"], device_id=req.device_id,
                                  bundle_id=req.bundle_id)
        if not built.ok or not built.artifact_path:
            raise _blocked(req, f"Build failed: {(built.error or '')[:400]}",
                           f"Fix the build for '{project['name']}' and retry.")
        artifact = built.artifact_path

    if not artifact:
        raise _blocked(req, "No matching build artifact available.",
                       f"Build/deploy the {req.role} app first.")

    want = app_builder.app_version(artifact)
    say(f"{req.role}: installing {os.path.basename(artifact)} "
        f"{want.get('version')} ({want.get('build')}) → {req.device_id[:8]}…")
    ok, msg = app_builder.install(req.device_id, artifact, req.platform)
    if not ok:
        raise _blocked(req, f"Install failed: {str(msg)[:400]}",
                       "Check the simulator is booted and the artifact is complete.")

    # Verify FROM THE DEVICE. A zero exit from `simctl install` is not proof.
    if not app_builder.is_installed(req.device_id, req.bundle_id):
        raise _blocked(req, "Install reported success but the device does not list "
                            f"{req.bundle_id} afterwards.",
                       "The artifact likely declares a different bundle id — rebuild "
                       "it for this variant.")
    now = app_builder.installed_version(req.device_id, req.bundle_id)
    res.installed = True
    res.action = "reinstalled" if res.previous_version else "installed"
    res.version, res.build = now.get("version"), now.get("build")
    res.detail = str(msg)
    # A fresh install resets permissions, so grant them AFTER installing.
    _grant_device_permissions(req, say)
    return res


def preflight_report(results: List[DeploymentResult], *, passed: bool) -> str:
    """The block printed before a run, so what it is about to drive is visible."""
    lines = ["SCENARIO PREFLIGHT", ""]
    for r in results:
        lines += [
            r.role.capitalize(),
            f"  Bundle ID : {r.bundle_id}",
            f"  Device    : {r.device_id[:8]}…",
            f"  Installed : {'YES' if r.installed else 'NO'}",
            f"  Version   : {r.version or '?'} ({r.build or '?'})",
            "",
        ]
    lines.append(f"Preflight: {'PASS' if passed else 'FAIL'}")
    return "\n".join(lines)


def prepare_scenario(requirements: List[AppRequirement], *, allow_build: bool = True,
                     on_log=None) -> List[DeploymentResult]:
    """Deploy every required app to its OWN assigned device.

    Each requirement carries its own device, so two apps needing two different
    simulators is the ordinary case, not a special one.
    """
    out: List[DeploymentResult] = []
    for req in requirements:
        out.append(ensure_app_on_device(req, allow_build=allow_build, on_log=on_log))
    return out
