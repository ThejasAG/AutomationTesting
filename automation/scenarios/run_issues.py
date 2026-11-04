"""Problems a run saw that did not fail it, shown YELLOW in the report and on Run Details.

A step that did its job passes even when the app made it hard: the app crashed after
it and was relaunched, a spinner stayed up, an error toast came and went, a screen took
long to come up, or the automation had to work around a known app bug. Those are still
worth a person's eye, so each is written into the segment's notes as ONE line starting
with its tag, and the report lists them in yellow under Failed At (red stays for what
failed and for sums that did not match).

The screens are the ones the steps read anyway: `StepWatch` is called by
idb_driver.describe_all after every read during a step, so watching costs no reads.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

CRASH = "[APP CRASH]"
APP_ERROR = "[APP ERROR]"
LOADING = "[LOADING]"
SLOW_SCREEN = "[SLOW SCREEN]"
GLITCH = "[APP GLITCH]"

# How each yellow note starts (after its indentation) -> its label in the report. The
# watchdog's own tags (cross_app_flows._finish_step_watchdog, ui_health.LoadReport) and
# '[warn]' were written before this and are yellow too. '[MONITOR UNAVAILABLE]' is not:
# it is the monitor failing, not the app. Nor is slowness ('[SLOW LOAD]', '[SLOW /
# RECOVERED]', '[SLOW SCREEN]'): the user, 2026-11-04, "dont mention slow steps … only
# loading issues and crashing issues and functional issues" -- the notes stay in the
# run's step lists, the report does not list or count them.
KINDS: Tuple[Tuple[str, str], ...] = (
    (CRASH, "App crash"),
    (APP_ERROR, "App error"),
    (LOADING, "Loading"),
    ("[STUCK LOADING]", "Loading"),
    ("[NO UI PROGRESS]", "Loading"),
    ("[PARTIAL LOAD]", "Loading"),
    ("[LOADING ISSUE]", "Loading"),
    (GLITCH, "App glitch"),
    ("[warn]", "Warning"),
    ("⚡ flaky:", "Flaky"),
)


def _seconds(env: str, default: float) -> float:
    try:
        return float(os.getenv(env, str(default)))
    except ValueError:
        return default


def loading_seconds() -> float:
    """A spinner up this long in one stretch is a loading issue. MEASURED 2026-11-04: a
    cold launch shows 'Loading from Metro…' then 'In progress' for under 5s each."""
    return _seconds("ISSUE_LOADING_SECONDS", 8.0)


def slow_screen_seconds() -> float:
    return _seconds("ISSUE_SLOW_SCREEN_SECONDS", 10.0)


def kind_of(note: str) -> Optional[Tuple[str, str]]:
    """(tag, label) when *note* is a yellow note, else None."""
    text = (note or "").strip()
    for tag, label in KINDS:
        if text.startswith(tag):
            return tag, label
    return None


def issues_of(reasons: Iterable[str]) -> List[Tuple[str, str]]:
    """(label, text) for every yellow note in *reasons*, each once, in the order found."""
    out: Dict[Tuple[str, str], None] = {}
    for note in reasons or ():
        kind = kind_of(str(note))
        if not kind:
            continue
        tag, label = kind
        text = str(note).strip()[len(tag):].strip()
        out.setdefault((label, text), None)
    return list(out)


# ── what a screen shows ────────────────────────────────────────────────────
# The app's own failure toasts and alerts (grepped from both apps' sources 2026-11-04:
# 'Something went wrong!', 'Payment not done, Please try again !!', 'Could not load
# appointments. Please try again.', 'Failed to remove guest', …). Deliberately not the
# expected refusals the flows provoke ("can't", "already", "please select": _TOAST_HINTS).
_ERROR_HINTS = ("went wrong", "try again", "could not", "couldn't", "failed to",
                "unable to", "network error", "request failed", "server error",
                "error sending", "not done", "timed out")


def error_texts(els: Sequence[dict]) -> List[str]:
    """The app's error messages on this screen: sentences, never ids."""
    hits = []
    for e in els or ():
        label = (e.get("AXLabel") or "").strip()
        if 10 <= len(label) <= 200 and " " in label and \
                any(h in label.lower() for h in _ERROR_HINTS):
            hits.append(label)
    return hits


def loading_label(els: Sequence[dict]) -> Optional[str]:
    """What says the screen is still loading, or None. Like idb_driver.busy, but an
    'In progress' StaticText is a status (a booking's), not a spinner: only the
    indicator element carries that label (MEASURED 2026-11-04: a GenericElement)."""
    for e in els or ():
        kind = e.get("type") or ""
        label = (e.get("AXLabel") or "").strip()
        low = label.lower()
        if "ActivityIndicator" in kind or "ProgressIndicator" in kind:
            return label or "a spinner"
        if low == "in progress" and kind != "StaticText":
            return label
        if kind == "StaticText" and ("loading" in low or "please wait" in low):
            return label
    return None


class StepWatch:
    """What the screens read during one step showed beyond what the step checks: the
    longest stretch a loading indicator stayed up, and the app's error messages. Called
    from more than one thread (the step's and the watchdog's), so it locks."""

    def __init__(self, udids: Iterable[str] = (), now: Callable[[], float] = time.time,
                 seen_errors: Optional[set] = None):
        self.udids = {u for u in udids if u}
        self._now = now
        self._lock = threading.Lock()
        self._since: Dict[str, Tuple[float, str]] = {}
        self.longest = (0.0, "")                 # (seconds, what was loading)
        self.errors: List[str] = []
        # Errors already noted in this segment: a toast that outlives a step, or the
        # same message on every screen, is said once.
        self._seen = seen_errors if seen_errors is not None else set()

    def __call__(self, udid: str, els: List[dict]) -> None:
        if self.udids and udid not in self.udids:
            return
        t = self._now()
        what = loading_label(els)
        with self._lock:
            if what:
                since, first = self._since.setdefault(udid, (t, what))
                if t - since > self.longest[0] or not self.longest[1]:
                    self.longest = (t - since, first)
            else:
                self._since.pop(udid, None)
            for text in error_texts(els):
                if text not in self._seen:
                    self._seen.add(text)
                    self.errors.append(text)

    def notes(self, step: str, slow_wait: float = 0.0) -> List[str]:
        """The step's yellow notes. *slow_wait*: how long the step's longest wait for
        the app lasted (0 if none was slow); not said again when a spinner explains it."""
        out = [f"{APP_ERROR} {step} — the app showed: '{t[:160]}'" for t in self.errors]
        secs, what = self.longest
        if what and secs >= loading_seconds():
            out.append(f"{LOADING} {step} — '{what[:60]}' stayed on screen for about "
                       f"{secs:.0f}s")
        elif slow_wait >= slow_screen_seconds():
            out.append(f"{SLOW_SCREEN} {step} — the app took more than {slow_wait:.0f}s "
                       f"to show the screen the step was waiting for")
        return out
