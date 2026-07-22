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

import os
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from appium.webdriver.common.appiumby import AppiumBy

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
    """Everything after the leading verb is the target description."""
    m = verb_re.match(step)
    rest = step[m.end():] if m else step
    return rest.strip(" .:-\t")


class ScenarioRunner:
    def __init__(self, driver, bundle_id: str, screenshot_dir: Optional[str] = None,
                 catalog: Optional[AutoIdCatalog] = None):
        self.d = driver
        self.bid = bundle_id
        self.shot_dir = screenshot_dir
        self._used_ids: set[str] = set()   # for building the script
        # Names elements the app never named, and records how sure we were.
        self.catalog = catalog or AutoIdCatalog()
        self._screen = "unknown"
        # page_source is a slow XCUITest round trip (1-3s). Several per-step checks
        # need it back-to-back, so share one fetch via a short TTL cache and
        # invalidate it the moment the screen is acted on.
        self._src_cache: Tuple[Optional[str], float] = (None, 0.0)

    def _page_source(self, ttl: float = 1.5) -> str:
        """page_source with a short-lived cache, so the several checks that run
        back-to-back within one step don't each pay the XCUITest round trip."""
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
                spinners = self.d.find_elements(
                    AppiumBy.IOS_PREDICATE,
                    'type == "XCUIElementTypeActivityIndicator" AND visible == true')
            except Exception:
                return True
            if not spinners:
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

        # 2. a tappable container whose subtree contains a keyword (cards, chips)
        if prefer_container:
            for cid in self._CONTAINERS:
                containers = self.d.find_elements(AppiumBy.ACCESSIBILITY_ID, cid)
                for w in sorted(long_words, key=len, reverse=True):
                    for c in containers:
                        if c.find_elements(AppiumBy.IOS_PREDICATE,
                                           f'name CONTAINS[c] "{w}" OR label CONTAINS[c] "{w}"'):
                            return Match(c, "container", (cid, w), method=METHOD_CONTAINER)

        # 3. id CONTAINS a keyword (e.g. "book" -> bookAppoitment)
        for w in sorted(long_words, key=len, reverse=True):
            pred = f'name CONTAINS[c] "{w}" AND name.length < {self._AGGREGATE_CHARS}'
            el = self._most_specific(
                self.d.find_elements(AppiumBy.IOS_PREDICATE, pred), w, ("name",))
            if el is not None:
                return Match(el, "ios_predicate", pred, method=METHOD_PARTIAL_ID)

        # 4. visible label CONTAINS a keyword
        for w in sorted(long_words, key=len, reverse=True):
            pred = (f'(name CONTAINS[c] "{w}" AND name.length < {self._AGGREGATE_CHARS})'
                    f' OR (label CONTAINS[c] "{w}" AND label.length < {self._AGGREGATE_CHARS})')
            el = self._most_specific(
                self.d.find_elements(AppiumBy.IOS_PREDICATE, pred), w, ("name", "label"))
            if el is not None:
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
            m = self._resolve(words, step=s)
            # chip/slot assertion — chips carry times, not the words "time/slot".
            if not m and any(w.lower() in ("slot", "time", "option", "chip") for w in words):
                chips = self.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "chip-container-outer-layer")
                if chips:
                    idx = _ordinal_index(s)
                    chip = chips[idx] if -len(chips) <= idx < len(chips) else chips[0]
                    m = Match(chip, "chip", "chip-container-outer-layer", exact=True)

            if not m:
                return StepResult(step=s, ok=False, action=f'"{phrase}" NOT found',
                                  detail=f"No element matches '{phrase}'.")

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

    def _tap_step(self, s: str, phrase: str, words: List[str], inferred: bool = False) -> StepResult:
        m = self._resolve(words, prefer_container=True, step=s)

        # "select a time slot" language -> the slot chip named by any ordinal
        # ("2nd time slot" -> chips[1]); defaults to the first chip.
        if not m and any(w.lower() in ("slot", "time", "option", "chip") for w in words):
            chips = self.d.find_elements(AppiumBy.ACCESSIBILITY_ID, "chip-container-outer-layer")
            if chips:
                idx = _ordinal_index(s)
                chip = chips[idx] if -len(chips) <= idx < len(chips) else chips[0]
                m = Match(chip, "container", ("chip-container-outer-layer", None), exact=True)

        # SELF-HEAL: the exact locator did not resolve (e.g. a testID changed on a
        # daily staging build). Before failing, find the closest visible control by
        # text and use it — but flag the step as healed so the report shows the app
        # changed and the scenario recovered rather than silently drifting.
        healed_note = ""
        if not m:
            m = self._fuzzy_resolve(words, s)
            if m:
                healed_note = f'exact match not found — healed to "{self._describe(m)}" (fuzzy {int(m.confidence*100)}%)'

        if not m:
            return StepResult(
                step=s, ok=False,
                action="did not understand this step" if inferred else f'could not find "{phrase}"',
                detail=(f"Could not map '{s}' to an action or element." if inferred else
                        f"No element matches '{phrase}' — it may need a testID "
                        f"(accessible={{true}})."))

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
        code = self._tap_resolved(m)
        self._wait_settle()

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
        # Cheap presence check instead of a full snapshot (runs before every step).
        try:
            present = self.d.find_elements(
                AppiumBy.IOS_PREDICATE,
                'label CONTAINS "Console Warning" OR label CONTAINS "Console Error" '
                'OR name CONTAINS "Console Warning" OR name CONTAINS "Console Error"')
        except Exception:
            return False
        if not present:
            return False
        for label in ("Dismiss", "Minimize"):
            btns = self.d.find_elements(
                AppiumBy.IOS_PREDICATE,
                f'type == "XCUIElementTypeButton" AND name == "{label}"')
            if not btns:
                btns = self.d.find_elements(AppiumBy.ACCESSIBILITY_ID, label)
            if btns:
                btns[0].click()
                time.sleep(1)
                return True
        return False

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
