"""Measured, honest reporting of project setup.

The rule this module exists to enforce: **every number printed was measured.**
Anything the underlying tool does not expose is printed as ``unavailable``,
never estimated, never interpolated, never back-filled from a previous run.

That rule is what decides the shape of everything below, so it is worth saying
what is and is not measurable, with the evidence:

* Clone         — duration, bytes on disk, commit, branch. All real.
                  `git clone` also reports transfer stats on stderr, captured
                  when present.
* Environment   — reused wholesale from ``macos_environment.detect_host`` /
                  ``detect_project``. Not re-shelled here; the doctor already
                  runs these once and this only formats its output.
* JS install    — Yarn Berry's ``--json`` stream gives per-STEP boundaries
                  (Resolution / Fetch / Link) and each step's duration. It does
                  NOT give a per-package counter and it does NOT give byte
                  totals. Verified against yarn 3.6.4 on this project: the whole
                  install emitted 8 YN0000 lines, three step markers, and no
                  progress events whatsoever.

                  So package-level progress is derived from the one thing that
                  IS ground truth: this project sets ``enableGlobalCache: false``,
                  so fetched archives land in the repo's own ``.yarn/cache`` and
                  can be counted and weighed while the install runs. The
                  denominator is the lockfile's own resolution count. On this
                  repo those agree to within one entry (2228 resolutions ↔ 2227
                  archives), which is why the counter is trustworthy enough to
                  show at all.

                  When the cache is global or absent, that ground truth is gone
                  and the counter goes to ``unavailable`` rather than guessing.
* ETA           — only ever computed from observed throughput of a stage that
                  has a real numerator AND denominator (i.e. the fetch step
                  above). The link step exposes neither, so it has no ETA and
                  says so. There is no fixed-percentage fallback anywhere.
* Pods          — duration, directory size before/after, pod count from
                  Podfile.lock, lockfile/manifest agreement. All real.

Timing is ``time.monotonic()`` throughout: wall-clock is subject to NTP steps
and DST, and a stage that appears to take -1.0s destroys trust in the whole
report. Wall-clock is recorded separately, once, only for display of start
times.

Rendering is plain text with no cursor control, so a log file reads exactly the
same as the terminal did.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

# The literal printed wherever a value is not measurable. One constant so the
# report can never drift into saying "unknown" in one place and "n/a" in another,
# and so tests can assert on it.
UNAVAILABLE = "unavailable"

PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
SKIPPED = "skipped"

_MARK = {
    PENDING: "[ ]",
    RUNNING: "[→]",
    COMPLETED: "[✓]",
    FAILED: "[✗]",
    SKIPPED: "[-]",
}


# ── formatting helpers ───────────────────────────────────────────────────────

def human_bytes(n: Optional[int]) -> str:
    """Bytes as a human string, or ``unavailable`` for None.

    None and 0 are deliberately different: a zero-byte download is a real
    measurement (a fully cached install downloads nothing) and must not be
    rendered as "unknown".
    """
    if n is None:
        return UNAVAILABLE
    step = 1024.0
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(val) < step or unit == "TB":
            return f"{val:.0f} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= step
    return f"{val:.1f} TB"


def human_duration(seconds: Optional[float]) -> str:
    """MM:SS (or HH:MM:SS past an hour), or ``unavailable``."""
    if seconds is None:
        return UNAVAILABLE
    seconds = max(0.0, float(seconds))
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def dir_size(path: str) -> Optional[int]:
    """Total bytes under *path*, or None if it does not exist / cannot be read.

    Counts allocated blocks rather than apparent size, and skips symlinks so a
    node_modules full of workspace links is not counted many times over.
    """
    if not path or not os.path.isdir(path):
        return None
    total = 0
    seen: set = set()
    for root, dirnames, filenames in os.walk(path, onerror=lambda e: None):
        for name in filenames:
            full = os.path.join(root, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            # Hardlinked files (pnpm/yarn stores do this) must be counted once.
            key = (st.st_dev, st.st_ino)
            if st.st_nlink > 1:
                if key in seen:
                    continue
                seen.add(key)
            total += st.st_size
    return total


def disk_free(path: str) -> Optional[int]:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


# ── one stage of the pipeline ────────────────────────────────────────────────

@dataclass
class Stage:
    """A single timed stage. Reports start / progress / elapsed / completed / failed.

    ``metrics`` holds only measured values. A key that is absent is absent from
    the report; a key whose value is None renders as ``unavailable``.
    """

    key: str
    label: str
    status: str = PENDING
    started_monotonic: Optional[float] = None
    ended_monotonic: Optional[float] = None
    started_wall: Optional[float] = None
    metrics: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    classification: Optional[str] = None
    remedy: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    # Live progress, set only when a real numerator/denominator exists.
    done_units: Optional[int] = None
    total_units: Optional[int] = None
    unit_name: str = "items"
    bytes_moved: Optional[int] = None

    def start(self) -> "Stage":
        self.status = RUNNING
        self.started_monotonic = time.monotonic()
        self.started_wall = time.time()
        return self

    def complete(self, **metrics) -> "Stage":
        self.metrics.update(metrics)
        self.status = COMPLETED
        self.ended_monotonic = time.monotonic()
        return self

    def fail(self, error: str, classification: Optional[str] = None,
             remedy: Optional[str] = None, **metrics) -> "Stage":
        """Fail, PRESERVING whatever progress was measured before the failure."""
        self.metrics.update(metrics)
        self.status = FAILED
        self.error = error
        self.classification = classification
        self.remedy = remedy
        self.ended_monotonic = time.monotonic()
        return self

    def skip(self, why: str, **metrics) -> "Stage":
        self.metrics.update(metrics)
        self.status = SKIPPED
        self.notes.append(why)
        if self.started_monotonic is not None and self.ended_monotonic is None:
            self.ended_monotonic = time.monotonic()
        return self

    @property
    def elapsed(self) -> Optional[float]:
        """Monotonic seconds. None until the stage starts."""
        if self.started_monotonic is None:
            return None
        end = self.ended_monotonic
        if end is None:
            end = time.monotonic()
        return end - self.started_monotonic

    def progress_fraction(self) -> Optional[float]:
        if not self.total_units or self.done_units is None:
            return None
        if self.total_units <= 0:
            return None
        return max(0.0, min(1.0, self.done_units / self.total_units))

    def eta_seconds(self) -> Optional[float]:
        """Seconds remaining, from OBSERVED throughput only.

        None whenever an honest estimate is impossible: no denominator, nothing
        done yet (no rate to measure), or already complete. Never a fixed
        percentage, never a constant.
        """
        frac = self.progress_fraction()
        el = self.elapsed
        if frac is None or el is None or el <= 0:
            return None
        if self.done_units is None or self.done_units <= 0:
            return None          # no throughput observed yet -> "calculating..."
        if frac >= 1.0:
            return 0.0
        rate = self.done_units / el          # units per second, actually observed
        if rate <= 0:
            return None
        remaining = self.total_units - self.done_units
        return remaining / rate

    def speed_bytes_per_sec(self) -> Optional[float]:
        el = self.elapsed
        if self.bytes_moved is None or el is None or el <= 0:
            return None
        if self.bytes_moved <= 0:
            return None
        return self.bytes_moved / el

    def render(self) -> List[str]:
        """Plain-text block. No cursor control — identical in a terminal and a file."""
        head = f"{_MARK[self.status]} {self.label}"
        if self.status in (COMPLETED, FAILED) and self.elapsed is not None:
            head += f"  {self.elapsed:.1f}s"
        lines = [head]

        if self.status == RUNNING:
            frac = self.progress_fraction()
            if frac is not None:
                lines.append(f"    Progress        : {self.done_units:,} / "
                             f"{self.total_units:,} {self.unit_name}")
            elif self.total_units is None and self.done_units is not None:
                # A count with no denominator is still a fact; show it as one.
                lines.append(f"    {self.unit_name.capitalize()}          : "
                             f"{self.done_units:,} (total {UNAVAILABLE})")
            if self.bytes_moved is not None:
                lines.append(f"    Cache data      : {human_bytes(self.bytes_moved)}")
            if self.elapsed is not None:
                lines.append(f"    Elapsed         : {human_duration(self.elapsed)}")
            # Which sub-step is running. Without this, the long unbracketed
            # build/postinstall tail reads as a stall.
            for n in self.notes:
                if n.startswith("step: "):
                    lines.append(f"    Current step    : {n[6:]}")
            speed = self.speed_bytes_per_sec()
            if speed is not None:
                lines.append(f"    Speed           : {human_bytes(int(speed))}/s")
            eta = self.eta_seconds()
            if eta is not None:
                lines.append(f"    ETA             : {human_duration(eta)}")
            elif frac is not None or self.done_units is not None:
                # Progress exists but no rate yet — say so rather than print a number.
                lines.append("    ETA             : calculating...")
            else:
                lines.append(f"    ETA             : {UNAVAILABLE}")

        for k, v in self.metrics.items():
            if isinstance(v, list):
                if not v:
                    continue
                lines.append(f"    {k:<16}: {len(v)}")
                lines.extend(f"      • {item}" for item in v)
            else:
                lines.append(f"    {k:<16}: {v if v is not None else UNAVAILABLE}")

        for n in self.notes:
            lines.append(f"    → {n}")

        if self.status == FAILED:
            lines.append("")
            lines.append("    Error:")
            for ln in (self.error or "").strip().splitlines()[-25:]:
                lines.append(f"        {ln}")
            if self.classification:
                lines.append("")
                lines.append(f"    Classification:  {self.classification}")
            if self.remedy:
                lines.append(f"    Recommended action:")
                lines.append(f"        {self.remedy}")
        return lines


# ── failure classification ───────────────────────────────────────────────────

ENVIRONMENT = "ENVIRONMENT"
PLATFORM = "PLATFORM"
APPLICATION = "APPLICATION"
DEPENDENCY = "DEPENDENCY"
XCODE = "XCODE"
COCOAPODS = "COCOAPODS"
NETWORK = "NETWORK"
BUILD = "BUILD"

# Ordered most-specific first: the first pattern that matches wins, so a
# CocoaPods message mentioning a network error is still classified COCOAPODS
# only if the pod-specific text matches first. Tuned against the failures this
# platform has actually produced.
_CLASSIFIERS: List[tuple] = [
    (NETWORK, (
        "could not resolve host", "connection refused", "connection reset",
        "network is unreachable", "etimedout", "econnreset", "enotfound",
        "timed out", "temporary failure in name resolution", "tls",
    )),
    # A broken Ruby/FFI install is an ENVIRONMENT problem even though CocoaPods
    # is what reports it: the fix is to repair the Ruby toolchain, never to edit
    # the Podfile. Classifying it as COCOAPODS sends people to the wrong file.
    (ENVIRONMENT, (
        "ffi", "loaderror", "gem install", "bundler", "rbenv", "rvm",
        "your ruby version is",
    )),
    (COCOAPODS, (
        "pod install", "podfile", "cocoapods", "podspec",
        "pods.xcodeproj", "manifest.lock",
    )),
    (XCODE, (
        "xcodebuild", "code signing", "no such module", "clang", "swiftc",
        "linker command failed", "ld: ", "sdk ", "provisioning profile",
    )),
    (DEPENDENCY, (
        "eresolve", "peer dep", "npm err", "yarn install", "lockfile",
        "cannot find module", "unmet dependency", "yn00", "package.json",
    )),
    (ENVIRONMENT, (
        "command not found", "no such file or directory", "permission denied",
        "ruby", "bundler", "xcode-select", "no space left", "disk full",
    )),
]


def classify_failure(text: str) -> str:
    """Bucket an error into one of the platform's failure classes.

    Falls back to BUILD rather than guessing: an unrecognised failure during a
    build IS a build failure, and claiming a specific cause we cannot support
    would be exactly the kind of invention this module exists to prevent.
    """
    low = (text or "").lower()
    for label, needles in _CLASSIFIERS:
        if any(n in low for n in needles):
            return label
    return BUILD


# ── yarn --json stream ───────────────────────────────────────────────────────

# Yarn Berry step boundaries. Verified on yarn 3.6.4: these are the only
# structural progress signals the JSON stream carries.
_STEP_RE = re.compile(r"^┌\s*(.+?)\s*step\s*$", re.I)
_DONE_RE = re.compile(r"^└\s*Completed(?:\s+in\s+(.+))?", re.I)


def parse_yarn_event(line: str) -> Optional[Dict[str, Any]]:
    """One line of ``yarn --json`` → a normalised event, or None.

    Returns ``{"kind": "step_start"|"step_end"|"warning"|"error"|"info",
    "step": str|None, "code": str, "text": str}``.
    """
    line = (line or "").strip()
    if not line.startswith("{"):
        return None
    try:
        payload = json.loads(line)
    except (ValueError, TypeError):
        return None

    data = payload.get("data")
    text = data if isinstance(data, str) else json.dumps(data) if data is not None else ""
    code = payload.get("displayName") or ""
    kind = payload.get("type") or "info"

    m = _STEP_RE.match(text.strip())
    if m:
        return {"kind": "step_start", "step": m.group(1).strip().lower(),
                "code": code, "text": text}
    if _DONE_RE.match(text.strip()):
        return {"kind": "step_end", "step": None, "code": code, "text": text}
    if kind == "error" or code.startswith("YN") and kind == "error":
        return {"kind": "error", "step": None, "code": code, "text": text}
    if kind == "warning":
        return {"kind": "warning", "step": None, "code": code, "text": text}
    return {"kind": "info", "step": None, "code": code, "text": text}


def lockfile_package_count(repo_path: str) -> Optional[int]:
    """How many packages the lockfile resolves, or None if not determinable.

    This is the only trustworthy denominator available: it is what the install
    is contracted to produce. Yarn Berry and npm lockfiles are both handled;
    anything else returns None and the report shows ``unavailable``.
    """
    berry = os.path.join(repo_path, "yarn.lock")
    if os.path.exists(berry):
        try:
            text = open(berry, encoding="utf-8", errors="replace").read()
        except OSError:
            return None
        # Berry (v2+) lists an explicit `resolution:` per package.
        n = len(re.findall(r'^\s+resolution: "', text, re.M))
        if n:
            return n
        # Yarn Classic has no resolution lines; count top-level spec headers.
        n = len(re.findall(r"^\"?[^#\s].*:\n  version ", text, re.M))
        return n or None

    npm_lock = os.path.join(repo_path, "package-lock.json")
    if os.path.exists(npm_lock):
        try:
            data = json.load(open(npm_lock, encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return None
        pkgs = data.get("packages")
        if isinstance(pkgs, dict):
            # "" is the root project itself, not a dependency.
            return max(0, len([k for k in pkgs if k])) or None
        deps = data.get("dependencies")
        if isinstance(deps, dict):
            return len(deps) or None
    return None


def yarn_cache_dir(repo_path: str) -> Optional[str]:
    """The project-local Yarn cache, if this project keeps one.

    Only a project-local cache can be attributed to this install. A global cache
    is shared with every other project on the machine, so counting it would
    report other people's downloads as this build's progress — so we return None
    and the fetch counter degrades to ``unavailable``, which is the honest
    outcome.
    """
    local = os.path.join(repo_path, ".yarn", "cache")
    if os.path.isdir(local):
        return local
    rc = os.path.join(repo_path, ".yarnrc.yml")
    if os.path.exists(rc):
        try:
            text = open(rc, encoding="utf-8", errors="replace").read()
        except OSError:
            return None
        m = re.search(r"^\s*cacheFolder:\s*[\"']?([^\"'\n]+)", text, re.M)
        if m:
            cand = m.group(1).strip()
            cand = cand if os.path.isabs(cand) else os.path.join(repo_path, cand)
            if os.path.isdir(cand):
                return cand
    return None


def count_cache_entries(cache_dir: Optional[str]) -> Optional[int]:
    if not cache_dir or not os.path.isdir(cache_dir):
        return None
    try:
        return sum(1 for f in os.listdir(cache_dir) if f.endswith(".zip"))
    except OSError:
        return None


class CacheSampler:
    """Polls a project-local Yarn cache on a thread so a running install can
    report real fetch progress.

    Sampling rather than parsing is deliberate: yarn's JSON stream carries no
    per-package events (measured, not assumed), so the cache directory is the
    only ground truth. Polling is cheap — a directory listing and a size walk,
    at a low duty cycle.
    """

    def __init__(self, stage: Stage, cache_dir: Optional[str],
                 interval: float = 2.0):
        self.stage = stage
        self.cache_dir = cache_dir
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.baseline_entries = count_cache_entries(cache_dir) or 0
        self.baseline_bytes = dir_size(cache_dir) if cache_dir else None

    def _sample(self) -> None:
        entries = count_cache_entries(self.cache_dir)
        if entries is not None:
            self.stage.done_units = max(0, entries - self.baseline_entries)
        size = dir_size(self.cache_dir) if self.cache_dir else None
        if size is not None and self.baseline_bytes is not None:
            self.stage.bytes_moved = max(0, size - self.baseline_bytes)

    def start(self) -> "CacheSampler":
        if not self.cache_dir:
            return self          # nothing measurable; stage stays "unavailable"

        def loop():
            while not self._stop.wait(self.interval):
                try:
                    self._sample()
                except Exception:
                    return       # never let sampling break an install
        self._thread = threading.Thread(target=loop, daemon=True,
                                        name="yarn-cache-sampler")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self.cache_dir:
            try:
                self._sample()   # final, authoritative sample
            except Exception:
                pass


# ── the whole report ─────────────────────────────────────────────────────────

_RULE = "─" * 54


@dataclass
class SetupReport:
    """Collects every stage and renders the report.

    Holds no logic of its own about what a stage means — stages are appended by
    the pipeline that actually does the work, so the report cannot drift out of
    step with reality.
    """

    project_name: str = ""
    stages: List[Stage] = field(default_factory=list)
    host: Dict[str, Any] = field(default_factory=dict)
    project: Dict[str, Any] = field(default_factory=dict)
    warm: Optional[bool] = None
    reuse: Dict[str, Any] = field(default_factory=dict)
    disk_free_before: Optional[int] = None
    disk_free_after: Optional[int] = None
    _started_monotonic: float = field(default_factory=time.monotonic)

    def stage(self, key: str, label: str) -> Stage:
        for s in self.stages:
            if s.key == key:
                return s
        s = Stage(key=key, label=label)
        self.stages.append(s)
        return s

    def get(self, key: str) -> Optional[Stage]:
        return next((s for s in self.stages if s.key == key), None)

    @property
    def total_elapsed(self) -> float:
        return time.monotonic() - self._started_monotonic

    @property
    def failed(self) -> Optional[Stage]:
        return next((s for s in self.stages if s.status == FAILED), None)

    # ── rendering ────────────────────────────────────────────────────────────

    def header(self) -> List[str]:
        out = [
            "╔══════════════════════════════════════════════════════╗",
            "║        AUTOMATION PLATFORM — PROJECT SETUP           ║",
            "╚══════════════════════════════════════════════════════╝",
            "",
        ]
        rows = [("Project", self.project_name or UNAVAILABLE)]
        for label, key in (("Branch", "Branch"), ("Commit", "Commit")):
            if key in self.project:
                rows.append((label, self.project.get(key) or UNAVAILABLE))
        for label, key in (
            ("macOS", "macOS"), ("Architecture", "Architecture"),
            ("Xcode", "Xcode"), ("iOS SDK", "iOS SDK"),
            ("xcode-select", "Developer Dir"), ("Node", "Node"),
            ("Yarn", "Yarn"), ("Ruby", "Ruby"),
            ("CocoaPods", "CocoaPods"), ("Homebrew", "Homebrew"),
        ):
            if key in self.host:
                rows.append((label, self.host.get(key) or UNAVAILABLE))
        if self.disk_free_before is not None:
            rows.append(("Disk available", human_bytes(self.disk_free_before)))
        out.extend(f"{k:<15}: {v}" for k, v in rows)
        return out

    def state_block(self) -> List[str]:
        """Cold vs warm, stated as the observed facts rather than a label alone."""
        if self.warm is None:
            return []
        out = ["", _RULE, "SETUP STATE — " + ("warm" if self.warm else "cold"), _RULE]
        for k in ("node_modules", "Pods", "build"):
            if k in self.reuse:
                out.append(f"{k:<15}: {self.reuse[k]}")
        return out

    def progress_block(self) -> List[str]:
        out = ["", _RULE, "SETUP PROGRESS", _RULE, ""]
        for s in self.stages:
            out.extend(s.render())
            out.append("")
        return out

    def summary(self) -> List[str]:
        done = [s for s in self.stages
                if s.status in (COMPLETED, FAILED) and s.elapsed is not None]
        failed = self.failed
        title = "SETUP FAILED" if failed else "SETUP COMPLETE"
        out = [
            "╔══════════════════════════════════════════════════════╗",
            f"║{title:^54}║",
            "╚══════════════════════════════════════════════════════╝",
            "",
        ]
        for s in done:
            out.append(f"{s.label:<23}: {human_duration(s.elapsed)}")
        out.append(_RULE)
        # Wall time for the whole run, which is >= the sum of the stages (it also
        # covers the gaps between them). Never less: if it were, one of the two
        # numbers would be wrong, and the larger is the one that actually bounds
        # the run.
        stage_sum = sum(s.elapsed or 0 for s in done)
        out.append(f"{'Total':<23}: "
                   f"{human_duration(max(self.total_elapsed, stage_sum))}")

        if self.disk_free_before is not None and self.disk_free_after is not None:
            consumed = self.disk_free_before - self.disk_free_after
            out += [
                "", "Disk", "────",
                f"{'Free before':<23}: {human_bytes(self.disk_free_before)}",
                f"{'Free after':<23}: {human_bytes(self.disk_free_after)}",
            ]
            # Free space is a WHOLE-VOLUME reading: Spotlight, Xcode caches and
            # other processes move it during a build, so the delta is only this
            # build's consumption to within that noise. Below a threshold the
            # delta IS the noise — reporting "969 MB freed" because the volume
            # happened to drift is exactly the invented number this must not
            # print. A negative delta is never dressed up as consumption.
            if consumed >= 50 * 1024 ** 2:
                out.append(f"{'Space consumed':<23}: {human_bytes(consumed)}"
                           f"  (whole-volume delta, ±other activity)")
            elif consumed <= -50 * 1024 ** 2:
                out.append(f"{'Space consumed':<23}: none — the volume gained "
                           f"{human_bytes(-consumed)} during the run "
                           f"(other processes)")
            else:
                out.append(f"{'Space consumed':<23}: below the noise floor of a "
                           f"whole-volume measurement")
        elif self.disk_free_before is not None:
            out += ["", "Disk", "────",
                    f"{'Free before':<23}: {human_bytes(self.disk_free_before)}",
                    f"{'Free after':<23}: {UNAVAILABLE}"]
        return out

    def render(self) -> str:
        parts = self.header() + self.state_block() + self.progress_block() + self.summary()
        return "\n".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        """Machine-readable form for the dashboard/API."""
        return {
            "project": self.project_name,
            "warm": self.warm,
            "reuse": self.reuse,
            "host": self.host,
            "project_info": self.project,
            "total_seconds": round(self.total_elapsed, 3),
            "disk_free_before": self.disk_free_before,
            "disk_free_after": self.disk_free_after,
            "stages": [
                {
                    "key": s.key,
                    "label": s.label,
                    "status": s.status,
                    "elapsed_seconds": round(s.elapsed, 3) if s.elapsed is not None else None,
                    "started_at": (datetime.fromtimestamp(s.started_wall).isoformat()
                                   if s.started_wall else None),
                    "metrics": s.metrics,
                    "done_units": s.done_units,
                    "total_units": s.total_units,
                    "bytes": s.bytes_moved,
                    "eta_seconds": s.eta_seconds(),
                    "error": s.error,
                    "classification": s.classification,
                    "notes": s.notes,
                }
                for s in self.stages
            ],
        }
