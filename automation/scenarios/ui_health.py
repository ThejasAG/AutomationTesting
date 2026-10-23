"""Passive UI loading / missing-content detection.

ADDITIVE AND PASSIVE BY CONSTRUCTION. Nothing here can fail a step: every entry
point swallows its own errors and the worst outcome is a report saying it could
not observe anything (MONITOR_UNAVAILABLE).

Two hard rules, both deliberate:

1. NEVER touch Appium. An Appium session is not safe for concurrent commands, and
   this monitor runs on a watchdog thread ALONGSIDE a step that is mid-command.
   Sampling therefore goes through idb/simctl only — the same choice the existing
   _capture_screenshot() and _visible_ids() helpers already make.

2. Elapsed time alone is NOT an issue. A slow screen that is still making progress
   and does arrive is a SLOW_SUCCESS, not a failure. A loading issue requires
   EVIDENCE: expected content still missing AND (no meaningful UI progress, or a
   loading indicator stuck up).

The volatile-token filtering is the crux. This build's UI never sits still —
LogBox counts down ("Log 8 of 29" -> "8 of 28"), badges carry a leading count
("6 Each child in a list ..."), clocks tick, queue counters move. Comparing raw
labels would call all of that "progress" and the monitor would never report
anything.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence


# ── result classes ───────────────────────────────────────────────────────────
class LoadResult:
    SUCCESS = "SUCCESS"                      # expected state arrived within the window
    SLOW_SUCCESS = "SLOW_SUCCESS"            # arrived, but after the watchdog window
    PARTIAL_LOAD = "PARTIAL_LOAD"            # screen up, some required content never came
    STUCK_LOADING = "STUCK_LOADING"          # a spinner stayed up and content never came
    NO_PROGRESS = "NO_PROGRESS"              # UI never meaningfully changed
    TIMEOUT = "TIMEOUT"                      # UI moved, expected state still absent
    RECOVERED = "RECOVERED"                  # a retry succeeded after an earlier issue
    MONITOR_UNAVAILABLE = "MONITOR_UNAVAILABLE"   # idb/simctl could not be sampled

    #: results that describe an actual application problem
    PROBLEMS = frozenset({PARTIAL_LOAD, STUCK_LOADING, NO_PROGRESS, TIMEOUT})


# Loading affordances. Evidence only — never the primary signal, per the brief.
SPINNER_HINTS = ("activityindicator", "spinner", "skeleton", "progressindicator",
                 "progressbar", "loading", "please wait")

# Tokens whose VALUE changes constantly and must not count as progress.
_CLOCK = re.compile(r"^\d{1,2}:\d{2}(\s*[APap][Mm])?$")
_LEADING_COUNT = re.compile(r"^\d+\s+")            # "6 Each child in a list ..."
_LOGBOX_COUNT = re.compile(r"^log\s+\d+\s+of\s+\d+$", re.I)
_QUEUE_COUNT = re.compile(r"^(in queue|queue)\s*\d+$", re.I)
_TRAILING_NUM = re.compile(r"\s+\d+$")


def normalise_state(labels: Sequence[str]) -> frozenset:
    """The comparable identity of a screen.

    Strips the parts that churn on their own so that "the UI changed" means the
    SCREEN changed, not that a counter ticked.
    """
    out = set()
    for raw in labels or ():
        s = (raw or "").strip()
        if not s:
            continue
        if _CLOCK.match(s) or _LOGBOX_COUNT.match(s) or _QUEUE_COUNT.match(s):
            continue                                  # pure volatility, drop it
        s = _LEADING_COUNT.sub("", s)                 # drop the badge count prefix
        s = _TRAILING_NUM.sub("", s)                  # "In Queue 04" style suffixes
        s = s.strip()
        if s:
            out.add(s[:80])
    return frozenset(out)


def find_spinners(labels: Sequence[str]) -> List[str]:
    """Visible loading affordances, by label. Evidence, not proof."""
    hits = []
    for raw in labels or ():
        low = (raw or "").strip().lower()
        if low and any(h in low for h in SPINNER_HINTS):
            hits.append((raw or "").strip()[:60])
    return hits


@dataclass(frozen=True)
class ScreenExpectation:
    """What a screen must show before it counts as loaded."""
    name: str
    required_all: tuple = ()      # every one of these must be present
    required_any: tuple = ()      # at least one of these must be present
    #: at least one label must CONTAIN one of these. Content ids are often
    #: dynamic — the wallet's bookings are '<Diner>Card', the board's are
    #: '<Name><Status>Card' — so "the list rendered" cannot be expressed with
    #: exact labels. This is what separates "screen opened" from "content loaded".
    required_any_contains: tuple = ()
    timeout: float = 30.0         # watchdog window (NOT the step timeout)

    def missing(self, labels: Sequence[str]) -> List[str]:
        present = {(l or "").strip() for l in (labels or ())}
        gaps = [x for x in self.required_all if x not in present]
        if self.required_any and not any(x in present for x in self.required_any):
            gaps.append(f"any of {list(self.required_any)}")
        if self.required_any_contains and not any(
                frag in lbl for lbl in present for frag in self.required_any_contains):
            gaps.append(f"any label containing {list(self.required_any_contains)}")
        return gaps

    def satisfied(self, labels: Sequence[str]) -> bool:
        if not (self.required_all or self.required_any or self.required_any_contains):
            return False          # nothing asked for -> nothing to satisfy
        return not self.missing(labels)


@dataclass
class LoadReport:
    """Structured evidence. Rendered into the run notes and an artefact file."""
    result: str
    expectation: str
    waited: float
    expected_all: tuple = ()
    expected_any: tuple = ()
    missing: List[str] = field(default_factory=list)
    spinners: List[str] = field(default_factory=list)
    progressed: bool = False
    samples: int = 0
    sample_errors: int = 0
    first_state: List[str] = field(default_factory=list)
    final_state: List[str] = field(default_factory=list)
    context: Dict[str, str] = field(default_factory=dict)

    @property
    def is_problem(self) -> bool:
        return self.result in LoadResult.PROBLEMS

    def as_recovered(self, attempt: int) -> "LoadReport":
        """Re-file an earlier issue as RECOVERED once a retry succeeded.

        Existing flows already retry (clear overlays, tap again). Without this the
        first attempt's issue would stand on the record and a feature that DID
        work would read as permanently broken. Keeps the original evidence — the
        first attempt genuinely did stall — but stops calling it a failure.
        """
        return LoadReport(
            result=LoadResult.RECOVERED, expectation=self.expectation,
            waited=self.waited, expected_all=self.expected_all,
            expected_any=self.expected_any, missing=list(self.missing),
            spinners=list(self.spinners), progressed=self.progressed,
            samples=self.samples, sample_errors=self.sample_errors,
            first_state=list(self.first_state), final_state=list(self.final_state),
            context={**self.context, "recovered_on_attempt": str(attempt),
                     "first_attempt_was": self.result})

    def to_note(self) -> str:
        """One compact block for the run notes / Live Steps panel."""
        head = {
            LoadResult.PARTIAL_LOAD: "[PARTIAL LOAD]",
            LoadResult.STUCK_LOADING: "[STUCK LOADING]",
            LoadResult.NO_PROGRESS: "[NO UI PROGRESS]",
            LoadResult.TIMEOUT: "[LOADING ISSUE]",
            LoadResult.SLOW_SUCCESS: "[SLOW LOAD]",
            LoadResult.RECOVERED: "[SLOW / RECOVERED]",
            LoadResult.MONITOR_UNAVAILABLE: "[MONITOR UNAVAILABLE]",
        }.get(self.result, "[LOADING]")
        bits = [f"{head} {self.expectation} — waited {self.waited:.1f}s"]
        if self.result == LoadResult.MONITOR_UNAVAILABLE:
            bits.append(f"could not sample the UI ({self.sample_errors}/{self.samples} reads "
                        f"failed) — this is a MONITORING fault, not an app loading failure")
            return "; ".join(bits)
        if self.missing:
            bits.append(f"missing: {self.missing}")
        if self.spinners:
            bits.append(f"loading indicator up: {self.spinners[:3]}")
        bits.append("UI progressed" if self.progressed else "no meaningful UI change")
        if self.context:
            bits.append(" ".join(f"{k}={v}" for k, v in self.context.items()))
        return "; ".join(bits)


class SingleFlightSampler:
    """Wraps a sampler so only ONE read is ever in flight.

    Each idb dump spawns a subprocess and takes ~2-3s on this app. Overlapping the
    watchdog's reads with the step's own would double that load for no gain, so a
    read that arrives while another is running gets the previous result instead.
    """

    def __init__(self, sample_fn: Callable[[], Sequence[str]]):
        self._fn = sample_fn
        self._busy = False
        self._last: Optional[List[str]] = None
        self.calls = 0
        self.skipped = 0

    def __call__(self) -> Optional[List[str]]:
        if self._busy:
            self.skipped += 1
            return self._last
        self._busy = True
        try:
            self.calls += 1
            got = self._fn()
            self._last = list(got) if got is not None else None
            return self._last
        except Exception:
            return None
        finally:
            self._busy = False


def monitor_ui_loading(sample_fn: Callable[[], Sequence[str]],
                       expectation: ScreenExpectation,
                       watchdog_s: Optional[float] = None,
                       sleep_s: float = 5.0,
                       context: Optional[Dict[str, str]] = None,
                       hard_s: Optional[float] = None,
                       clock: Callable[[], float] = time.monotonic,
                       sleeper: Callable[[float], None] = time.sleep) -> LoadReport:
    """Watch for *expectation* and classify what happened. Never raises.

    Elapsed time alone is never a failure: if the screen arrives late it is a
    SLOW_SUCCESS. A problem is only reported when the expected content is still
    missing AND the UI is either not moving or stuck behind a loading indicator.

    Watching CONTINUES past *watchdog_s* until *hard_s* (default 2x the window),
    because "appeared after 42.3s" is a materially different report from "never
    appeared" — stopping dead on the window would collapse the two and make
    SLOW_SUCCESS unreachable.
    """
    window = expectation.timeout if watchdog_s is None else watchdog_s
    limit = (window * 2) if hard_s is None else max(hard_s, window)
    sampler = SingleFlightSampler(sample_fn)
    started = clock()
    first_state: Optional[frozenset] = None
    first_labels: List[str] = []
    last_labels: List[str] = []
    seen_states = set()
    spinner_every_sample = True
    spinners_last: List[str] = []
    samples = errors = 0

    try:
        while True:
            labels = sampler()
            samples += 1
            if labels is None:
                errors += 1
            else:
                last_labels = list(labels)
                state = normalise_state(labels)
                if first_state is None:
                    first_state, first_labels = state, list(labels)
                seen_states.add(state)
                spinners_last = find_spinners(labels)
                if not spinners_last:
                    spinner_every_sample = False
                if expectation.satisfied(labels):
                    waited = clock() - started
                    return LoadReport(
                        result=(LoadResult.SUCCESS if waited <= window
                                else LoadResult.SLOW_SUCCESS),
                        expectation=expectation.name, waited=waited,
                        expected_all=expectation.required_all,
                        expected_any=expectation.required_any,
                        spinners=spinners_last, progressed=len(seen_states) > 1,
                        samples=samples, sample_errors=errors,
                        first_state=first_labels[:40], final_state=last_labels[:40],
                        context=dict(context or {}))
            if clock() - started >= limit:
                break
            sleeper(sleep_s)
    except Exception:
        # Monitoring must never take the run down with it.
        pass

    waited = clock() - started
    progressed = len(seen_states) > 1

    # Could we observe at all? If not, say so — never blame the app.
    if samples and errors >= samples:
        return LoadReport(result=LoadResult.MONITOR_UNAVAILABLE,
                          expectation=expectation.name, waited=waited,
                          samples=samples, sample_errors=errors,
                          context=dict(context or {}))

    missing = expectation.missing(last_labels)
    # PARTIAL = some of what we expected IS on screen, but not all of it. Comparing
    # the gap count against required_all alone was wrong: a satisfied shell with a
    # missing content list produced one gap and one required_all entry, so the
    # comparison said "nothing found" and it was filed as TIMEOUT instead.
    _present = {(l or "").strip() for l in (last_labels or ())}
    _hits = (
        [x for x in expectation.required_all if x in _present]
        + [x for x in expectation.required_any if x in _present]
        + [f for f in expectation.required_any_contains
           if any(f in lbl for lbl in _present)]
    )
    found_some = bool(missing) and bool(_hits)

    # ORDER MATTERS. A real partial load looks exactly like "no progress": the shell
    # renders once and then sits there because the list request never lands. Checking
    # `not progressed` first swallowed that case and reported NO_PROGRESS, throwing away
    # the one useful fact — WHICH expected content is missing. PARTIAL_LOAD keeps the
    # "no meaningful UI change" evidence in its note, so nothing is lost by preferring it.
    if spinner_every_sample and spinners_last:
        result = LoadResult.STUCK_LOADING
    elif found_some:
        result = LoadResult.PARTIAL_LOAD
    elif not progressed:
        result = LoadResult.NO_PROGRESS
    else:
        result = LoadResult.TIMEOUT

    return LoadReport(result=result, expectation=expectation.name, waited=waited,
                      expected_all=expectation.required_all,
                      expected_any=expectation.required_any,
                      missing=missing, spinners=spinners_last, progressed=progressed,
                      samples=samples, sample_errors=errors,
                      first_state=first_labels[:40], final_state=last_labels[:40],
                      context=dict(context or {}))


# ── known screens ────────────────────────────────────────────────────────────
# Ids taken from real failure dumps, not from the source: the wallet reported
# ['tab','couponBlock','walletUpcomingSearchInput','walletUpcomingFilterIcon',
#  'MypreorderList','RoopaDpreOrderCard', ...]. The shell and the CONTENT are
# separated deliberately so "screen opened but the bookings never loaded" is a
# PARTIAL_LOAD rather than a pass.
SCREENS: Dict[str, ScreenExpectation] = {
    "wallet": ScreenExpectation(
        name="wallet",
        required_all=("walletUpcomingSearchInput",),          # the shell
        required_any_contains=("Card", "preorder", "Preorder"),  # the bookings list
        timeout=30.0,
    ),
}
