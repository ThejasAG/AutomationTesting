"""Measured wrappers around the EXISTING preparation steps.

Nothing here reimplements installation. Each function calls the same builder /
repository / preparation code the platform already uses and records what it
observes around and during it. That is the point: a second installation path
would drift from the real one and report on something that never ran.

Every value written into a Stage here is either read from the filesystem, timed
with a monotonic clock, or parsed out of the tool's own output. Where a tool
does not expose something, the metric is set to None and renders as
``unavailable``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from automation.projects import setup_report as sr
from automation.projects.setup_report import (
    CacheSampler, Stage, UNAVAILABLE, classify_failure, count_cache_entries,
    dir_size, disk_free, human_bytes, lockfile_package_count, parse_yarn_event,
    yarn_cache_dir,
)

logger = logging.getLogger(__name__)


# ── repository state (cold vs warm) ──────────────────────────────────────────

def observe_state(repo_path: str) -> Tuple[bool, Dict[str, str]]:
    """Is this a cold or a warm tree? Returns (warm, per-artifact description).

    Warm means every expensive artifact is already present. Reported as the
    observed facts, not just a label, because "warm" alone hides which half of
    the work is actually being reused.
    """
    nm = os.path.join(repo_path, "node_modules")
    pods = os.path.join(repo_path, "ios", "Pods")
    build = os.path.join(repo_path, "ios", "build")

    has_nm = os.path.isdir(nm) and bool(os.listdir(nm)) if os.path.isdir(nm) else False
    has_pods = os.path.isdir(os.path.join(pods, "Pods.xcodeproj"))
    has_build = os.path.isdir(build)

    state = {
        "node_modules": "already present" if has_nm else "absent",
        "Pods": "already present" if has_pods else "absent",
        "build": "incremental" if has_build else "absent",
    }
    return (has_nm and has_pods), state


# ── 1. clone / checkout ──────────────────────────────────────────────────────

# git's transfer summary, e.g.
#   "Receiving objects: 100% (12345/12345), 42.80 MiB | 5.10 MiB/s, done."
_GIT_XFER = re.compile(
    r"Receiving objects:\s*100%\s*\((\d+)/(\d+)\)(?:,\s*([\d.]+)\s*([KMG])iB)?", re.I)


def parse_git_transfer(text: str) -> Dict[str, Any]:
    """Transfer stats from git's stderr, when git chose to print them.

    Absent for a local/cached clone, which is why every field is Optional — a
    missing stat means git did not report one, not that zero bytes moved.
    """
    out: Dict[str, Any] = {"objects": None, "received_bytes": None}
    m = _GIT_XFER.search(text or "")
    if not m:
        return out
    out["objects"] = int(m.group(2))
    if m.group(3) and m.group(4):
        mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}[m.group(4).upper()]
        out["received_bytes"] = int(float(m.group(3)) * mult)
    return out


def measure_clone(stage: Stage, repo_path: str, do_clone: Callable[[], Tuple[bool, str]],
                  was_present: bool) -> bool:
    """Time a clone/pull and record size, commit, branch and transfer stats.

    *do_clone* performs the real work (the existing repository_manager call);
    this only measures around it.
    """
    stage.start()
    ok, output = do_clone()

    commit = _git(repo_path, ["rev-parse", "--short", "HEAD"])
    branch = _git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    xfer = parse_git_transfer(output)

    # The CHECKOUT's size, not the working directory's. A prepared tree also
    # holds node_modules, .yarn/cache, Pods and ios/build — walking those
    # reported a 190 MB repository as "28.5 GB" and took half a minute doing it,
    # which also inflated the clone duration this function exists to measure.
    # git's own object store plus the tracked files is what "repository size"
    # means; the other artifacts are measured by the stages that create them.
    size = _repo_size(repo_path)

    metrics: Dict[str, Any] = {
        "Mode": "reused existing checkout" if was_present else "fresh clone",
        "Repository size": human_bytes(size),
        "Branch": branch or UNAVAILABLE,
        "Commit": commit or UNAVAILABLE,
    }
    # Only present these when git actually reported them.
    if xfer["received_bytes"] is not None:
        metrics["Received"] = human_bytes(xfer["received_bytes"])
    if xfer["objects"] is not None:
        metrics["Objects"] = f"{xfer['objects']:,}"
    if was_present and xfer["received_bytes"] is None:
        metrics["Received"] = "n/a (no fresh clone)"

    if ok:
        stage.complete(**metrics)
    else:
        stage.fail(output, classification=classify_failure(output),
                   remedy="Check the git URL, branch name and network access.",
                   **metrics)
    return ok


def _repo_size(repo_path: str) -> Optional[int]:
    """Bytes of the git checkout: the object store plus the tracked worktree.

    Asks git rather than walking the tree, so build output and dependency
    directories are excluded by construction instead of by a blocklist that
    would go stale. Falls back to the .git directory alone if the worktree
    query fails, and to None if git cannot answer at all.
    """
    if not os.path.isdir(os.path.join(repo_path, ".git")):
        return None

    git_dir = dir_size(os.path.join(repo_path, ".git")) or 0

    # Tracked files only — `ls-files -z` excludes everything git ignores.
    try:
        res = subprocess.run(["git", "ls-files", "-z"], cwd=repo_path,
                             capture_output=True, timeout=60)
        if res.returncode != 0:
            return git_dir or None
        total = git_dir
        for rel in res.stdout.split(b"\0"):
            if not rel:
                continue
            try:
                st = os.lstat(os.path.join(repo_path, rel.decode("utf-8", "replace")))
            except OSError:
                continue
            total += st.st_size
        return total
    except (OSError, subprocess.SubprocessError):
        return git_dir or None


def _git(repo_path: str, args: List[str]) -> Optional[str]:
    try:
        res = subprocess.run(["git"] + args, cwd=repo_path, capture_output=True,
                             text=True, timeout=30)
        return res.stdout.strip() if res.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


# ── 3. JS dependency install ─────────────────────────────────────────────────

def measure_js_install(
    stage: Stage,
    repo_path: str,
    cmd: List[str],
    env: Optional[Dict[str, str]] = None,
    timeout: int = 1800,
    on_line: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, str]:
    """Run the JS install with real, streamed progress.

    Streaming is the change that makes progress possible at all: the previous
    implementation used ``subprocess.run(capture_output=True)``, which returns
    only once the install has finished — nothing could observe it mid-flight.

    Progress numbers come from two independent real sources:
      * the lockfile (denominator — what the install is contracted to produce)
      * the project-local Yarn cache (numerator — archives actually fetched)
    Yarn's own JSON stream supplies step boundaries and per-step durations.

    The link step exposes neither a count nor bytes, so while it runs the stage
    reports elapsed time only and its ETA is ``unavailable``. That is a
    deliberate refusal to interpolate.
    """
    stage.start()

    is_yarn_berry = "yarn" in " ".join(cmd) and os.path.exists(
        os.path.join(repo_path, "yarn.lock"))

    total = lockfile_package_count(repo_path)
    stage.total_units = total
    stage.unit_name = "packages"

    nm = os.path.join(repo_path, "node_modules")
    nm_before = dir_size(nm)
    free_before = disk_free(repo_path)
    cache_dir = yarn_cache_dir(repo_path) if is_yarn_berry else None

    # Pre-install facts, all measured.
    stage.metrics["Package manager"] = " ".join(cmd[:2])
    stage.metrics["Lockfile"] = (
        "yarn.lock" if os.path.exists(os.path.join(repo_path, "yarn.lock"))
        else "package-lock.json" if os.path.exists(
            os.path.join(repo_path, "package-lock.json")) else "none")
    stage.metrics["Total expected"] = f"{total:,}" if total else UNAVAILABLE
    stage.metrics["node_modules before"] = human_bytes(nm_before)
    stage.metrics["Free before"] = human_bytes(free_before)

    sampler = CacheSampler(stage, cache_dir).start()

    run_cmd = list(cmd)
    if is_yarn_berry and "--json" not in run_cmd:
        run_cmd.append("--json")

    steps: Dict[str, float] = {}
    current_step: Optional[str] = None
    step_started = time.monotonic()
    warnings = 0
    lines: List[str] = []
    ok = False

    try:
        proc = subprocess.Popen(
            run_cmd, cwd=repo_path, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            env={**os.environ, **(env or {})},
        )
    except FileNotFoundError:
        sampler.stop()
        msg = (f"Command not found: {run_cmd[0]}. Install it and make sure it is on "
               f"the PATH of the process running the platform.")
        stage.fail(msg, classification=sr.ENVIRONMENT,
                   remedy=f"Install {run_cmd[0]} and re-run preparation.")
        return False, msg

    deadline = time.monotonic() + timeout
    try:
        for raw in proc.stdout:            # streams as the install runs
            lines.append(raw.rstrip("\n"))
            if on_line:
                on_line(raw.rstrip("\n"))

            ev = parse_yarn_event(raw)
            if ev:
                if ev["kind"] == "step_start":
                    if current_step:
                        steps[current_step] = time.monotonic() - step_started
                    current_step = ev["step"]
                    step_started = time.monotonic()
                    stage.notes = [f"step: {current_step}"]
                elif ev["kind"] == "step_end" and current_step:
                    steps[current_step] = time.monotonic() - step_started
                    current_step = None
                elif ev["kind"] == "warning":
                    warnings += 1

            if time.monotonic() > deadline:
                proc.kill()
                raise subprocess.TimeoutExpired(run_cmd, timeout)

        proc.wait(timeout=60)
        ok = proc.returncode == 0
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        sampler.stop()
        msg = f"{' '.join(run_cmd[:3])}… timed out after {timeout}s."
        stage.fail(msg, classification=sr.ENVIRONMENT,
                   remedy="Check network access and whether the install is stalled.")
        return False, msg
    finally:
        sampler.stop()
        if current_step:
            steps.setdefault(current_step, time.monotonic() - step_started)

    output = "\n".join(lines)
    nm_after = dir_size(nm)
    free_after = disk_free(repo_path)
    installed = _count_installed_packages(nm)

    metrics: Dict[str, Any] = {
        "Installed": f"{installed:,}" if installed is not None else UNAVAILABLE,
        "Downloaded (cache)": (human_bytes(stage.bytes_moved)
                               if stage.bytes_moved is not None else UNAVAILABLE),
        "node_modules growth": (human_bytes(nm_after - nm_before)
                                if nm_after is not None and nm_before is not None
                                else human_bytes(nm_after)),
        "node_modules after": human_bytes(nm_after),
        "Free after": human_bytes(free_after),
        "Warnings": warnings,
    }
    for name in ("resolution", "fetch", "link"):
        if name in steps:
            metrics[f"{name.capitalize()} step"] = f"{steps[name]:.1f}s"

    # Yarn's three named steps routinely account for a minority of the wall time:
    # measured on this project, they summed to 40s of a 208s install. The rest is
    # the build/postinstall work yarn does not bracket with a step marker (here,
    # the `postinstall` script). Leaving it unattributed is what makes an install
    # look stalled, so name it rather than letting it hide.
    total_el = stage.elapsed
    accounted = sum(steps.values())
    if total_el is not None and steps and (total_el - accounted) > 1.0:
        metrics["Build/postinstall"] = (
            f"{total_el - accounted:.1f}s (not itemised by the package manager)")

    stage.notes = []
    if ok:
        stage.complete(**metrics)
    else:
        tail = "\n".join(lines[-40:])
        stage.fail(tail or "install failed", classification=classify_failure(output),
                   remedy=_js_remedy(output), **metrics)
    return ok, output


def _js_remedy(output: str) -> str:
    low = (output or "").lower()
    if "eresolve" in low:
        return ("Peer-dependency conflict. Fix the version constraints in "
                "package.json rather than forcing the install.")
    if "enotfound" in low or "could not resolve host" in low:
        return "The registry was unreachable — check network/proxy access."
    return "Inspect the install output above; the failing package is named in it."


def _count_installed_packages(node_modules: str) -> Optional[int]:
    """Real package directories under node_modules (scopes expanded).

    Counts directories containing a package.json, which is what "installed"
    means on disk. None when the tree is absent.
    """
    if not os.path.isdir(node_modules):
        return None
    n = 0
    try:
        for entry in os.listdir(node_modules):
            if entry.startswith("."):
                continue
            full = os.path.join(node_modules, entry)
            if entry.startswith("@"):
                try:
                    for sub in os.listdir(full):
                        if os.path.exists(os.path.join(full, sub, "package.json")):
                            n += 1
                except OSError:
                    continue
            elif os.path.exists(os.path.join(full, "package.json")):
                n += 1
    except OSError:
        return None
    return n


def declared_dependency_count(repo_path: str) -> Dict[str, Optional[int]]:
    """Counts declared in package.json — separate from what is installed."""
    path = os.path.join(repo_path, "package.json")
    try:
        data = json.load(open(path, encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {"dependencies": None, "devDependencies": None}
    return {
        "dependencies": len(data.get("dependencies") or {}),
        "devDependencies": len(data.get("devDependencies") or {}),
    }


# ── 4. platform-injected dependencies ────────────────────────────────────────

def patches_conflicting_with_injection(repo_path: str,
                                       injected: Dict[str, str]) -> List[str]:
    """Patches whose pinned version the platform's own injection has moved past.

    patch-package names files ``<package>+<version>.patch`` and applies one only
    when the installed version matches. So injecting a NEWER version of a
    patched package silently disables that package's patch — the install
    succeeds, the patch never applies, and the build fails somewhere unrelated.

    Real case on preprod-2-May18: the branch carries
    react-native-compressor+1.10.3.patch, while the platform injects 1.13.0 (the
    first release that builds against the iOS 26 SDK). Reporting the two facts
    separately leaves the reader to spot the collision; this names it.
    """
    patches_dir = os.path.join(repo_path, "patches")
    if not os.path.isdir(patches_dir) or not injected:
        return []

    out: List[str] = []
    for fname in sorted(f for f in os.listdir(patches_dir) if f.endswith(".patch")):
        stem = fname[:-len(".patch")]
        parts = stem.split("+")
        if len(parts) < 2:
            continue
        pkg = "/".join(parts[:-1])
        pinned = parts[-1]
        target = injected.get(pkg)
        if target and target != pinned:
            out.append(
                f"{fname} pins {pkg}@{pinned}, but the platform installs "
                f"{target} — patch-package will NOT apply this patch")
    return out


def report_platform_deps(stage: Stage, injected: Dict[str, str],
                         skipped: Dict[str, str]) -> None:
    """State exactly what the PLATFORM added, separately from the project's own.

    Both halves are always shown, including the empty case: "injected 0, skipped
    react-native-compressor, reason: no source import detected" is the report
    that proves nothing was slipped in silently.
    """
    stage.start()
    stage.metrics["Injected"] = len(injected)
    if injected:
        stage.metrics["Packages"] = [f"{k}@{v}" for k, v in injected.items()]
        stage.metrics["Reason"] = "imported by target source"
    if skipped:
        stage.metrics["Skipped"] = list(skipped.keys())
        stage.metrics["Skip reason"] = "no source import detected"
    stage.complete()


# ── 5. patches ───────────────────────────────────────────────────────────────

def measure_patches(stage: Stage, repo_path: str) -> None:
    """Report patch-package state, reusing the doctor's per-patch checks.

    Never silent: every patch that is not applied is listed with the reason the
    doctor gave. A project with no patches/ directory reports 0 found, which is
    a fact rather than an omission.
    """
    from automation.projects import macos_environment as env

    stage.start()
    patches_dir = os.path.join(repo_path, "patches")
    if not os.path.isdir(patches_dir):
        stage.metrics["Patches found"] = 0
        stage.skip("no patches/ directory in this project")
        return

    files = sorted(f for f in os.listdir(patches_dir) if f.endswith(".patch"))
    checks = env._check_patches(repo_path)
    conflicts = getattr(stage, "_injection_conflicts", [])
    per_patch = [c for c in checks if c.name.startswith("patch ")]

    applicable = [c for c in per_patch if c.status == env.PASS]
    problems = [c for c in per_patch if c.status != env.PASS]

    stage.metrics["Patches found"] = len(files)
    stage.metrics["Applicable"] = len(applicable)
    stage.metrics["Not applicable"] = len(problems)
    stage.complete()

    for c in applicable:
        stage.notes.append(f"[✓] {c.name[6:]} — {c.detail}")
    for c in problems:
        mark = "[!]" if c.status == env.WARN else "[✗]"
        stage.notes.append(f"{mark} {c.name[6:]} — {c.detail}"
                           + (f" ({c.fix})" if c.fix else ""))

    # A patch disabled by the platform's OWN injection is the platform's doing,
    # so it is called out rather than left as two facts the reader must connect.
    for msg in conflicts:
        stage.metrics["Disabled by injection"] = len(conflicts)
        stage.notes.append(f"[!] {msg}")


# ── 6. CocoaPods ─────────────────────────────────────────────────────────────

def pod_dir_for(repo_path: str) -> Optional[str]:
    for candidate in (os.path.join(repo_path, "ios"), repo_path):
        if os.path.exists(os.path.join(candidate, "Podfile")):
            return candidate
    return None


def count_pods(pod_dir: str) -> Optional[int]:
    """Pods from Podfile.lock — the authoritative list of what was installed."""
    lock = os.path.join(pod_dir, "Podfile.lock")
    if not os.path.exists(lock):
        return None
    try:
        text = open(lock, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    m = re.search(r"^PODS:\n(.*?)(?=^\S)", text, re.S | re.M)
    if not m:
        return None
    # Top-level entries only ("  - Name (1.2.3):"), not their sub-dependencies.
    return len(re.findall(r"^  - ", m.group(1), re.M)) or None


def measure_pods(stage: Stage, repo_path: str,
                 do_install: Callable[[], Tuple[bool, str]]) -> bool:
    """Time `pod install` and record size/count/lockfile state around it.

    Runs whatever the builder already runs — this never substitutes `pod update`
    to force a green result.
    """
    stage.start()
    pod_dir = pod_dir_for(repo_path)
    if not pod_dir:
        stage.metrics["Podfile"] = "none"
        stage.skip("project has no Podfile — CocoaPods not used")
        return True

    pods_path = os.path.join(pod_dir, "Pods")
    before = dir_size(pods_path)
    lock = os.path.join(pod_dir, "Podfile.lock")
    manifest = os.path.join(pods_path, "Manifest.lock")

    stage.metrics["Podfile.lock"] = "present" if os.path.exists(lock) else "absent"
    stage.metrics["Pods before"] = human_bytes(before)

    ok, output = do_install()

    after = dir_size(pods_path)
    consistent = UNAVAILABLE
    if os.path.exists(lock) and os.path.exists(manifest):
        try:
            consistent = ("consistent"
                          if open(lock, errors="replace").read()
                          == open(manifest, errors="replace").read()
                          else "DRIFTED from Manifest.lock")
        except OSError:
            consistent = UNAVAILABLE

    metrics = {
        "Lockfile state": consistent,
        "Pods after": human_bytes(after),
        "Pods installed": (lambda n: f"{n}" if n is not None else UNAVAILABLE)(
            count_pods(pod_dir)),
        "Operation": "pod install",
    }
    if "already installed" in (output or "").lower():
        metrics["Operation"] = "reused existing Pods (up to date)"

    if ok:
        stage.complete(**metrics)
    else:
        stage.fail((output or "")[-4000:], classification=classify_failure(output),
                   remedy=_pod_remedy(output), **metrics)
    return ok


def _pod_remedy(output: str) -> str:
    low = (output or "").lower()
    if "ffi" in low:
        return ("The Ruby FFI extension could not load — this is a Ruby/CocoaPods "
                "environment problem. Reinstall CocoaPods against the active Ruby "
                "(e.g. via bundler or Homebrew) rather than changing the Podfile.")
    if "deployment target" in low:
        return "A pod requires a higher iOS deployment target than the Podfile sets."
    return "Inspect the pod output above; the failing pod is named in it."
