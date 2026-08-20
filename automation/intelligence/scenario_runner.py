"""Interpret plain-language scenario steps and DRIVE the app, one step at a time.

A non-technical user writes directions like:

    open the app
    open Nylai Kitchen2
    select a time slot
    book a table
    verify the app did not crash

Each step is resolved against the LIVE accessibility tree — so we tap what is
actually on screen, not a guessed selector — the action is performed, and a real
Appium/pytest line is recorded. The result is a runnable script that used real
locators the whole way, plus a per-step report (and, for any step we could not
resolve, the exact element that needs a testID).

This is deliberately rule-based: the intents below cover the vocabulary a person
naturally uses to describe an app flow. An LLM is only a last-resort tie-breaker
(pick_element), because "which of these on-screen ids matches this phrase" is a
classification a small local model can do, unlike writing a whole script.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from appium.webdriver.common.appiumby import AppiumBy

logger = logging.getLogger("scenario_runner")

from automation.intelligence.element_catalog import (
    AutoIdCatalog, METHOD_CONTAINER, METHOD_EXACT_ID, METHOD_HIERARCHY,
    METHOD_PARTIAL_ID, METHOD_TEXT,
)

# Containers worth scanning when a keyword matches nothing that carries an id.
# `products-list` is the FlatList: its ROWS have no testID and no
# accessibilityLabel, and are told apart only by the text they render
# ("TagliatellealMonti 5.1 ★ 17.51 €"). Scanning it is the only way a product can
# ever be added — and therefore the only way cart/checkout/payment are reachable.
_SCANNABLE_CONTAINERS = ("products-list", "category-list", "card-container-outer-layer")

# Spinners/loaders to wait out before deciding an element is simply absent.
_LOADING_HINTS = ("loading", "spinner", "activityindicator", "please wait", "loader")

# The "book a date" popup that intercepts add-to-cart when no slot exists yet.
# Both buttons are labelled, so this is handled by real ids.
_BOOK_POPUP_ACCEPT = "preOrderBooking"   # -> the booking flow
_BOOK_POPUP_DISMISS = "orderLater"       # DISCARDS the pending item — never auto-tap
_BOOK_POPUP_TEXT = "book a date in order to"

# Ordinal words → 0-based index, so "select the 2nd time slot" picks the SECOND
# chip, not the first. "last" maps to -1.
_ORDINAL_WORDS = {
    "first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4,
    "sixth": 5, "seventh": 6, "eighth": 7, "ninth": 8, "tenth": 9, "last": -1,
}


def _ordinal_index(text: str) -> int:
    """0-based index named in *text* ('2nd'→1, 'second'→1, 'last'→-1); 0 if none."""
    t = text.lower()
    for word, idx in _ORDINAL_WORDS.items():
        if re.search(rf"\b{word}\b", t):
            return idx
    m = re.search(r"\b(\d+)\s*(?:st|nd|rd|th)\b", t)
    if m:
        return int(m.group(1)) - 1
    return 0


# ── Intent detection (checked in order) ──────────────────────────────────────
_OPEN_APP = re.compile(r"\b(open|launch|start|go\s*to)\b.*\bapp\b", re.I)
_ASSERT = re.compile(r"^\s*(assert|verify|check|confirm|ensure|expect|should\s+see|see\b)", re.I)
_TYPE = re.compile(r"^\s*(type|enter|input|fill|search(?:\s+for)?)\b", re.I)
_BACK = re.compile(r"^\s*(go\s*back|back\b|return\b|previous\b)", re.I)
_WAIT = re.compile(r"^\s*(wait|pause|hold)\b", re.I)
# Gestures. Without these every step was a tap, so anything reached by swiping —
# the split/void/comp section of the payment UI — could not be expressed at all.
_SWIPE = re.compile(r"^\s*(swipe|scroll|drag|flick)\b", re.I)
_DIRECTION = re.compile(r"\b(left|right|up|down)\b", re.I)
_TAP = re.compile(r"^\s*(tap|click|press|touch|open|select|choose|pick|go\s*to|goto|book|place|add|reserve|confirm|submit|proceed|continue)\b", re.I)

# A sentence that DESCRIBES what the app does is an observation, not a command.
# "it gives a popup of book a slot" names no target: it used to fall through to
# the best-effort tap below, match the word "book", and really tap the Book
# button — driving the app somewhere the user never asked for and reporting it
# as a success. Check it and move on instead.
_OBSERVATION = re.compile(
    r"^\s*(?:it|there|a|an|the)\b.*?\b(?:gives?|shows?|will\s+show|appears?|displays?|pops?\s*up|opens?\s+up|should)\b",
    re.I,
)

# Verbs that say HOW to touch, never WHAT to touch. Passing them to the locator
# made `name CONTAINS "click"` a real query that could match any element.
_UI_VERBS = {"tap", "click", "press", "touch", "go", "goto", "open",
             "select", "choose", "pick"}

# Verbs that CAN name their own target ("book a table" -> bookAppoitment), so
# they stay searchable. They are not nouns, though, so they never count toward
# how specifically a step identified what it wanted.
_DOMAIN_VERBS = {"book", "place", "add", "reserve", "confirm",
                 "submit", "proceed", "continue", "order"}

# Controls that destroy state. A FUZZY match must never land on one of these by
# accident: "click Menu" resolved to "menuLogout" on a screen with no "Menu",
# which would have signed the session out and invalidated the whole run — while
# reporting a green tap. An EXACT id, or a step that says the word itself, is
# still honoured: the user asked for it by name.
_DESTRUCTIVE = ("logout", "log out", "signout", "sign out", "delete", "remove",
                "deactivate", "unsubscribe", "clear", "reset", "refund")

# Words carrying no locator meaning — stripped when extracting the target phrase.
_STOP = {
    "the", "a", "an", "on", "to", "in", "into", "at", "of", "for", "with",
    "please", "app", "screen", "page", "button", "icon", "option", "tab",
    "and", "then", "is", "shown", "present", "visible", "displayed", "my",
    "first", "any", "some", "it", "that", "this",
}


@dataclass
class StepResult:
    step: str
    ok: bool
    action: str = ""            # human-readable summary of what happened
    detail: str = ""            # error / note
    code: str = ""              # the recorded pytest line(s), if any
    screenshot: Optional[str] = None
    healed: bool = False        # resolved via a fallback (fuzzy) locator, not the exact one
    healed_note: str = ""       # what the self-healing matched instead


@dataclass
class ScenarioResult:
    results: List[StepResult] = field(default_factory=list)
    script: str = ""

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results) and bool(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.ok)


def _keywords(phrase: str) -> List[str]:
    return [w for w in re.findall(r"[A-Za-z0-9]+", phrase) if w.lower() not in _STOP]


def _locator_words(phrase: str) -> List[str]:
    """Keywords worth searching for — the interaction verb is not one of them."""
    return [w for w in _keywords(phrase) if w.lower() not in _UI_VERBS]


def _nouns(words: List[str]) -> List[str]:
    """The words that actually name a target, de-duplicated, order preserved."""
    return list(dict.fromkeys(
        w for w in words if w.lower() not in _UI_VERBS and w.lower() not in _DOMAIN_VERBS
    ))


@dataclass
class Match:
    """A resolved element, plus how confidently it was resolved.

    ``text`` is the element's OWN name/label — the only honest basis for saying
    what was tapped, and for judging whether the step really identified it.
    ``method``/``confidence``/``auto_id`` carry how it was found, so a guess is
    never reported as a certainty.
    """
    el: object = None
    by: str = ""
    value: object = None
    text: str = ""
    exact: bool = False     # resolved by exact accessibility id — always trusted
    method: str = ""
    confidence: float = 0.0
    auto_id: str = ""       # temporary id minted when the app named nothing

    def __bool__(self) -> bool:
        return self.el is not None


def _target_phrase(step: str, verb_re: re.Pattern) -> str:
    """Everything after the leading verb is the target description.

    Strips trailing assertion suffixes so 'verify addNewEvent is visible' targets
    'addNewEvent', not 'addNewEvent is visible'. Negation is detected separately.
    """
    m = verb_re.match(step)
    rest = step[m.end():] if m else step
    rest = rest.strip(" .:-\t")
    rest = re.sub(
        r"\s+(is|are|should\s+be|to\s+be|being)\s+(not\s+|no\s+longer\s+)?"
        r"(visible|present|shown|displayed|there|available|enabled|on\s+screen)\s*$",
        "", rest, flags=re.I)
    return rest.strip(" .:-\t")


def _is_negative_assert(step: str) -> bool:
    """True for 'verify X is NOT visible / not present / no longer shown / gone'."""
    return bool(re.search(r"\b(not\s+(visible|present|shown|displayed|there)|"
                          r"no\s+longer|isn'?t\s+(visible|present)|is\s+gone|"
                          r"not\s+be\s+(visible|present)|absent)\b", step, re.I))


class ScenarioRunner:
    def __init__(self, driver, bundle_id: str, screenshot_dir: Optional[str] = None,
                 catalog: Optional[AutoIdCatalog] = None):
        self.d = driver
        self.bid = bundle_id
        self.shot_dir = screenshot_dir
        self._used_ids: set[str] = set()   # for building the script
        # Names elements the app never named, and records how sure we were.
        self.catalog = catalog or AutoIdCatalog()
        # Numbers remembered by 'capture <label> as <name>', read back by
        # 'verify calculation'. Per-runner, so scenarios never leak into each other.
        self._captured: dict = {}
        # Persistent self-healing memory: the locator that last worked per (app,target).
        try:
            from automation.intelligence.learned_locators import get_store
            self.learned = get_store()
        except Exception:
            self.learned = None
        self._screen = "unknown"
        # page_source is a slow XCUITest round trip (1-3s). Several per-step checks
        # need it back-to-back, so share one fetch via a short TTL cache and
        # invalidate it the moment the screen is acted on.
        self._src_cache: Tuple[Optional[str], float] = (None, 0.0)

    def _page_source(self, ttl: float = 60.0) -> str:
        """page_source with a cache, so the several checks that run back-to-back
        within one step don't each pay the XCUITest round trip.

        The TTL must exceed the cost of the call it guards. Measured on this app
        page_source is 241 KB and takes ~17.5s, so the old 1.5s TTL could never
        hit: any two checks in a step were separated by more than 1.5s of Appium
        work, and each paid full price. One failing step measured 338s — roughly
        nineteen full fetches.

        Correctness rests on INVALIDATION, not expiry: _invalidate_source() fires
        after every action (run_one, clear_blockers). Anything that mutates the
        screen and then re-reads it must invalidate first — the wait-for-element
        poll in _tap_step does exactly that."""
        src, ts = self._src_cache
        if src is not None and (time.time() - ts) < ttl:
            return src
        try:
            src = self.d.page_source or ""
        except Exception:
            src = ""
        self._src_cache = (src, time.time())
        return src

    def _invalidate_source(self) -> None:
        """Drop the cached page_source — call after acting on the screen so the
        next check reads the real, post-action state."""
        self._src_cache = (None, 0.0)

    # ── element resolution against the live screen ───────────────────────────

    # Touchable list/row/chip wrappers — tapping the WRAPPER navigates, whereas
    # tapping the text label inside it often does nothing.
    _CONTAINERS = ("card-container-outer-layer", "button-container-outer-layer",
                   "chip-container-outer-layer", "button-container")

    def _element_text(self, el, attrs: Tuple[str, ...] = ("name", "label", "value")) -> str:
        """The element's own name/label/value — what it really is.

        Every attribute is a separate Appium round trip, and these are read for
        each candidate, so callers screening a list pass only the attributes
        their predicate actually matched on.
        """
        parts = []
        for attr in attrs:
            try:
                v = el.get_attribute(attr)
            except Exception:
                v = None
            if v:
                parts.append(str(v))
        return " ".join(dict.fromkeys(parts))

    def _describe(self, m: "Match") -> str:
        """What was ACTUALLY resolved — never the user's own phrase.

        Echoing the phrase back ('tapped "menu and open menu section"') made a tap
        on the WRONG element indistinguishable from a correct one: the report just
        repeated the request.
        """
        if m.by == "accessibility_id":
            return str(m.value)
        text = (m.text or "").strip()
        if text:
            # Say it was an INVENTED name, never pass a guess off as an id.
            if m.auto_id:
                return f"{text[:44]} [{m.auto_id} ~{m.confidence:.2f}]"
            return text[:60]
        if m.by == "container":
            cid, kw = m.value
            return f"{cid} containing '{kw}'" if kw else str(cid)
        return "(unnamed element)"

    def _seen(self, phrase: str, words: List[str], step: str) -> Optional["Match"]:
        """Is *phrase* on screen? Cheapest oracle first: an exact accessibility id,
        then an exact/token idb label, and only then the fuzzy resolver.

        The same ladder _tap_step uses, for the same measured reason — _resolve
        costs 30-150s a step on this app's tree while idb answers in ~1.6-3.4s.
        idb also reports the GenericElements Appium's collapsed snapshot drops, so
        without it a control can be plainly on screen and still be "not found"."""
        m = self._exact_id(phrase)
        if m:
            return m
        if phrase and self._idb_element(phrase):
            # No element handle: every caller of this only REPORTS presence.
            return Match(el=True, by="idb", value=f'label == "{phrase}"', text=phrase,
                         exact=True, method="idb-label", confidence=1.0)
        return self._resolve(words, step=step)

    def _vague(self, m: "Match", words: List[str]) -> Optional[Tuple[List[str], List[str]]]:
        """(nouns, matched) when the step named 2+ targets but the element only
        answers to one of them — otherwise None.

        'menu section' matching an element whose text is merely 'menu' is a guess,
        and a wrong tap silently invalidates every step after it. An exact
        accessibility id is never a guess, so it is always trusted.
        """
        if m.exact:
            return None
        nouns = _nouns(words)
        if len(nouns) < 2:
            return None
        low = (m.text or "").lower()
        matched = [w for w in nouns if w.lower() in low]
        return (nouns, matched) if len(matched) <= 1 else None

    def _destructive_guess(self, m: "Match", step: str) -> Optional[str]:
        """The destructive word a FUZZY match landed on that the step never said.

        None when the match is exact (named by id), when the step asked for it,
        or when nothing destructive is involved.
        """
        if m.exact:
            return None
        low_step = step.lower()
        low_el = (m.text or "").lower()
        for word in _DESTRUCTIVE:
            if word in low_el and word not in low_step:
                return word
        return None

    # ── screen identity (for the technical-debt report) ──────────────────────

    def current_screen(self) -> str:
        """A best-effort name for the screen, so the report can tell developers
        WHERE to add identifiers. Derived from ids the app does expose."""
        markers = [
            ("products-list", "StoreView/Products"),
            ("bookAppoitment", "StoreView/Reservation"),
            ("cartCheckout", "Cart"),
            ("ePayment", "Payment/Stripe"),
            ("menuLogout", "Menu/Drawer"),
            ("signIn", "Login"),
            ("card-container-outer-layer", "Home"),
        ]
        for marker, name in markers:
            try:
                if self.d.find_elements(AppiumBy.ACCESSIBILITY_ID, marker):
                    self._screen = name
                    return name
            except Exception:
                continue
        return self._screen

    # ── resilience ───────────────────────────────────────────────────────────

    def wait_for_idle(self, timeout: float = 4.0) -> bool:
        """Block while a spinner is on screen. Returns False on timeout.

        Uses a cheap find_elements query for a visible ActivityIndicator rather
        than a full page_source (which costs seconds on a complex RN screen and
        was the dominant per-step cost). An element is not 'missing' just because
        the screen has not finished loading — conflating those makes a suite flaky.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                busy = self.d.find_elements(
                    AppiumBy.IOS_PREDICATE,
                    '(type == "XCUIElementTypeActivityIndicator" AND visible == true) '
                    'OR (type == "XCUIElementTypeStaticText" AND visible == true AND '
                    '(label CONTAINS[c] "loading" OR label CONTAINS[c] "please wait"))')
            except Exception:
                return True
            if not busy:
                return True
            time.sleep(0.4)
        return False

    def _resolve(self, words: List[str], prefer_container: bool = False,
                 step: str = "") -> "Match":
        """Best match for *words*, or a falsy Match.

        Order, strongest first:
          1. exact accessibility id          (real id      — confidence 1.00)
          2. tappable container + keyword    (container    — 0.85)
          3. id CONTAINS keyword             (partial id   — 0.70)
          4. visible label CONTAINS keyword  (partial id   — 0.70)
          5. text/hierarchy scan of a known list  (text/hierarchy — 0.60/0.50)

        Step 5 is what makes an unlabelled app automatable: it walks a known
        container's subtree and matches the text a row RENDERS. Nothing is ever
        refused merely for lacking an identifier.
        """
        # The words rejoined are the label as a person would write it ("1 hr"),
        # which is often the exact accessibility id — tried before any squashing.
        phrase = " ".join(words)

        # ── Self-heal fast path ────────────────────────────────────────────────
        # Try the locator that last worked for this (app, target): one direct find
        # instead of re-deriving through the fuzzy fallbacks. A miss (id drifted)
        # just falls through to the normal strategies, which then RE-LEARN below.
        drifted = False
        if getattr(self, "learned", None):
            try:
                rec = self.learned.get(self.bid, phrase)
                if rec:
                    lm = self._try_learned(rec)
                    if lm:
                        return lm
                    drifted = True   # had a learned locator but it no longer resolves
            except Exception:
                pass

        m = self._resolve_raw(words, prefer_container, phrase=phrase)

        # Nothing with an id matched — the element may simply have none.
        if not m:
            m = self._scan_containers(words, step)

        if m:
            if not m.text:
                m.text = self._element_text(m.el)
            # Mint a temporary id for anything the app did not name itself.
            if m.method != METHOD_EXACT_ID and not m.auto_id:
                entry = self.catalog.mint(
                    text=m.text, method=m.method, screen=self.current_screen(),
                    step=step, prefix="auto",
                )
                m.auto_id = entry.auto_id
                m.confidence = entry.confidence
            elif m.method == METHOD_EXACT_ID:
                self.catalog.record_resolution(
                    step, METHOD_EXACT_ID, str(m.value), self._screen, 1.0)
                m.confidence = 1.0
                # Remember the real id that worked so next run resolves it directly;
                # healed=drifted flags a genuine self-heal (the old locator had failed).
                if getattr(self, "learned", None):
                    try:
                        from automation.intelligence.learned_locators import AID
                        self.learned.learn(self.bid, phrase, AID, str(m.value), healed=drifted)
                    except Exception:
                        pass
        return m

    def _try_learned(self, rec) -> "Match":
        """Find an element by a remembered (method, value). Returns a falsy Match on
        miss, so the caller falls through to the normal strategies and re-learns."""
        from automation.intelligence.learned_locators import AID
        method, value = rec
        by = AppiumBy.ACCESSIBILITY_ID if method == AID else AppiumBy.IOS_PREDICATE
        try:
            els = self.d.find_elements(by, value)
        except Exception:
            return Match()
        vis = []
        for e in els:
            try:
                if e.is_displayed():
                    vis.append(e)
            except Exception:
                vis.append(e)
        if not vis:
            return Match()
        m = Match(el=vis[0], value=value, method=METHOD_EXACT_ID, confidence=0.97)
        m.text = self._element_text(m.el)
        return m

    def _scan_containers(self, words: List[str], step: str) -> "Match":
        """Find an element by the TEXT it renders, inside a known list.

        The product rows under `products-list` are XCUIElementTypeOther with no
        testID and no accessibilityLabel — only their text ("TagliatellealMonti
        5.1 ★ 17.51 €") tells them apart. Without this, no product can be added
        and the whole payment path is untestable.
        """
        targets = [w for w in words if len(w) >= 3 and w.lower() not in _DOMAIN_VERBS]
        if not targets:
            return Match()

        for cid in _SCANNABLE_CONTAINERS:
            try:
                containers = self.d.find_elements(AppiumBy.ACCESSIBILITY_ID, cid)
            except Exception:
                continue
            if not containers:
                continue

            # Rank rows by how many of the step's words their text carries, so
            # "monti" picks TagliatellealMonti rather than the first row.
            best, best_score, best_text = None, 0, ""
            for container in containers[:4]:
                try:
                    kids = container.find_elements(AppiumBy.XPATH, ".//*")
                except Exception:
                    continue
                for el in kids[: self._MAX_CANDIDATES * 3]:
                    text = self._element_text(el, ("name",))
                    if not text or len(text) > self._AGGREGATE_CHARS:
                        continue
                    low = text.lower()
                    score = sum(1 for w in targets if w.lower() in low)
                    if score > best_score:
                        best, best_score, best_text = el, score, text

            if best is not None and best_score:
                # Matched real rendered text -> text-match; otherwise structural.
                method = METHOD_TEXT if best_score else METHOD_HIERARCHY
                return Match(best, "scanned", (cid, best_text), text=best_text,
                             method=method)
        return Match()

    def _resolve_raw(self, words: List[str], prefer_container: bool = False,
                     phrase: str = "") -> "Match":
        # The label as WRITTEN. Real ids contain spaces ("1 hr", "Reserve a
        # table", "Not Sure"); squashing them to "1hr"/"1-hr" never matched, so a
        # step naming a control exactly still fell through to fuzzy matching.
        if phrase:
            for cand in (phrase.strip(), phrase.strip().title()):
                if not cand:
                    continue
                els = self.d.find_elements(AppiumBy.ACCESSIBILITY_ID, cand)
                if els:
                    return Match(els[0], "accessibility_id", cand, exact=True,
                                 method=METHOD_EXACT_ID)

        joined = "".join(words)
        camel = (words[0].lower() + "".join(w.capitalize() for w in words[1:])) if words else ""
        kebab = "-".join(w.lower() for w in words)
        long_words = [w for w in words if len(w) >= 3]

        # 1. exact accessibility id
        for cand in filter(None, {joined, camel, kebab, *words}):
            els = self.d.find_elements(AppiumBy.ACCESSIBILITY_ID, cand)
            if els:
                return Match(els[0], "accessibility_id", cand, exact=True,
                             method=METHOD_EXACT_ID)

        # 1b. Normalized id/label match — bridges spacing + case between the
        #     written step ("Nylai kitchen 2") and the real testID ("NylaiKitchen2").
        #     Exact-normalized wins over a mere prefix so we tap the card, not its
        #     "…Fav"/"…Unsub" sibling buttons.
        def _norm(s):
            return re.sub(r"[^a-z0-9]", "", (s or "").lower())
        target = _norm(phrase) or _norm(joined)
        if target and long_words:
            seed = max(long_words, key=len)
            try:
                cands = self.d.find_elements(
                    AppiumBy.IOS_PREDICATE,
                    f'name CONTAINS[c] "{seed}" OR label CONTAINS[c] "{seed}"')
            except Exception:
                cands = []
            exact_el, contains_el = None, None
            for el in cands[: self._MAX_CANDIDATES]:
                for attr in ("name", "label"):
                    try:
                        nv = _norm(el.get_attribute(attr))
                    except Exception:
                        nv = ""
                    if not nv or len(nv) >= self._AGGREGATE_CHARS:
                        continue
                    if nv == target and exact_el is None:
                        exact_el = el
                    elif target in nv and contains_el is None:
                        contains_el = el
                if exact_el is not None:
                    break
            chosen = exact_el or contains_el
            if chosen is not None:
                return Match(chosen, "accessibility_id", target,
                             exact=bool(exact_el), method=METHOD_EXACT_ID)

        # 2. a tappable container whose subtree contains a keyword (cards, chips)
        # Same rule as 3/4 below: a MULTI-word step may not be satisfied by ONE of its
        # words. 'click book now' matched the container of `bookAppoitment` on "book"
        # alone (0.85) and re-tapped a button the flow had already pressed, sending the
        # app somewhere the next step could not recover from.
        if prefer_container:
            for cid in self._CONTAINERS:
                containers = self.d.find_elements(AppiumBy.ACCESSIBILITY_ID, cid)
                for w in sorted(long_words, key=len, reverse=True):
                    for c in containers:
                        if c.find_elements(AppiumBy.IOS_PREDICATE,
                                           f'name CONTAINS[c] "{w}" OR label CONTAINS[c] "{w}"'):
                            if len(long_words) > 1:
                                text = self._element_text(c, ("name", "label")).lower()
                                covered = sum(1 for x in long_words if x.lower() in text)
                                if covered < len(long_words):
                                    self._last_ambiguous = (
                                        f"'{' '.join(words)}' only partially matches the "
                                        f"container '{text[:60]}' (matched {covered}/"
                                        f"{len(long_words)} words — on '{w}'). "
                                        f"Refusing to guess; use the exact id.")
                                    continue
                            return Match(c, "container", (cid, w), method=METHOD_CONTAINER)

        # 3. id CONTAINS a keyword (e.g. "book" -> bookAppoitment)
        # 4. visible label CONTAINS a keyword
        #
        # NEVER match on ONE word of a MULTI-word step. "click book now" used to match
        # preOrderBooking, because "book" is a substring of preOrder-BOOK-ing — the run
        # silently chose PRE-ORDER, and the next step ("order later") then hung for the
        # full 150s on a screen the flow never meant to be on. A wrong tap is worse than
        # no tap: it changes app state and the failure surfaces somewhere unrelated.
        # So when the step gives several words, the element must account for ALL of them;
        # if nothing does, say so and list what was there instead of guessing.
        for attrs, name_only in ((("name",), True), (("name", "label"), False)):
            hits: List[tuple] = []                # (matched_word_count, text_len, el, w)
            for w in sorted(long_words, key=len, reverse=True):
                if name_only:
                    pred = f'name CONTAINS[c] "{w}" AND name.length < {self._AGGREGATE_CHARS}'
                else:
                    pred = (f'(name CONTAINS[c] "{w}" AND name.length < {self._AGGREGATE_CHARS})'
                            f' OR (label CONTAINS[c] "{w}" AND label.length < {self._AGGREGATE_CHARS})')
                el = self._most_specific(
                    self.d.find_elements(AppiumBy.IOS_PREDICATE, pred), w, attrs)
                if el is None:
                    continue
                text = self._element_text(el, attrs).lower()
                covered = sum(1 for x in long_words if x.lower() in text)
                hits.append((covered, len(text), el, w, pred))

            if not hits:
                continue
            best = max(hits, key=lambda h: (h[0], -h[1]))
            covered, _len, el, w, pred = best
            if covered < len(long_words):
                # Partial evidence only. Record WHY nothing was tapped so the step's
                # failure names the real problem rather than "element not found".
                self._last_ambiguous = (
                    f"'{' '.join(words)}' only partially matches "
                    f"'{self._element_text(el, attrs)}' (matched {covered}/{len(long_words)} "
                    f"words — on '{w}'). Refusing to guess; use the exact id."
                )
                continue
            return Match(el, "ios_predicate", pred, method=METHOD_PARTIAL_ID)

        return Match()

    # How many of the (already length-filtered) candidates to rank. Each costs an
    # Appium round trip.
    _MAX_CANDIDATES = 12

    # A screen wrapper's name is the concatenation of EVERY descendant id — on the
    # home screen that is 2400+ characters, and it "contains" almost any keyword.
    # A real target ("bookAppoitment", "menuLogout") is short. Tapping a wrapper
    # does nothing yet still reported a successful tap, so they are excluded IN
    # THE PREDICATE: filtering them server-side cut `name CONTAINS "menu"` from
    # 51 elements/64s to 11 elements/10s, and what comes back is real leaves.
    _AGGREGATE_CHARS = 120

    def _most_specific(self, els: List, w: str, attrs: Tuple[str, ...]):
        """The tightest real element matching *w*, or None if all are wrappers.

        find_elements returns DOCUMENT ORDER — ancestors first — so els[0] is the
        OUTERMOST match: for "product" that is the whole screen. The shortest own
        text is the leaf actually carrying the word.
        """
        best, best_len = None, None
        for el in els[: self._MAX_CANDIDATES]:
            text = self._element_text(el, attrs)
            if w.lower() not in text.lower():
                continue
            if len(text) > self._AGGREGATE_CHARS:
                continue
            if best is None or len(text) < best_len:
                best, best_len = el, len(text)
        return best

    def _tap_resolved(self, m: "Match") -> str:
        """Tap the resolved element and return the pytest line(s) that reproduce it."""
        el, by, value = m.el, m.by, m.value
        el.click()

        # Found by scanning a list for its text — there is no id to record, so
        # the generated script has to reproduce the same scan. It is pinned to
        # rendered text, which is exactly the fragility the report flags.
        if by == "scanned":
            cid, text = value
            snippet = text.split()[0][:30] if text else ""
            return (
                f'    # no testID on this row — matched by rendered text ({m.auto_id})\n'
                f'    _list = driver.find_element(AppiumBy.ACCESSIBILITY_ID, "{cid}")\n'
                f'    for _row in _list.find_elements(AppiumBy.XPATH, ".//*"):\n'
                f'        if "{snippet}".lower() in (_row.get_attribute("name") or "").lower():\n'
                f'            _row.click(); break'
            )
        if by == "accessibility_id":
            self._used_ids.add(value)
            return f'    by_id(driver, "{value}").click()'
        if by == "container":
            cid, kw = value
            if kw:
                return (
                    f'    for _c in driver.find_elements(AppiumBy.ACCESSIBILITY_ID, "{cid}"):\n'
                    f'        if _c.find_elements(AppiumBy.IOS_PREDICATE, \'name CONTAINS[c] "{kw}" OR label CONTAINS[c] "{kw}"\'):\n'
                    f'            _c.click(); break'
                )
            return f'    driver.find_elements(AppiumBy.ACCESSIBILITY_ID, "{cid}")[0].click()'
        return f'    driver.find_element(AppiumBy.IOS_PREDICATE, {value!r}).click()'

    # ── per-step execution ───────────────────────────────────────────────────

    def _do_step(self, step: str) -> StepResult:
        s = step.strip()
        if not s or s.startswith("#"):
            return StepResult(step=s, ok=True, action="skipped (comment/blank)")

        # VERIFY ELEMENT SIZE — "verify size/width/height of <X> = <N>" (px).
        if re.search(r"verify\s+(size|width|height)\b", s, re.I):
            return self._verify_size(s)

        # VERIFY BILL / DISCOUNT — reuse the bot's proven bill_validator on the
        # current bill screen (item sums, totals, coupon/discount).
        if re.search(r"verify\s+(bill|total|discount|coupon)\b", s, re.I):
            return self._verify_bill(s)

        # CAPTURE — "capture <label> as <name>". Must come before the tap resolver, or
        # the whole line is treated as something to click.
        if re.match(r"^\s*capture\s+.+\s+as\s+[A-Za-z_][\w]*\s*$", s, re.I):
            return self._capture_number(s)

        # VERIFY CALCULATION — "verify calculation <math> = <expected>", checks the
        # computed result against a number visible on screen (bills, XP math, etc.).
        if re.search(r"verify\s+calculation|verify\s+.+=", s, re.I) and "=" in s:
            calc = self._verify_calculation(s)
            if calc is not None:
                return calc

        # API ASSERTION — "verify api <path> <field> == <value>" reads a backend
        # value (for data that is NOT on screen, e.g. inventory stock).
        if re.search(r"\bverify\s+api\b|\bapi\s+check\b", s, re.I):
            return self._verify_api(s)

        # ORDER LATER — an explicit user choice on the "book a date" popup. The
        # auto-handler never taps this (it discards the cart), but when the user
        # asks for it by name we honour it.
        if re.search(r"order\s*later|\blater\b", s, re.I):
            els = self.d.find_elements(AppiumBy.ACCESSIBILITY_ID, _BOOK_POPUP_DISMISS)
            if els:
                els[0].click()
                self._wait_settle()
                return StepResult(step=s, ok=True, action='chose "order later"',
                                  code=f'    by_id(driver, "{_BOOK_POPUP_DISMISS}").click()')

        # OPEN APP
        if _OPEN_APP.search(s):
            self.d.activate_app(self.bid)
            self._wait_settle()
            return StepResult(step=s, ok=True, action="activated the app",
                              code=f'    driver.activate_app("{self.bid}")\n    time.sleep(3)')

        # WAIT
        if _WAIT.match(s):
            secs = int((re.search(r"(\d+)", s) or [0, "2"])[1] if re.search(r"\d+", s) else 2)
            time.sleep(secs)
            return StepResult(step=s, ok=True, action=f"waited {secs}s",
                              code=f"    time.sleep({secs})")

        # SWIPE / SCROLL — checked before TAP so "scroll down" is never a tap.
        if _SWIPE.match(s):
            return self._swipe_step(s)

        # BACK
        if _BACK.match(s):
            back = self.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "screenBackBtn")
            if back:
                back[0].click(); self._wait_settle()
                self._used_ids.add("screenBackBtn")
                return StepResult(step=s, ok=True, action="went back",
                                  code='    by_id(driver, "screenBackBtn").click()')
            return StepResult(step=s, ok=False, action="could not go back",
                              detail="No back control (screenBackBtn) on this screen.")

        # ASSERT
        if _ASSERT.match(s):
            phrase = _target_phrase(s, _ASSERT)
            low = phrase.lower()

            # NEGATIVE assertion: "verify X is NOT visible / no longer present".
            # Passes when the element is ABSENT (e.g. "Points" gone after rename).
            if _is_negative_assert(s):
                words = _locator_words(phrase)
                m = self._seen(phrase, words, s)
                gone = not m
                return StepResult(
                    step=s, ok=gone,
                    action=f'"{phrase}" is correctly absent' if gone
                           else f'"{phrase}" is STILL present (should be gone)',
                    detail="" if gone else f'Found "{self._describe(m)}" — expected it to be absent.',
                    code=f'    assert not driver.find_elements(AppiumBy.IOS_PREDICATE, '
                         f'\'label CONTAINS[c] "{phrase}"\'), "{phrase} should be absent"')

            # app-state assertion ("the app is running / did not crash")
            if (("app" in low and any(k in low for k in ("run", "crash", "foreground", "alive", "load", "open")))
                    or any(k in low for k in ("no crash", "not crash", "no redbox", "no error"))):
                state = self.d.query_app_state(self.bid)
                ok = state == 4
                return StepResult(
                    step=s, ok=ok,
                    action="app is in the foreground" if ok else "app is NOT in the foreground",
                    detail="" if ok else f"query_app_state returned {state} (4 == foreground).",
                    code='    assert driver.query_app_state(BUNDLE_ID) == 4, "app not in foreground"')

            words = _locator_words(phrase)
            m = self._seen(phrase, words, s)
            # chip/slot assertion — chips carry times, not the words "time/slot".
            if not m and any(w.lower() in ("slot", "time", "option", "chip") for w in words):
                chips = self.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "chip-container-outer-layer")
                if chips:
                    idx = _ordinal_index(s)
                    chip = chips[idx] if -len(chips) <= idx < len(chips) else chips[0]
                    m = Match(chip, "chip", "chip-container-outer-layer", exact=True)

            if not m:
                why = getattr(self, "_last_ambiguous", "")
                self._last_ambiguous = ""
                return StepResult(step=s, ok=False, action=f'"{phrase}" NOT found',
                                  detail=(why or f"No element matches '{phrase}'."))

            vague = self._vague(m, words)
            if vague:
                nouns, matched = vague
                return StepResult(
                    step=s, ok=False,
                    action=f'could not confirm "{phrase}"',
                    detail=f"Closest element was \"{self._describe(m)}\", which only matches "
                           f"{matched or 'none'} of {nouns} — too weak to call this verified.")

            if m.by in ("accessibility_id", "chip"):
                self._used_ids.add(m.value)
                code = f'    assert exists(driver, "{m.value}"), "{phrase} not found"'
            else:
                code = f'    assert driver.find_elements(AppiumBy.IOS_PREDICATE, {m.value!r}), "{phrase} not found"'
            return StepResult(step=s, ok=True,
                              action=f'found "{self._describe(m)}"',
                              code=code)

        # TYPE
        if _TYPE.match(s):
            phrase = _target_phrase(s, _TYPE)
            # "type <text> in <field>" — split on ' in '
            m = re.split(r"\s+in\s+", phrase, maxsplit=1)
            text = m[0].strip('"\' ')
            target = m[1] if len(m) > 1 else "search"
            m = self._resolve(_locator_words(target), step=s)
            if not m:
                return StepResult(step=s, ok=False, action=f'no field for "{target}"',
                                  detail=f"No input matches '{target}'.")
            m.el.send_keys(text)
            code = (f'    by_id(driver, "{m.value}").send_keys({text!r})'
                    if m.by == "accessibility_id"
                    else f'    driver.find_element(AppiumBy.IOS_PREDICATE, {m.value!r}).send_keys({text!r})')
            if m.by == "accessibility_id":
                self._used_ids.add(m.value)
            return StepResult(step=s, ok=True,
                              action=f'typed "{text}" into "{self._describe(m)}"', code=code)

        # TAP / open / select / book / …
        if _TAP.match(s):
            phrase = _target_phrase(s, _TAP)
            # The whole step, minus the interaction verb: "book" still reaches
            # bookAppoitment, but "click" is no longer a thing to search for.
            words = _locator_words(s)
            return self._tap_step(s, phrase, words)

        # An observation ("it gives a popup…") asks nothing to be tapped. Verify
        # what it claims instead of guessing at a target.
        if _OBSERVATION.match(s):
            return self._verify_step(s)

        # Unrecognised — best effort tap on the whole phrase ("nylai kitchen 2").
        return self._tap_step(s, s, _locator_words(s), inferred=True)

    def _fuzzy_resolve(self, words: List[str], step: str) -> Optional["Match"]:
        """Self-healing fallback: find the closest visible control by text when the
        exact locator no longer resolves (a testID/label changed on a new build).

        Fast path: score against the CACHED page_source (one call, in-memory match),
        then fetch only the single winning element's handle — not attribute reads
        across the whole screen. Only returns a match it is reasonably sure of, so
        healing recovers from small UI drift without turning into blind clicking."""
        import difflib
        import xml.etree.ElementTree as ET
        nouns = [n.lower() for n in _nouns(words)]
        target = " ".join(w.lower() for w in words).strip()
        noun_str = " ".join(nouns).strip()
        if not target:
            return None
        try:
            root = ET.fromstring(self._page_source())
        except Exception:
            return None

        best = None
        best_score = 0.0
        for el in root.iter():
            a = el.attrib
            if a.get("visible", "true") == "false":
                continue
            name, label, value = a.get("name") or "", a.get("label") or "", a.get("value") or ""
            txt = (label or name or value).strip().lower()
            if not txt or len(txt) > 80:
                continue
            score = max(
                difflib.SequenceMatcher(None, target, txt).ratio(),
                difflib.SequenceMatcher(None, noun_str, txt).ratio() if noun_str else 0.0,
            )
            if nouns and all(n in txt for n in nouns):
                score = max(score, 0.9)
            if score > best_score:
                best_score, best = score, (name, label, a.get("type", ""))

        if not best or best_score < 0.62:
            return None

        # Fetch the winning element's handle with a single query.
        name, label, _typ = best
        el = None
        try:
            if name:
                els = self.d.find_elements(AppiumBy.ACCESSIBILITY_ID, name)
                el = els[0] if els else None
            if el is None and label:
                safe = label.replace('"', '')
                els = self.d.find_elements(AppiumBy.IOS_PREDICATE, f'label == "{safe}"')
                el = els[0] if els else None
        except Exception:
            el = None
        if el is None:
            return None
        return Match(el=el, by="fuzzy", value=(name or label), text=(label or name),
                     method="fuzzy-heal", confidence=best_score)

    def _exact_id(self, phrase: str) -> Optional["Match"]:
        """Resolve the target phrase as an EXACT accessibility id. Recorded steps
        carry the element's real testID, which must match exactly rather than be
        re-interpreted by word matching."""
        pid = (phrase or "").strip()
        if not pid:
            return None
        try:
            els = self.d.find_elements(AppiumBy.ACCESSIBILITY_ID, pid)
        except Exception:
            return None
        if els:
            return Match(el=els[0], by="accessibility_id", value=pid, text=pid,
                         exact=True, method="exact-id", confidence=1.0)
        return None


    # ── idb fallback ────────────────────────────────────────────────────────
    # Appium's XCUITest snapshot is depth-capped and does not report this app's
    # GenericElement nodes at all — BOOK NOW (`bookAppoitment`), `orderLater`,
    # `preOrderBooking`, `pickUpOrderConfirm` and the menu category chips are all
    # invisible to it. Every strategy above then fails and the step burns its full
    # timeout. idb reads the real tree, so use it as a last resort before giving up.
    def _device_udid(self) -> str:
        """The simulator this session drives. Appium normalises capability keys
        differently across versions, so check every spelling rather than assume
        one — getting this wrong makes the idb fallback silently do nothing."""
        cached = getattr(self, "_udid", "")
        if cached:
            return cached
        udid = ""
        try:
            caps = self.d.capabilities or {}
            for k in ("udid", "appium:udid", "deviceUDID", "appium:deviceUDID"):
                if caps.get(k):
                    udid = str(caps[k]); break
        except Exception:
            udid = ""
        self._udid = udid
        return udid

    def _idb_on_screen(self, pt) -> bool:
        """Is this point actually visible? An idb tap is a raw coordinate tap, so
        unlike an Appium element click it will NOT scroll the target into view —
        tapping an off-screen frame hits whatever is at those pixels instead."""
        try:
            size = self.d.get_window_size()
            return 0 <= pt[0] <= size["width"] and 0 <= pt[1] <= size["height"]
        except Exception:
            return False

    def _idb_all(self) -> List[dict]:
        """Every element idb can see, as raw dicts. ~1.6-3.4s, versus ~17s for
        driver.page_source — and idb reports the GenericElement nodes Appium's
        depth-capped snapshot drops entirely."""
        udid = self._device_udid()
        if not udid:
            return []
        try:
            import subprocess
            from automation.scenarios.idb_path import idb_binary
            raw = subprocess.run([idb_binary(), "ui", "describe-all", "--udid", udid],
                                 capture_output=True, text=True, timeout=45).stdout
            return json.loads(raw) if raw.strip().startswith("[") else []
        except Exception:
            return []

    @staticmethod
    def _centre(e: dict):
        f = e.get("frame") or {}
        return (int(f.get("x", 0) + f.get("width", 0) / 2),
                int(f.get("y", 0) + f.get("height", 0) / 2))

    def _idb_element(self, name: str, arr: Optional[List[dict]] = None):
        """Frame centre (cx, cy) of the element whose accessibility label is *name*.

        Falls back to a label that CONTAINS *name* as a distinct token: the
        restaurant cards are labelled "card-container-outer-layer NylaiKitchen2 …",
        so an exact-only match sent every `click NylaiKitchen2` down the fuzzy
        resolver at ~63.8s a step. A \\b-delimited token is still strict —
        "NylaiKitchen2" does not match "NylaiKitchen2Sub" or "NylaiKitchen2Fav",
        which are separate targets in these scenarios — and an ambiguous phrase
        (two or more elements) is rejected rather than guessed."""
        labels = [((e.get("AXLabel") or "").strip(), e)
                  for e in (self._idb_all() if arr is None else arr)]
        for label, e in labels:
            if label == name:
                return self._centre(e)
        try:
            tok = re.compile(r"\b" + re.escape(name) + r"\b")
        except re.error:
            return None
        hits = [e for label, e in labels if label and tok.search(label)]
        return self._centre(hits[0]) if len(hits) == 1 else None

    # A COLLAPSED LogBox toast is a single GenericElement carrying the warning
    # text — it exposes no "Dismiss" button at all, so dismiss_logbox(), which
    # looks for one, never sees it. Measured on this build: the toast is a
    # full-width strip at (10, 801.7, 382, 48) — directly over BOOK NOW at
    # (201, 804) and over the Wallet tab. An idb tap is a RAW COORDINATE tap, so
    # it lands on the toast, LogBox EXPANDS, and the step cheerfully reports a tap
    # the app never received. That is the "tap 1 did not open the dialog" retry:
    # attempt 2 works only because the now-expanded LogBox does have "Dismiss".
    _LOGBOX = re.compile(r"Console (Warning|Error)|LogBox|addLog|Unhandled Promise Rejection"
                         r"|Require cycle|Warning:|VirtualizedList", re.I)

    def _logbox_toast(self, arr: List[dict]) -> Optional[dict]:
        """The collapsed LogBox toast in *arr*, if one is up."""
        for e in arr:
            if e.get("type") == "GenericElement" and self._LOGBOX.search(e.get("AXLabel") or ""):
                return e
        return None

    def _dismiss_toast(self, e: dict) -> bool:
        """Tap the toast's ✕, which sits at the right end of its own frame.
        Anywhere else on the toast EXPANDS LogBox over the whole screen, which is
        strictly worse than the toast — so aim from the measured frame, never at
        the toast's centre."""
        f = e.get("frame") or {}
        if not f.get("width"):
            return False
        pt = (int(f["x"] + f["width"] - 20), int(f["y"] + f.get("height", 0) / 2))
        try:
            import subprocess
            from automation.scenarios.idb_path import idb_binary
            subprocess.run([idb_binary(), "ui", "tap", "--udid", self._device_udid(),
                            str(pt[0]), str(pt[1])], timeout=15)
        except Exception:
            return False
        time.sleep(1.0)
        self._invalidate_source()
        return True

    def _idb_tap_name(self, name: str) -> bool:
        arr = self._idb_all()
        # Clear the toast BEFORE locating, not just before tapping: this debug
        # build emits warnings continuously, so one can land between a step's
        # dismiss_logbox() and this tap.
        toast = self._logbox_toast(arr)
        if toast and self._dismiss_toast(toast):
            arr = self._idb_all()
        pt = self._idb_element(name, arr)
        if not pt:
            return False
        try:
            import subprocess
            from automation.scenarios.idb_path import idb_binary
            udid = self._device_udid()
            subprocess.run([idb_binary(), "ui", "tap", "--udid", udid,
                            str(pt[0]), str(pt[1])], timeout=15)
            self._invalidate_source()   # the screen just changed under the cache
            return True
        except Exception:
            return False

    def _tap_step(self, s: str, phrase: str, words: List[str], inferred: bool = False) -> StepResult:
        # Exact accessibility-id first (recorded testIDs), then word/text resolution.
        m = self._exact_id(phrase)

        # Before the fuzzy resolver: an EXACT accessibility-label match via idb.
        # Measured on this app _resolve("Home") takes 68.9s and returns a 0.7
        # confidence PARTIAL match, while idb finds the same control in 1.6s by
        # exact label — and the wait-for-element loop below re-runs the resolver
        # four more times, which is where 276s of a 336s scenario went.
        # This is not a guess: an exact label match is strictly more precise than
        # the partial match it saves us from computing.
        if not m and phrase and not inferred:
            pt = self._idb_element(phrase)
            if pt and self._idb_on_screen(pt) and self._idb_tap_name(phrase):
                self._wait_settle()
                return StepResult(step=s, ok=True,
                                  action=f'tapped "{phrase}" (idb exact-id)')

        if not m:
            m = self._resolve(words, prefer_container=True, step=s)

        # The element may not be on screen YET — a list/feed that loads from an API
        # renders a moment after the screen appears. Retry a few times before
        # giving up, so a slow render is not mistaken for a missing element.
        if not m:
            for _ in range(4):
                time.sleep(0.8)
                self.wait_for_idle(2.0)
                # This loop exists to catch an element that renders LATE, so it
                # must look at the real screen each pass. The cache is keyed on
                # invalidation now, and nothing acted on the screen here — so
                # drop it explicitly, or all four passes re-read one stale tree
                # and a slow render is reported as a missing element.
                self._invalidate_source()
                # Ask the cheap oracle first: if the element has rendered, idb
                # sees it in ~1.6s. Only pay for the fuzzy resolver when it has
                # genuinely not appeared.
                if phrase and not inferred:
                    pt = self._idb_element(phrase)
                    if pt and self._idb_on_screen(pt) and self._idb_tap_name(phrase):
                        self._wait_settle()
                        return StepResult(step=s, ok=True,
                                          action=f'tapped "{phrase}" (idb exact-id, after wait)')
                m = self._exact_id(phrase) or self._resolve(words, prefer_container=True, step=s)
                if m:
                    break

        # "select a time slot" language -> the slot chip named by any ordinal
        # ("2nd time slot" -> chips[1]); defaults to the first chip.
        if not m and any(w.lower() in ("slot", "time", "option", "chip") for w in words):
            # Consumer app: chips share one id. Business app: each chip is a
            # distinct time-labeled id like "18:00Btn" — collect those in order.
            chips = self.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "chip-container-outer-layer")
            cid = "chip-container-outer-layer"
            if not chips:
                try:
                    timed = self.d.find_elements(
                        AppiumBy.IOS_PREDICATE,
                        'name MATCHES "^[0-9]{1,2}:[0-9]{2}Btn$" OR '
                        'label MATCHES "^[0-9]{1,2}:[0-9]{2}Btn$"')
                    # Order by the time in the id so "1st slot" = earliest (≈ now).
                    def _t(e):
                        v = (e.get_attribute("name") or e.get_attribute("label") or "")
                        mt = re.match(r"(\d{1,2}):(\d{2})", v)
                        return (int(mt.group(1)), int(mt.group(2))) if mt else (99, 99)
                    chips = sorted(timed, key=_t)
                    cid = "time-slot"
                except Exception:
                    chips = []
            if chips:
                idx = _ordinal_index(s)
                chip = chips[idx] if -len(chips) <= idx < len(chips) else chips[0]
                m = Match(chip, "container", (cid, None), exact=True)

        # SELF-HEAL: the exact locator did not resolve (e.g. a testID changed on a
        # daily staging build). Before failing, find the closest visible control by
        # text and use it — but flag the step as healed so the report shows the app
        # changed and the scenario recovered rather than silently drifting.
        healed_note = ""
        if not m:
            m = self._fuzzy_resolve(words, s)
            if m:
                healed_note = f'exact match not found — healed to "{self._describe(m)}" (fuzzy {int(m.confidence*100)}%)'

        # Last resort before failing: tap it through idb. See _idb_element above —
        # this app's GenericElement controls are invisible to Appium entirely, so
        # without this every booking scenario dies on BOOK NOW / order later.
        if not m and phrase and not inferred:
            if self._idb_tap_name(phrase):
                self._wait_settle()
                return StepResult(step=s, ok=True,
                                  action=f'tapped "{phrase}" (idb — Appium could not see it)')

        if not m:
            return StepResult(
                step=s, ok=False,
                action="did not understand this step" if inferred else f'could not find "{phrase}"',
                detail=(f"Could not map '{s}' to an action or element." if inferred else
                        (getattr(self, "_last_ambiguous", "") or
                         f"No element matches '{phrase}' — it may need a testID "
                         f"(accessible={{true}}).")))

        # Never let a GUESS destroy the session. Tapping a wrong button is
        # recoverable; logging out or deleting the account ends the run and
        # everything after it is meaningless.
        destructive = self._destructive_guess(m, s)
        if destructive:
            return StepResult(
                step=s, ok=False,
                action=f'refused to tap "{self._describe(m)}" — did not tap',
                detail=f"That is a destructive control ({destructive}) and this step "
                       f"never asked for it. Name it exactly if you mean it.")

        # Tap the best relevant element rather than holding out for a perfect
        # name: "menu section" should still open "Menu". A partial match is only
        # dangerous when it is SILENT, so the report always names the element
        # actually tapped, and says so when it answered to only part of the step.
        # (Asserting stays strict — claiming something is true is not best-effort.)
        sig_before = self._screen_signature()
        code = self._tap_resolved(m)
        self._wait_settle()

        # VERIFY the tap did something. Appium reports a click as successful once
        # it has dispatched it, which says nothing about whether the app acted:
        # BOOK NOW is found, clicked, logged [ok] — and the reservation form stays
        # put, so the NEXT step hunts for a dialog that never opened and burns its
        # whole timeout. When the screen is unchanged, re-tap the same control at
        # its real coordinates through idb, which does land.
        if self._screen_signature() == sig_before and phrase:
            if self._idb_tap_name(phrase):
                self._wait_settle()
                if self._screen_signature() != sig_before:
                    return StepResult(
                        step=s, ok=True,
                        action=f'tapped "{phrase}" (Appium click had no effect; idb re-tap worked)',
                        code=code, healed=bool(healed_note), healed_note=healed_note)

        vague = self._vague(m, words)
        if vague:
            nouns, matched = vague
            return StepResult(
                step=s, ok=True,
                action=f'tapped "{self._describe(m)}" — matched only {matched} of {nouns}',
                detail=f"Give the intended target a testID (accessible={{true}}) if this is "
                       f"not the element you meant.",
                code=code, healed=bool(healed_note), healed_note=healed_note)

        return StepResult(step=s, ok=True,
                          action=(f'healed → tapped "{self._describe(m)}"' if healed_note
                                  else f'tapped "{self._describe(m)}"'),
                          code=code, healed=bool(healed_note), healed_note=healed_note)

    def _swipe_step(self, s: str) -> StepResult:
        """Swipe the screen, or one element: 'swipe left on splitCard' / 'scroll down'.

        *direction* is the way the finger travels, which is how XCUITest's
        `mobile: swipe` reads it — 'swipe left' reveals what is off to the right.
        """
        d = _DIRECTION.search(s)
        if not d:
            return StepResult(
                step=s, ok=False, action="no direction in this step",
                detail="Say which way: 'swipe left', 'scroll down'.")
        direction = d.group(1).lower()

        # "swipe left on <target>" — anything after 'on' names the element to
        # swipe. Without it the gesture goes to the whole screen.
        args: dict = {"direction": direction}
        on = re.search(r"\bon\s+(.+)$", s, re.I)
        target = on.group(1).strip() if on else ""

        if target:
            m = self._resolve(_locator_words(target), prefer_container=True, step=s)
            if not m:
                return StepResult(
                    step=s, ok=False, action=f'could not find "{target}"',
                    detail=f"No element matches '{target}' to swipe on.")
            args["element"] = m.el.id
            where = f' on "{self._describe(m)}"'
            code = (f'    _el = driver.find_element(AppiumBy.ACCESSIBILITY_ID, "{m.value}")\n'
                    f'    driver.execute_script("mobile: swipe", '
                    f'{{"direction": "{direction}", "element": _el.id}})'
                    if m.by == "accessibility_id" else
                    f'    driver.execute_script("mobile: swipe", {{"direction": "{direction}"}})')
        else:
            where = ""
            code = f'    driver.execute_script("mobile: swipe", {{"direction": "{direction}"}})'

        try:
            self.d.execute_script("mobile: swipe", args)
        except Exception as e:
            return StepResult(step=s, ok=False, action=f"swipe {direction} failed",
                              detail=str(e)[:200])

        self._wait_settle()
        return StepResult(step=s, ok=True, action=f"swiped {direction}{where}", code=code)

    def _verify_step(self, s: str) -> StepResult:
        """Check that an observed thing is on screen. Never taps."""
        words = _locator_words(s)
        m = self._resolve(words, step=s)
        if not m:
            return StepResult(step=s, ok=False, action="could not verify this",
                              detail=f"Nothing on screen matches '{s}'.")
        vague = self._vague(m, words)
        if vague:
            nouns, matched = vague
            return StepResult(
                step=s, ok=False, action="could not verify this",
                detail=f"Closest element was \"{self._describe(m)}\", which only matches "
                       f"{matched or 'none'} of {nouns} — too weak to call this verified.")
        code = (f'    assert exists(driver, "{m.value}"), "{s} not found"'
                if m.by == "accessibility_id"
                else f'    assert driver.find_elements(AppiumBy.IOS_PREDICATE, {m.value!r}), "{s} not found"')
        if m.by == "accessibility_id":
            self._used_ids.add(m.value)
        return StepResult(step=s, ok=True, action=f'saw "{self._describe(m)}"', code=code)

    def _wait_settle(self, secs: float = 0.8):
        # Brief pause after acting. XCUITest no longer blocks on app-idle per
        # command (waitForQuiescence off), and each step begins with wait_for_idle,
        # so a long fixed settle here is wasted time. Was 2.0s.
        time.sleep(secs)

    # ── public ────────────────────────────────────────────────────────────────

    # ── crash + popup awareness ──────────────────────────────────────────────

    def dismiss_logbox(self) -> bool:
        """Close a React-Native LogBox overlay if one is showing.

        A yellow "Console Warning" (e.g. moment.js's non-ISO date deprecation,
        which the app trips when a time/date is picked) is NOT a crash, but its
        overlay covers the screen and swallows the next tap. Dismissing it lets
        the run continue. Only touches the LogBox — never real app UI.
        """
        # Look for the DISMISS CONTROL directly — do not gate on the overlay's text.
        # The old gate required a label containing "Console Warning"/"Console Error", but a
        # collapsed LogBox toast shows its MESSAGE instead ("Rendering <Context> directly is
        # not supported", "no valid aps-environment entitlement", "Each child in a list should
        # have a unique key"). So the gate returned False, the dismiss below never ran, and the
        # toast sat over the bottom of the screen eating taps: a run tapped BOOK NOW, logged
        # '[ok] tapped bookAppoitment by id', and the booking was never created — every later
        # segment then failed looking for a reservation that does not exist.
        # Same cost as the old gate (one predicate query), strictly more effective.
        for label in ("Dismiss", "Minimize"):
            try:
                btns = self.d.find_elements(
                    AppiumBy.IOS_PREDICATE,
                    f'type == "XCUIElementTypeButton" AND name == "{label}"')
                if not btns:
                    btns = self.d.find_elements(AppiumBy.ACCESSIBILITY_ID, label)
            except Exception:
                return False
            if btns:
                try:
                    btns[0].click()
                except Exception:
                    return False
                time.sleep(1)
                return True
        # Nothing named Dismiss/Minimize — but a COLLAPSED toast has no such
        # control at all (see _logbox_toast). It still covers the bottom strip and
        # still eats taps, so clear it here rather than discovering it as a
        # mysteriously ignored tap several steps later.
        toast = self._logbox_toast(self._idb_all())
        return bool(toast and self._dismiss_toast(toast))

    # ── Calculation & API assertions (Phase 3) ───────────────────────────────
    def _safe_eval(self, expr: str) -> Optional[float]:
        """Evaluate a pure arithmetic expression (numbers + - * / ( ) . only)."""
        expr = expr.strip()
        if not expr or not re.fullmatch(r"[\d\s.+\-*/()]+", expr):
            return None
        try:
            return float(eval(expr, {"__builtins__": {}}, {}))  # sandboxed: digits/ops only
        except Exception:
            return None

    def _screen_numbers(self):
        """All numbers currently visible on screen, with the element text they're in."""
        out = []
        try:
            src = self._page_source()
        except Exception:
            src = ""
        for m in re.finditer(r'(?:name|label|value)="([^"]*?)(\d[\d.,]*)([^"]*)"', src):
            try:
                out.append((float(m.group(2).replace(",", "")), (m.group(1) + m.group(2) + m.group(3))[:60]))
            except ValueError:
                continue
        return out

    def _capture_number(self, s: str) -> "StepResult":
        """`capture <label> as <name>` — remember a number on screen under *name*.

        The XP scenarios were written as "capture XP balance as before" … "verify
        calculation before + 20 = after", but no capture intent existed: the line fell
        through to the tap resolver, matched nothing meaningful, and the calculation
        that depended on it could never have real values. Storing the number makes the
        pair work, and makes before/after arithmetic expressible at all.
        """
        m = re.match(r"^\s*capture\s+(.+?)\s+as\s+([A-Za-z_][\w]*)\s*$", s, re.I)
        if not m:
            return StepResult(step=s, ok=False, action="could not read this capture step",
                              detail="Use: capture <label> as <name>  "
                                     "(e.g. 'capture XP balance as before').")
        label, name = m.group(1).strip(), m.group(2)
        words = [w for w in re.findall(r"[A-Za-z]{3,}", label)]
        nums = self._screen_numbers()
        if not nums:
            return StepResult(step=s, ok=False, action=f'no number to capture as "{name}"',
                              detail="No numbers are visible on this screen.")
        # Prefer a number whose surrounding text mentions the label; else the first.
        chosen, ctx_used = None, ""
        for val, ctx in nums:
            if words and all(w.lower() in ctx.lower() for w in words):
                chosen, ctx_used = val, ctx
                break
        if chosen is None:
            return StepResult(
                step=s, ok=False, action=f'could not find a number for "{label}"',
                detail=f"Visible numbers: {[f'{v:g} ({c[:26]})' for v, c in nums[:6]]}")
        self._captured[name] = chosen
        return StepResult(step=s, ok=True,
                          action=f'captured {name} = {chosen:g} (from "{ctx_used[:34]}")',
                          code=f"    {name} = {chosen:g}  # captured from screen")

    def _verify_calculation(self, s: str) -> Optional["StepResult"]:
        """`verify calculation <lhs> = <rhs>`.

        - RHS is evaluated as arithmetic (e.g. '10 - 1' → 9, or a literal '9').
        - If LHS is a label, the number shown for that label is compared to RHS.
        - Otherwise the computed result is asserted to be visible on screen.
        """
        body = re.sub(r"^\s*(assert|verify|check|confirm|ensure|expect)\s+", "", s, flags=re.I)
        body = re.sub(r"^\s*calculation\s+", "", body, flags=re.I)
        if "=" not in body:
            return None
        lhs, rhs = body.split("=", 1)
        # Substitute captured names so "before + 20 = after" becomes real arithmetic.
        for nm, val in (getattr(self, "_captured", None) or {}).items():
            pat = re.compile(rf"\b{re.escape(nm)}\b")
            lhs, rhs = pat.sub(f"{val:g}", lhs), pat.sub(f"{val:g}", rhs)
        expected = self._safe_eval(rhs)
        if expected is None:
            # RHS wasn't pure math — maybe LHS is the math and RHS a label; try LHS.
            expected = self._safe_eval(lhs)
            lhs, rhs = rhs, lhs
        if expected is None:
            return None  # not a calculation we can evaluate — fall through to normal assert

        # Both sides numeric (captured values on each side): compare them, do not go
        # hunting for the value on screen — "before + 20 = after" is about the two
        # captures, not about what is currently displayed.
        left = self._safe_eval(lhs)
        if left is not None:
            ok = abs(left - expected) <= 0.01
            return StepResult(
                step=s, ok=ok,
                action=(f"calculation checks out ({left:g} = {expected:g})" if ok
                        else f"calculation FAILED: {left:g} != {expected:g}"),
                detail="" if ok else f"Expected {expected:g}, got {left:g}.",
                code=f"    assert {left:g} == {expected:g}")

        nums = self._screen_numbers()
        label_words = [w for w in re.findall(r"[A-Za-z]{3,}", lhs)]
        actual = None
        # Prefer a number in an element whose text mentions the label.
        for val, ctx in nums:
            if label_words and all(w.lower() in ctx.lower() for w in label_words) and abs(val - expected) <= 0.01:
                actual = val; break
        if actual is None:
            # Fall back: is the expected value visible anywhere?
            for val, _ctx in nums:
                if abs(val - expected) <= 0.01:
                    actual = val; break
        ok = actual is not None
        return StepResult(
            step=s, ok=ok,
            action=f"calculation checks out (= {expected:g})" if ok else "calculation mismatch",
            detail="" if ok else f"Expected {expected:g} on screen; not found among visible numbers.",
            code=f'    # verify calculation: expected {expected:g} visible')

    def _verify_size(self, s: str) -> "StepResult":
        """`verify size/width/height of <element> = <N>` — checks pixel dimensions.

        Handles UI-size tickets (e.g. 'close table button should be 60px') that
        have no on-screen number — it reads the element's rect from Appium.
        """
        m = re.search(r"verify\s+(size|width|height)\s+of\s+(.+?)\s*(?:=|is|to)\s*(\d+)", s, re.I)
        if not m:
            return StepResult(step=s, ok=False, action="bad size assertion",
                              detail='Use: verify width of <element> = <px>')
        dim, target, expected = m.group(1).lower(), m.group(2).strip(), int(m.group(3))
        el = self._exact_id(target)
        if not el:
            mm = self._resolve(_locator_words(target), step=s)
            el = mm.el if mm else None
        if el is None:
            return StepResult(step=s, ok=False, action=f'"{target}" not found',
                              detail=f"No element matches '{target}' to measure.")
        try:
            rect = el.rect  # {x, y, width, height}
            actual = rect["height"] if dim == "height" else rect["width"]
        except Exception as e:
            return StepResult(step=s, ok=False, action="could not read size", detail=str(e)[:120])
        ok = abs(actual - expected) <= 2   # 2px tolerance
        return StepResult(step=s, ok=ok,
                          action=f'{dim} = {actual}px' if ok else f'{dim} is {actual}px, expected {expected}px',
                          detail="" if ok else f"{target}: measured {dim}={actual}px, expected {expected}px.",
                          code=f'    assert abs(el.rect["{"height" if dim=="height" else "width"}"] - {expected}) <= 2')

    def _verify_bill(self, s: str) -> "StepResult":
        """Reuse the bot's bill_validator on the current bill screen (items→total,
        coupon/discount). This brings the bot's proven financial checks into the
        platform runner without running the whole bot."""
        try:
            from automation.intelligence import bill_validator as bv
        except Exception as e:
            return StepResult(step=s, ok=False, action="bill_validator unavailable", detail=str(e)[:120])
        xml = self._page_source()
        low = s.lower()
        # discount/coupon check: "verify discount <original> <coupon_value>"
        if "discount" in low or "coupon" in low:
            nums = [float(x) for x in re.findall(r"[\d.]+", s)]
            if len(nums) >= 2:
                res = bv.validate_coupon(xml, nums[0], nums[1])
            else:
                return StepResult(step=s, ok=False, action="need original + coupon value",
                                  detail='Use: verify discount <original_total> <coupon_value>')
        else:
            res = bv.validate_bill(xml)
        ok = bool(res.get("pass"))
        return StepResult(step=s, ok=ok,
                          action="bill checks out" if ok else "bill mismatch",
                          detail=(res.get("calculation") or "") if ok else res.get("reason", "bill validation failed"),
                          code='    # bill_validator (from the Vya bot)')

    def _verify_api(self, s: str) -> "StepResult":
        """`verify api <url> <jsonpath-ish field> == <value>` — read a backend value.

        Uses the project's API base when a relative path is given. This covers
        data that is NOT on screen (e.g. inventory stock after a cancel).
        """
        import os, json, httpx
        m = re.search(r"api\s+(\S+)\s+([\w.\[\]]+)\s*(==|=|!=|>|<)\s*(\S+)", s, re.I)
        if not m:
            return StepResult(step=s, ok=False, action="bad api assertion",
                              detail='Use: verify api <url> <field> == <value>')
        path, field, op, expected = m.group(1), m.group(2), m.group(3), m.group(4).strip('"\'')
        base = os.getenv("APP_API_BASE", "")
        url = path if path.startswith("http") else f"{base.rstrip('/')}/{path.lstrip('/')}"
        try:
            r = httpx.get(url, timeout=15)
            data = r.json()
            for key in re.split(r"[.\[\]]+", field):
                if key == "":
                    continue
                data = data[int(key)] if key.isdigit() else data[key]
            actual = str(data)
        except Exception as e:
            return StepResult(step=s, ok=False, action="api read failed", detail=str(e)[:160])
        try:
            an, en = float(actual), float(expected)
            ok = {"==": an == en, "=": an == en, "!=": an != en, ">": an > en, "<": an < en}[op]
        except ValueError:
            ok = (actual == expected) if op in ("==", "=") else (actual != expected)
        return StepResult(step=s, ok=ok,
                          action=f"api {field}={actual} {op} {expected}" if ok else "api value mismatch",
                          detail="" if ok else f"backend {field}={actual}, expected {op} {expected}",
                          code=f'    # verify api: {field} {op} {expected}')

    def app_crash(self) -> Optional[str]:
        """The JS error on screen, if the app has red-boxed. None when healthy.

        A crash must never be reported as a passing step: once the red box is up
        the app is gone, and every step after it is measuring the error screen.
        """
        # Cheap check: the RN red box surfaces identifiable static text. A targeted
        # find_elements beats snapshotting the whole tree on every step.
        try:
            hits = self.d.find_elements(
                AppiumBy.IOS_PREDICATE,
                'type == "XCUIElementTypeStaticText" AND (label CONTAINS "Render Error" '
                'OR label CONTAINS "RCTFatal" OR label CONTAINS "No bundle URL" '
                'OR label CONTAINS "RedBox")')
        except Exception:
            return None
        if hits:
            try:
                return (hits[0].get_attribute("label") or "app red-boxed (see screenshot)")[:160]
            except Exception:
                return "app red-boxed (see screenshot)"
        # Native crash: the app process is no longer running/foreground (state 1 =
        # not running). A JS redbox keeps the app foreground; a native crash kills it.
        try:
            if self.d.query_app_state(self.bid) == 1:
                return "app terminated (native crash — process no longer running)"
        except Exception:
            pass
        return None

    def book_popup_open(self) -> bool:
        """True when the 'You need to book a date' popup is intercepting."""
        try:
            # Just the accept-button id — no page_source. It's the reliable signal
            # and this runs after every step, so it must be cheap.
            return bool(self.d.find_elements(AppiumBy.ACCESSIBILITY_ID, _BOOK_POPUP_ACCEPT))
        except Exception:
            return False

    def handle_book_popup(self) -> Optional[StepResult]:
        """Take the booking offer when the popup blocks an add-to-cart.

        NEVER taps `orderLater`: that dismisses the popup AND silently discards
        the item the user was adding, leaving an empty cart and a run that looks
        like it worked.
        """
        if not self.book_popup_open():
            return None

        els = self.d.find_elements(AppiumBy.ACCESSIBILITY_ID, _BOOK_POPUP_ACCEPT)
        if not els:
            return None
        els[0].click()
        self._wait_settle(3.0)

        crash = self.app_crash()
        if crash:
            return StepResult(
                step=f"auto: book a date ({_BOOK_POPUP_ACCEPT})", ok=False,
                action="the app crashed while opening the booking flow",
                detail=f"APP BUG (not automation): {crash}",
                code="")
        return StepResult(
            step=f"auto: book a date ({_BOOK_POPUP_ACCEPT})", ok=True,
            action="took the booking offer from the popup",
            code=f'    by_id(driver, "{_BOOK_POPUP_ACCEPT}").click()')

    # Controls that dismiss an obstacle, most-preferred first. GRANTING a permission is
    # preferred over denying: denying also clears the dialog, but then the location-gated
    # home screen returns no restaurants and the run fails later for an invented reason.
    _BLOCKER_BUTTONS = (
        "Allow While Using App", "Allow Once", "Allow", "OK", "Continue",
        "Got it", "GOT IT", "YES, GOT IT", "Skip", "Next", "Get Started",
        "Maybe Later", "Not Now", "Done", "Close", "Dismiss",
    )
    # Never press these to "unblock" — they destroy state or make a real choice for the
    # user. An obstacle should be stepped over, not answered on their behalf.
    _BLOCKER_NEVER = ("Don't Allow", "Dont Allow", "Deny", "Delete", "Log out", "Logout",
                      "Sign out", "Cancel", "Remove", "Reset", "Skip Login")

    def clear_blockers(self, intent: str = "") -> str:
        """Dismiss something standing in the way. Returns what it pressed, or "".

        Deterministic first: a known dismiss control by exact label. Only if none is
        present does it ask the model, and only to answer "which control gets past
        this screen" — never "which control is the step's target". The model's answer
        is rejected unless it names an element that is actually on screen.
        """
        # 0. SYSTEM alerts first — "Would Like to Send You Notifications", location,
        #    camera, Bluetooth. These belong to SpringBoard, NOT the app, so they do not
        #    appear in page_source at all: element queries for "Allow" find nothing while
        #    the dialog is plainly on screen and blocking every tap. The alert API is the
        #    only way to reach them. accept() presses the affirmative button (Allow),
        #    which is what unblocks — denying also closes it but then location-gated
        #    screens come back empty and the run fails later for an invented reason.
        try:
            alert = self.d.switch_to.alert
            text = (alert.text or "").strip()
            alert.accept()
            time.sleep(1.2)
            self._invalidate_source()
            first = text.splitlines()[0] if text else "system alert"
            return f"allowed system alert: {first[:60]}"
        except Exception:
            pass                      # no alert present — the normal case

        # 1. Known dismiss controls, in preference order — but a tap only COUNTS if the
        #    screen actually changed. Tapping "Skip" on this app's onboarding did nothing
        #    (the label is not the tappable element), and without this check the unblocker
        #    reported "cleared: Skip" seven times in a row while the screen sat still —
        #    the same false-success it exists to prevent.
        before = self._screen_signature()
        for label in self._BLOCKER_BUTTONS:
            try:
                els = self.d.find_elements(AppiumBy.IOS_PREDICATE,
                                           f'label == "{label}" OR name == "{label}"')
            except Exception:
                continue
            for el in els:
                try:
                    if not el.is_displayed():
                        continue
                    el.click()
                    time.sleep(1.2)
                    self._invalidate_source()
                    if self._screen_signature() != before:
                        return label
                    # An element click did nothing. On RN screens the visible text is
                    # often a plain label with the TouchableOpacity as an ancestor, so
                    # clicking the label is a no-op (the same trap as the T&C checkbox,
                    # where the tappable square sat inside a wider row). A coordinate
                    # tap at its centre goes through the real hit-test.
                    if self._tap_center(el):
                        time.sleep(1.2)
                        self._invalidate_source()
                        if self._screen_signature() != before:
                            return f"{label} (tap)"
                    # Still nothing — that control is inert here. Try the next candidate.
                except Exception:
                    continue

        # 2. Nothing known matched — ask the model what clears this screen.
        return self._llm_unblock(intent)

    def _llm_unblock(self, intent: str = "") -> str:
        """Last resort: let the model name a control that gets past an unknown screen.

        Strictly bounded — it may only choose from ids ON SCREEN, never a destructive
        one, and the choice is reported so a green step never hides a guess.
        """
        if os.getenv("AI_UNBLOCK", "true").lower() in ("0", "false", "no"):
            return ""
        try:
            onscreen = self._visible_labels()
        except Exception:
            return ""
        if not onscreen:
            return ""
        try:
            from automation.ai.provider import create_provider, default_config
        except Exception as e:
            logger.debug("no AI provider for unblock: %s", e)
            return ""

        system = (
            "You unblock an automated mobile test. You are given the goal and the "
            "controls on screen. Reply with the exact text of ONE control, or NONE."
        )
        prompt = (
            "An automated test is stuck. It was trying to: "
            f"{intent or 'proceed with the app'}.\n"
            f"Controls on screen: {onscreen[:40]}\n\n"
            "If this screen is an OBSTACLE unrelated to that goal (a permission alert, "
            "an onboarding slide, a rating prompt, a cookie notice), reply with the ONE "
            "control that gets past it. Prefer granting permission over denying it. "
            "If this screen IS the goal, or nothing would clear it, reply NONE.\n"
            "Reply with the control text only."
        )
        try:
            resp = create_provider(default_config).generate(
                system, prompt, json_schema={}, max_tokens=24, temperature=0)
            answer = (resp.content or "").strip().strip('"').splitlines()[0].strip()
        except Exception as e:
            logger.debug("unblock model call failed: %s", e)
            return ""
        if not answer or answer.upper() == "NONE":
            return ""
        if any(bad.lower() in answer.lower() for bad in self._BLOCKER_NEVER):
            logger.info("Refusing model's unblock suggestion %r — destructive.", answer)
            return ""
        # It must name something actually on screen; otherwise it invented one.
        if not any(answer.lower() == o.lower() for o in onscreen):
            logger.info("Refusing model's unblock suggestion %r — not on screen.", answer)
            return ""
        # It must plausibly DISMISS something. Asked about a normal home screen the model
        # happily answered "Wallet" — a real control, not destructive, and tapping it
        # would navigate AWAY from the step's target and make the failure worse. So an
        # answer is only accepted when it reads like a dismissal, or when it is a button
        # inside a genuine system alert (where every button dismisses the alert).
        if not (self._is_dismissive(answer) or self._in_system_alert(answer)):
            logger.info("Refusing model's unblock suggestion %r — not a dismiss control.",
                        answer)
            return ""
        try:
            before = self._screen_signature()
            els = self.d.find_elements(AppiumBy.IOS_PREDICATE,
                                       f'label == "{answer}" OR name == "{answer}"')
            if els:
                els[0].click()
                time.sleep(1.2)
                self._invalidate_source()
                # Same rule as the deterministic path: a tap that changed nothing
                # cleared nothing, and must not be reported as a success.
                if self._screen_signature() != before:
                    return f"{answer} (AI)"
                logger.info("Model's unblock %r changed nothing — not counting it.", answer)
        except Exception:
            pass
        return ""

    # Words that mean "get past this", in the phrasings apps actually use.
    _DISMISSIVE = re.compile(
        r"\b(allow|ok|okay|continue|next|skip|got\s*it|understood|agree|accept|"
        r"proceed|start|get\s+started|later|not\s+now|maybe|done|close|dismiss|"
        r"confirm|yes)\b", re.I)

    def _is_dismissive(self, text: str) -> bool:
        return bool(self._DISMISSIVE.search(text or ""))

    def _in_system_alert(self, text: str) -> bool:
        """True when *text* is a button inside a real alert — there, any button dismisses."""
        try:
            alerts = self.d.find_elements(AppiumBy.IOS_PREDICATE,
                                          'type == "XCUIElementTypeAlert"')
            for a in alerts:
                for b in a.find_elements(AppiumBy.IOS_PREDICATE,
                                         'type == "XCUIElementTypeButton"'):
                    if (b.get_attribute("label") or b.get_attribute("name") or "").strip() == text:
                        return True
        except Exception:
            pass
        return False

    def _tap_center(self, el) -> bool:
        """Tap an element's centre by coordinate, bypassing the element hit-test."""
        try:
            from selenium.webdriver.common.actions.action_builder import ActionBuilder
            from selenium.webdriver.common.actions.pointer_input import PointerInput
            r = el.rect
            x = int(r["x"] + r["width"] / 2)
            y = int(r["y"] + r["height"] / 2)
            a = ActionBuilder(self.d, mouse=PointerInput("touch", "finger"))
            a.pointer_action.move_to_location(x, y).pointer_down().pause(0.1).pointer_up()
            a.perform()
            return True
        except Exception as e:
            logger.debug("coordinate tap failed: %s", e)
            return False

    def _screen_signature(self) -> str:
        """Cheap fingerprint of what is on screen, to tell whether a tap did anything.

        Measured on this app: driver.page_source is 241 KB and takes ~17.5s, while
        idb's describe-all takes ~3.4s and reports the tree Appium's depth-capped
        snapshot misses. Anything calling this twice per tap has to use idb, or a
        7-step scenario spends four minutes just fingerprinting screens.
        """
        udid = self._device_udid()
        if udid:
            try:
                import subprocess
                from automation.scenarios.idb_path import idb_binary
                raw = subprocess.run([idb_binary(), "ui", "describe-all", "--udid", udid],
                                     capture_output=True, text=True, timeout=30).stdout
                arr = json.loads(raw) if raw.strip().startswith("[") else []
                labels = [(e.get("AXLabel") or "").strip() for e in arr]
                return "|".join([l for l in labels if l][:25])
            except Exception:
                pass
        try:
            return "|".join(self._visible_labels(limit=25))
        except Exception:
            return ""

    def _visible_labels(self, limit: int = 40) -> List[str]:
        """Short, tappable-looking labels on screen — the model's only menu.

        idb first, for the same reason _screen_signature uses it: ~1.6-3.4s
        against ~17s for page_source, and it sees this app's GenericElements."""
        out: List[str] = []
        for e in self._idb_all():
            txt = (e.get("AXLabel") or "").strip()
            if txt and len(txt) <= 40 and txt not in out:
                out.append(txt)
            if len(out) >= limit:
                return out
        if out:
            return out
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(self._page_source())
        except Exception:
            return out
        for el in root.iter():
            a = el.attrib
            if a.get("visible", "true") == "false":
                continue
            txt = (a.get("label") or a.get("name") or "").strip()
            if txt and len(txt) <= 40 and txt not in out:
                out.append(txt)
            if len(out) >= limit:
                break
        return out

    def run_one(self, step: str, index: int) -> StepResult:
        """Run a single step and return its result. Never raises.

        Kept public and self-contained so a caller that reports progress live can
        drive the scenario one step at a time and still get behaviour identical
        to ``run()`` — the two must never drift apart.

        *index* only names the failure screenshot.
        """
        # A step cannot succeed against a half-loaded screen, and an element is
        # not "missing" merely because the spinner has not cleared yet.
        self.wait_for_idle()
        # A yellow LogBox warning (e.g. moment.js date deprecation on time/date
        # selection) overlays the screen and would eat this tap — clear it first.
        self.dismiss_logbox()

        try:
            res = self._do_step(step)
        except Exception as e:  # never let one step abort the whole scenario
            res = StepResult(step=step, ok=False, action="error", detail=str(e)[:200])

        # A step can fail simply because something UNASKED-FOR is in the way — an iOS
        # permission alert, an onboarding carousel, a "rate us" sheet. No rule covers
        # those, and none should: they are obstacles, not targets. Clear the obstacle
        # and retry the step ONCE, exactly as written.
        #
        # This is deliberately NOT the same thing as guessing what a step meant. The
        # step's own target is never substituted — 'click book now' still fails rather
        # than becoming preOrderBooking. Only things standing BETWEEN us and the screen
        # get touched.
        if not res.ok and not getattr(self, "_unblocking", False):
            self._unblocking = True                 # never recurse
            try:
                cleared = self.clear_blockers(intent=step)
            finally:
                self._unblocking = False
            if cleared:
                self._invalidate_source()
                try:
                    retried = self._do_step(step)
                except Exception as e:
                    retried = StepResult(step=step, ok=False, action="error",
                                         detail=str(e)[:200])
                if retried.ok:
                    retried.action = f"{retried.action} (after clearing: {cleared})"
                    res = retried

        # The screen was just acted on — the cached source is stale now.
        self._invalidate_source()

        # Did this step kill the app? Once the red box is up every later step is
        # measuring the error screen, so a crash is never a pass.
        crash = self.app_crash()
        if crash and res.ok:
            res = StepResult(
                step=step, ok=False,
                action=f"{res.action} — then the app CRASHED",
                detail=f"APP BUG (not automation): {crash}",
                code=res.code)

        if not res.ok and self.shot_dir:
            try:
                path = os.path.join(self.shot_dir, f"step_{index}.png")
                self.d.get_screenshot_as_file(path)
                res.screenshot = path
            except Exception:
                pass
        return res

    def run(self, steps: List[str]) -> ScenarioResult:
        out = ScenarioResult()
        for step in steps:
            out.results.append(self.run_one(step, len(out.results)))
        out.script = self.build_script(out)
        return out

    def build_script(self, result: ScenarioResult, test_name: str = "scenario") -> str:
        body = []
        for r in result.results:
            body.append(f"    # {r.step}")
            if r.code:
                body.append(r.code)
            elif not r.ok:
                body.append(f"    # UNRESOLVED: {r.detail}")
            body.append("")
        indented = "\n".join(body) or "    pass"
        return (
            '"""Generated from a plain-language scenario by the Scenario Runner.\n'
            "Every locator below was resolved against the live app, so it ran as written.\n"
            '"""\n\n'
            "import time\n"
            "from appium.webdriver.common.appiumby import AppiumBy\n"
            "from conftest import BUNDLE_ID, by_id, exists\n\n\n"
            f"def test_{test_name}(driver):\n{indented}\n"
        )
