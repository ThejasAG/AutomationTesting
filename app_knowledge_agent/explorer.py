"""Explorer — autonomous iOS app exploration with proper DFS navigation.

Fixes applied in this version
──────────────────────────────
1. visited_hashes is NOT pre-populated on resume — every run does a fresh
   exploration. Raw files already on disk are kept (save is idempotent) so
   no disk space is wasted, but screens_found counts correctly.

2. Off-screen horizontal elements (category chips, product rows) are
   scrolled into view individually before giving up on them, not batch-
   scrolled at the end of the page.

3. Filter screen / empty-tree modals: if idb returns 0 or 1 elements after
   a tap, we try a close/dismiss gesture and re-read before declaring the
   page exhausted.

4. (removed — restaurant cards need no special-casing; see fix 11.)

5. (removed — see fix 11.)

6. Home screen exhaustion: only declare "session complete" when ALL tab-bar
   sections (Home restaurant, Home products, Wallet, Menu) have been visited
   AND the restaurant list has been fully scrolled.

7. global_tapped persists element keys across nav-hash changes so toggles
   like homeRestuarantBar / homeProductsBar are never re-tapped.

8. _ensure_foreground() checks that the Vya app is actually in the
   foreground before each session starts.

9. SAFETY: "book now" / bookAppoitment (the app's real, misspelled id — see
   AGENTS.md gotcha #8) is now denied. Without this the explorer would
   complete a real staging booking the first time it reached that screen.

10. A dead end (no back control, no close button) no longer ends the whole
    session. It relaunches the app, resets to the home page's already-tapped
    state (nothing is re-walked), and continues — up to
    _MAX_RELAUNCH_RECOVERIES times. The summary now reports
    terminated_cleanly and relaunch_recoveries so an incomplete run is
    never silently reported as if home were fully explored.

11. tap_el() is now retried with scroll=True before falling back to the
    horizontal-chip scroll. idb_driver.tap_el already scrolls an element
    into view VERTICALLY when allowed to (AGENTS.md gotcha #2) — step 1 was
    calling it with scroll=False and then only compensating for the
    horizontal case, so a restaurant card below the fold on Home was never
    reachable. This is why items 4 and 5 above are gone: once vertical
    reachability works, the normal DFS taps every element, including
    restaurant cards, without needing special-casing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── Path setup ─────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from automation.config import load_env
load_env()

from automation.scenarios.idb_driver import (
    describe_all, tap, tap_el, fill,
    swipe, swipe_screen, busy, crash_text, logbox_buttons,
    centre, frame, hittable,
)
from automation.scenarios.idb_coords import to_device
from automation.scenarios.cross_app_config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("aka.explorer")

# ── Paths ──────────────────────────────────────────────────────────────────────
_AKA_DIR = _PROJECT_ROOT / "app_knowledge_agent"
_RAW_DIR = _AKA_DIR / "knowledge_pack" / "raw"

# ── Tuning ─────────────────────────────────────────────────────────────────────
# Raised from 600: global_tapped dedup bounds real work to roughly the number
# of distinct interactive elements discovered, not an exponential walk, and a
# real Home page (restaurants + products + wallet + menu, each with its own
# deep sub-tree) can need more than 600 steps to fully exhaust. If this cap is
# hit before the stack naturally drains to depth<=1, terminated_cleanly is
# False in the summary rather than this being silently indistinguishable from
# a complete run.
_MAX_STEPS       = 2500
_MAX_RELAUNCH_RECOVERIES = 5   # dead-end recoveries before genuinely giving up
_MAX_EMPTY_MODAL_RETRIES = 4   # dismiss attempts before an unreadable modal
                               # counts as a dead end (~15-20s, not forever)

# ── Interchangeable-label families ──────────────────────────────────────────
# Shapes that CANNOT be a distinct, meaningful action by construction — a time
# like "11:50" is never a button whose identity matters, unlike "ThaiCategorie"
# or "Reserve a table". Measured: on a time-slot picker, tapping different
# slots left BOTH the nav hash and the content hash unchanged — idb's tree does
# not even reflect the selection — so tapping every slot adds no coverage,
# only time (~22s each once off-screen, since each failed attempt still runs
# the full scroll-into-view fallback before giving up).
_TIME_SLOT_RE    = re.compile(r"^\d{1,2}:\d{2}(\s*[AaPp][Mm])?$")
# A bare single character is, by construction, never a meaningful distinct
# action — it is a key on an on-screen keyboard (confirmed: a custom RN
# keyboard's q/w/e/.../1/2/3 keys each produced the identical "same logical
# page/no navigation" outcome, so tapping all of them added nothing, only
# ~6.5s each). Deliberately not limited to letters/digits: the SAME
# keyboard's numbers/symbols layer uses punctuation keys (-, /, :, ;, ...),
# confirmed to slip through an [A-Za-z0-9]-only version of this regex
# uncapped. Real, meaningful buttons in this app use descriptive camelCase
# ids (counterPlus, screenBackBtn) or multi-word labels, never a bare single
# character — so matching any one-character label, of any kind, is safe.
_SINGLE_CHAR_RE  = re.compile(r"^.$")
# A calendar day chip ("SAT 10", "SUN 11", ...). Confirmed on the waiter's
# table-booking calendar: each day tapped opened a "new" nav_hash (the
# calendar highlights a different day), so the DFS walked the entire
# calendar one day at a time -- SAT 10, SUN 11, MON 12, ... past a full year
# -- treating every date as a distinct page instead of 365 samples of the
# same "a day is selected" behaviour.
_CALENDAR_DAY_RE = re.compile(
    r"^(MON|TUE|WED|THU|FRI|SAT|SUN)\s+\d{1,2}$", re.IGNORECASE)
_MAX_FAMILY_TAPS = 2   # sample size per family per page — never 0, so the
                       # family's behaviour is still recorded at least once


def _family_of(label: str) -> Optional[str]:
    """Which known interchangeable-label family *label* belongs to, or None.

    Deliberately narrow and shape-based, not a name blocklist: it must never
    catch a genuinely distinct action. Add further families here only for
    shapes with the same property (a bare time/date/number/character that
    repeats across many otherwise-identical candidates), never by element id
    or by guessing a screen's purpose.
    """
    if _TIME_SLOT_RE.match(label):
        return "time_slot"
    if _SINGLE_CHAR_RE.match(label):
        return "keyboard_key"
    if _CALENDAR_DAY_RE.match(label):
        return "calendar_day"
    return None
_SETTLE_S        = 2.5
_BACK_SETTLE_S   = 1.5
_SCROLL_SETTLE_S = 1.2
_LAUNCH_WAIT_S   = 8.0
_LOGIN_WAIT_S    = 5.0
_MAX_V_SCROLLS   = 3
_MAX_H_SCROLLS   = 3

# ── Bundle IDs ─────────────────────────────────────────────────────────────────
_CONSUMER_BUNDLE = os.getenv("VYA_CONSUMER_BUNDLE", "org.vyapy.sarls.vyaconsumerstaging")
_BUSINESS_BUNDLE = os.getenv("VYA_BUSINESS_BUNDLE", "org.vyapy.sarls.vyabusinessipadstaging")

# ── Deny lists ─────────────────────────────────────────────────────────────────
# Payment, order-placement, send-to-kitchen and booking/event creation are
# DELIBERATELY allowed — full automated execution against the real staging
# backend was an explicit decision (not an oversight): the explorer must be
# able to place a real preorder, pay for it, send it to the kitchen, and
# create a real booking/event to reach those screens at all, in order to
# fully explore the waiter/kitchen order-management flow end to end.
# Destructive actions that destroy EXISTING data (not create new data) stay
# blocked by default — deletion and cancellation were never part of this
# instruction and remain a separate, more dangerous category.
_DENY_FRAGMENTS = {
    "delete", "remove",
    "cancel booking", "cancel order", "cancel event", "cancel reservation",
}
_DENY_IDS = {
    "deleteBtn", "removeBtn", "cancelBookingBtn",
}

# ── Profile edit protection ────────────────────────────────────────────────────
_PROFILE_EDIT_MARKERS = {
    "editProfile", "saveProfile", "updateProfile",
    "Edit Profile", "Save Profile", "Update Profile",
    "saveBtn", "updateBtn", "changePassword",
    "editEmail", "editName", "editPhone", "profileForm",
}

# ── Login ──────────────────────────────────────────────────────────────────────
_EMAIL_IDS    = {"emailValue","email","username","loginEmail","emailInput"}
_PASSWORD_IDS = {"passwordValue","password","loginPassword","passwordInput"}
_SUBMIT_IDS   = {"signInBtn","loginBtn","submitBtn","signIn","login",
                 "signInButton","loginButton"}

# ── iOS home screen indicators (SpringBoard) ───────────────────────────────────
_SPRINGBOARD_LABELS = {
    "safari","messages","settings","fitness","calendar","photos","maps",
    "health","reminders","news","files","contacts","wallet","preview",
    "webdriveragentrunner-runner","utilities folder","watch",
}

# ── Card side-effect buttons to skip ──────────────────────────────────────────
_CARD_SKIP_SUFFIXES = ("Fav", "Unsub", "Sub")

# ── Close/dismiss label patterns ──────────────────────────────────────────────
_CLOSE_LABELS = {
    "Close","close","Dismiss","dismiss","Done","done",
    "Cancel","cancel","Back","back","productClose",
    "filterClose","modalClose","closeBtn","dismissBtn",
    "screenBackBtn",   # this app's REAL back control (confirmed: tapped
                       # successfully as a normal candidate throughout every
                       # run) — a GenericElement, not type=="Button".
}

# ── Module-level transition dedup ──────────────────────────────────────────────
_logged_transitions: set[str] = set()


# ══════════════════════════════════════════════════════════════════════════════
# HASH / IDENTITY
# ══════════════════════════════════════════════════════════════════════════════

def _field_id(el: dict) -> str:
    return (el.get("AXIdentifier") or el.get("AXLabel") or "").strip()


def _nav_hash(elements: list[dict]) -> str:
    """Navigation hash — changes only on genuine page transitions."""
    parts = []
    for el in elements:
        kind = el.get("type","")
        key  = _field_id(el)
        if key or kind in {"Button","GenericElement","Cell","TabBarButton",
                           "Link","Switch","Toggle","Image","TextField",
                           "SecureTextField","NavigationBar"}:
            parts.append(f"{kind}:{key}")
    parts.sort()
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:32]


def _content_hash(elements: list[dict]) -> str:
    """Content hash — also changes on tab/filter toggles."""
    parts = []
    for el in elements:
        kind = el.get("type","")
        key  = _field_id(el)
        val  = str(el.get("AXValue") or "")
        sel  = str(el.get("selected") or el.get("isSelected") or "")
        f    = el.get("frame") or {}
        y    = round(float(f.get("y") or 0) / 8)
        if key or kind in {"Button","GenericElement","Cell"}:
            parts.append(f"{kind}:{key}:{val}:{sel}:{y}")
    parts.sort()
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:32]


def _element_key(el: dict, elements: Optional[list[dict]] = None) -> str:
    """Identity key for an element, used to dedupe taps globally.

    Label-only whenever that is unambiguous. Position used to be baked into
    EVERY Cell/GenericElement/Image key, so a navigation button that renders a
    few points off between screens (status-bar insets, scroll offset, header
    height) was treated as a brand-new, untapped element each time — Pickup,
    Reserve a table, browseMenu and screenBackBtn were each re-tapped,
    re-opening and re-exploring screens already fully explored.

    Falls back to the old position-qualified key ONLY when *elements* (the
    full current screen) is given AND more than one element on THIS screen
    genuinely shares the same type+label — a real collision (the same dish
    name on two kitchen tickets), where position is the only thing that tells
    them apart. With no *elements* given, behaves exactly as before: always
    position-qualified, the safe default for any call site not updated to
    pass the current screen.
    """
    kind  = el.get("type","")
    label = _field_id(el) or "<unnamed>"
    if kind in ("Cell","GenericElement","Image"):
        if elements is not None:
            dupes = sum(1 for e in elements
                        if e.get("type")==kind
                        and (_field_id(e) or "<unnamed>")==label)
            if dupes <= 1:
                return f"{kind}:{label}"
        f = el.get("frame") or {}
        x = round(float(f.get("x") or 0) / 16) * 16
        y = round(float(f.get("y") or 0) / 16) * 16
        return f"{kind}:{label}:{x}:{y}"
    return f"{kind}:{label}"


def _app_dims(elements: list[dict]) -> Optional[tuple[float,float]]:
    app = next((e for e in elements if e.get("type")=="Application"),None)
    if not app:
        return None
    f = app.get("frame") or {}
    w,h = float(f.get("width") or 0),float(f.get("height") or 0)
    return (w,h) if w and h else None


def _is_springboard(elements: list[dict]) -> bool:
    """Return True when idb is reading the iOS home screen, not the app."""
    if not elements:
        return False
    labels = {_field_id(e).lower() for e in elements}
    sb_count = sum(1 for l in labels if l in _SPRINGBOARD_LABELS)
    has_generic = any(e.get("type")=="GenericElement" for e in elements)
    # SpringBoard: many system-app buttons, no GenericElement
    return sb_count >= 3 and not has_generic


# ══════════════════════════════════════════════════════════════════════════════
# ELEMENT CLASSIFICATION
# ══════════════════════════════════════════════════════════════════════════════

def _is_denied(el: dict) -> bool:
    label = _field_id(el).lower()
    return (any(d in label for d in _DENY_FRAGMENTS)
            or _field_id(el) in _DENY_IDS)


def _is_logout(el: dict) -> bool:
    label = _field_id(el).lower()
    return any(x in label for x in
               ("logout","log out","signout","sign out"))


def _is_profile_edit_screen(elements: list[dict]) -> bool:
    labels = {_field_id(e) for e in elements}
    has_edit = bool(labels & _PROFILE_EDIT_MARKERS)
    has_form = any(e.get("type") in ("TextField","SecureTextField")
                   for e in elements)
    return has_edit and has_form


def _is_login_screen(elements: list[dict]) -> bool:
    labels = {_field_id(e) for e in elements}
    return bool(labels & _EMAIL_IDS) and bool(labels & _PASSWORD_IDS)


def _is_permission_dialog(elements: list[dict]) -> bool:
    types = {e.get("type") for e in elements}
    if "Alert" not in types:
        return False
    triggers = {"would like to send","would like to access",
                "would like to use","location","camera","microphone","photos"}
    has_t = any(e.get("type")=="StaticText"
                and any(t in (e.get("AXLabel") or "").lower() for t in triggers)
                for e in elements)
    allow = {"allow","allow once","allow while using app","ok","continue","don't allow"}
    has_b = any(e.get("type")=="Button"
                and (e.get("AXLabel") or "").lower() in allow
                for e in elements)
    return has_t and has_b


def _is_empty_modal(elements: list[dict]) -> bool:
    """idb returned an effectively empty tree — probably a native modal
    that the accessibility stack does not expose."""
    interactive = [e for e in elements
                   if e.get("type") not in ("Application","Window","Other","")
                   and _field_id(e)]
    return len(interactive) <= 1


def _visible_tappable(elements: list[dict]) -> list[dict]:
    """All labelled, visible, enabled, non-tiny elements sorted top→bottom."""
    result = []
    seen   = set()
    for el in elements:
        kind    = el.get("type","")
        label   = _field_id(el)
        visible = el.get("visible",True)
        enabled = el.get("enabled",True)
        f       = el.get("frame") or {}
        w       = float(f.get("width") or 0)
        h       = float(f.get("height") or 0)

        if not visible or not enabled:
            continue
        if w < 10 or h < 10:
            continue
        if kind in ("Application","Window","Other","StaticText"):
            continue
        if not label:
            continue
        if any(label.endswith(s) for s in _CARD_SKIP_SUFFIXES):
            continue

        key = _element_key(el,elements)
        if key in seen:
            continue
        seen.add(key)
        result.append(el)

    result.sort(key=lambda e:(
        round(float((e.get("frame") or {}).get("y") or 0)/4),
        round(float((e.get("frame") or {}).get("x") or 0)/4),
    ))
    return result


def _normalize_label(label: str) -> str:
    return re.sub(r"[^a-z0-9]","",label.lower())


def _restaurant_like(el: dict, elements: list[dict]) -> bool:
    """True when a GenericElement has a same-named Image sibling on this
    screen — the structural signature a restaurant/listing card has here.
    Confirmed from a real capture of Home: every restaurant card is exactly
    an {Image, GenericElement} pair sharing a name (LasanRestaurant / Lasan
    Restaurant, NylaiKitchen2 / Nylai Kitchen2, nammashop / Namma Shop, ...),
    and no chrome button or category chip has a same-named Image sibling.

    Used ONLY to try these candidates before others on the same page —
    restaurant cards sit far down a long vertical list, so a DFS walk that
    tries candidates in on-screen top-to-bottom order can spend a very long
    time in an earlier sibling's subtree (confirmed: one coupon-icon tap led
    80+ steps deep into product browsing, a menu and a contact-search
    keyboard) before ever getting back to try them. Nothing is skipped by
    this — nothing stops being a candidate, it is only tried sooner.
    """
    if el.get("type") != "GenericElement":
        return False
    norm = _normalize_label(_field_id(el))
    if not norm:
        return False
    return any(e.get("type")=="Image" and _normalize_label(_field_id(e))==norm
               for e in elements)


def _find_close_button(elements: list[dict]) -> Optional[dict]:
    """Find a close/dismiss button on the current screen."""
    for el in elements:
        label = _field_id(el)
        if label in _CLOSE_LABELS:
            return el
    return None


# ══════════════════════════════════════════════════════════════════════════════
# DEVICE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _screenshot(udid: str, path: Path) -> bool:
    try:
        r = subprocess.run(
            ["xcrun","simctl","io",udid,"screenshot",str(path)],
            capture_output=True,timeout=20)
        return r.returncode==0 and path.exists()
    except Exception as e:
        logger.warning("Screenshot failed: %s",e)
        return False


def _launch(udid: str, bundle: str) -> None:
    try:
        subprocess.run(["xcrun","simctl","launch",udid,bundle],
                       capture_output=True,timeout=30)
        time.sleep(_LAUNCH_WAIT_S)
        logger.info("  Launched %s on %s",bundle,udid[:8])
    except Exception as e:
        logger.warning("  Launch failed: %s",e)


def _terminate(udid: str, bundle: str) -> None:
    try:
        subprocess.run(["xcrun","simctl","terminate",udid,bundle],
                       capture_output=True,timeout=10)
        time.sleep(0.5)
    except Exception:
        pass


def _wait_for_render(udid: str) -> None:
    """Block until the app has at least 3 labelled elements (or 15 2s polls
    pass). _ensure_foreground only checks "not SpringBoard", which is true
    the instant the app's bare shell appears — well before the JS bundle
    (Metro) finishes rendering real content. Relaunch-recovery used to skip
    straight back into the step loop on that bare-shell tree, misreading the
    still-loading screen as an unreadable empty modal and burning its whole
    retry+relaunch budget without ever seeing real content (confirmed: 5/5
    recoveries, 0 screens, every attempt logged as "empty modal")."""
    for attempt in range(15):
        time.sleep(2.0)
        probe    = describe_all(udid)
        labelled = [e for e in probe
                    if e.get("type") not in ("Application","Window","Other")
                    and _field_id(e)]
        if len(labelled)>=3 and not _is_springboard(probe):
            logger.info("  Ready after ~%ds (%d labelled).",
                        (attempt+1)*2,len(labelled))
            return
        logger.info("  Loading... %d/15 (%d labelled).",attempt+1,len(labelled))
    logger.warning("  App may not have loaded — proceeding.")


def _ensure_foreground(udid: str, bundle: str) -> bool:
    """Relaunch if the Vya app is not in the foreground."""
    for attempt in range(3):
        els = describe_all(udid)
        if els and not _is_springboard(els):
            return True
        logger.warning(
            "  Foreground check %d/3: app not visible — relaunching.",
            attempt+1)
        subprocess.run(["xcrun","simctl","launch",udid,bundle],
                       capture_output=True,timeout=30)
        time.sleep(6.0)
    logger.error("  Could not bring app to foreground.")
    return False


def _press_back(udid: str, elements: list[dict]) -> bool:
    # NOT type-restricted, and uses _field_id (checks AXIdentifier before
    # AXLabel) — this app's real back control, screenBackBtn, is a
    # GenericElement whose id lives in AXIdentifier. The old
    # type=="Button" + raw-AXLabel check never matched it, so this function
    # silently always fell through to the edge-swipe gesture below, which
    # does not reliably work on this app (confirmed: repeated "back did not
    # change screen" warnings, and an already-exhausted destination being
    # re-pushed and re-walked because leaving it had failed).
    for el in elements:
        if _field_id(el) in ("Back","back","chevron left","screenBackBtn"):
            ok,_ = tap_el(udid,el,elements,scroll=False)
            if ok:
                time.sleep(_BACK_SETTLE_S)
                return True
    dims = _app_dims(elements)
    if dims:
        w,h = dims
        px1,py1 = to_device(udid,5,h/2)
        px2,py2 = to_device(udid,w*0.45,h/2)
        try:
            subprocess.run(
                ["idb","ui","swipe","--udid",udid,
                 str(px1),str(py1),str(px2),str(py2),"--duration","0.35"],
                capture_output=True,timeout=10)
            time.sleep(_BACK_SETTLE_S)
            return True
        except Exception:
            pass
    return False


def _scroll(udid: str, elements: list[dict], direction: str) -> bool:
    try:
        swipe_screen(udid,direction,elements)
        time.sleep(_SCROLL_SETTLE_S)
        return True
    except Exception as e:
        logger.debug("Scroll %s failed: %s",direction,e)
        return False


# Row-reveal gestures (void/comp/split on an order line item): never
# explored before this was added — the Explorer's safety denylist used to
# block reaching an order's Payment/Order screen at all (sending to kitchen,
# completing payment), so no run ever got this far. Kept general/opportunistic
# rather than targeting specific ids: there is no ground-truth element data
# for this screen yet to target precisely.
_ROW_REVEAL_TYPES = ("GenericElement","Cell")


def _swipe_element_reveal(udid: str, el: dict, direction: str = "right") -> bool:
    """A local drag starting INSIDE *el*'s own frame and ending partway
    across it -- the iOS 'swipe a list row to reveal hidden actions' gesture,
    not a full-screen scroll. swipe_screen always drags from screen-centre,
    which would drag a DIFFERENT row under the finger whenever the target
    isn't centred; this stays inside the target's own bounds so the row
    actually being swiped is the one under the finger."""
    f = el.get("frame") or {}
    x,y = float(f.get("x") or 0), float(f.get("y") or 0)
    w,h = float(f.get("width") or 0), float(f.get("height") or 0)
    if w <= 0 or h <= 0:
        return False
    cy = y + h/2
    start_x = x + w*0.85 if direction=="left" else x + w*0.15
    end_x   = x + w*0.15 if direction=="left" else x + w*0.85
    try:
        swipe(udid, start_x, cy, end_x, cy, 0.3)
        time.sleep(_SETTLE_S)
        return True
    except Exception as e:
        logger.debug("Element swipe %s failed: %s",direction,e)
        return False


def _dismiss_permission(udid: str, elements: list[dict]) -> bool:
    preferred = ["Allow While Using App","Allow Once","Allow","OK","Continue"]
    for p in preferred:
        for el in elements:
            if el.get("type")=="Button" and (el.get("AXLabel") or "")==p:
                ok,_ = tap_el(udid,el,elements,scroll=False)
                if ok:
                    time.sleep(0.8)
                    return True
    return False


_MAX_GRID_SCROLLS_PER_DIR = 14   # enough to cross a wide content-space grid
                                 # (confirmed live: waiter booking cards up to
                                 # x~7096 on a 1210-wide viewport -- ~12 swipes
                                 # of swipe_screen's own 25%-of-width reach)


def _try_scroll_element_into_view(
    udid: str, target: dict, elements: list[dict]
) -> Optional[dict]:
    """Scroll to bring an off-screen element into view, trying every
    direction in turn (not interleaved, to avoid oscillating back and forth
    without net progress) -- the waiter's booking timeline is a genuine 2D
    grid in CONTENT space (idb reports content coordinates, not screen
    positions -- AGENTS.md gotcha #2): live cards were seen reporting frames
    up to x~7096, y~1845 on an 1210x834 viewport, which the old 3-swipe,
    horizontal-only search could never reach. Each direction gives up early
    once a swipe stops changing the target's reported frame (truly stuck,
    not just not-yet-found), same rule idb_driver._bring_into_view uses.
    Returns the refreshed element if found, else None.
    """
    key = _element_key(target,elements)
    for direction in ("left", "right", "down", "up"):
        last_frame = None
        for _ in range(_MAX_GRID_SCROLLS_PER_DIR):
            if not _scroll(udid, elements, direction):
                break
            fresh = describe_all(udid)
            if not fresh:
                return None
            elements = fresh
            match = next((e for e in _visible_tappable(fresh)
                          if _element_key(e,fresh)==key), None)
            if match is None:
                continue
            try:
                if hittable(udid, match):
                    return match
            except Exception:
                return match
            frame = match.get("frame")
            if frame == last_frame:
                break   # scrolling this direction no longer moves it
            last_frame = frame
    return None


# ══════════════════════════════════════════════════════════════════════════════
# PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════

def _save_screen(
    nav_hash: str, elements: list[dict], udid: str,
    role: str, session_index: int, raw_dir: Path,
    visited: set[str],
) -> bool:
    if nav_hash in visited:
        return False
    d = raw_dir/nav_hash
    d.mkdir(parents=True,exist_ok=True)
    if not (d/"screenshot.png").exists():
        _screenshot(udid,d/"screenshot.png")
    if not (d/"tree.json").exists():
        (d/"tree.json").write_text(
            json.dumps(elements,indent=2,ensure_ascii=False),encoding="utf-8")
    if not (d/"elements.json").exists():
        (d/"elements.json").write_text(
            json.dumps(_visible_tappable(elements),indent=2,ensure_ascii=False),
            encoding="utf-8")
    if not (d/"meta.json").exists():
        (d/"meta.json").write_text(
            json.dumps({"app_role":role,"session_index":session_index,
                        "discovered_at":datetime.now(timezone.utc).isoformat(),
                        "screen_hash":nav_hash},indent=2),
            encoding="utf-8")
    return True


def _log_transition(
    from_h: str, label: str, to_h: str,
    role: str, action: str, raw_dir: Path,
) -> None:
    key = f"{from_h}:{label}:{to_h}:{role}"
    if key in _logged_transitions:
        return
    _logged_transitions.add(key)
    line = json.dumps({
        "from_hash":from_h,"element_label":label,"to_hash":to_h,
        "app_role":role,"action_type":action,
        "logged_at":datetime.now(timezone.utc).isoformat(),
    },ensure_ascii=False)
    f = raw_dir/"transitions.jsonl"
    with open(f,"a",encoding="utf-8") as fh:
        fh.write(line+"\n")


# ══════════════════════════════════════════════════════════════════════════════
# LOGIN / LOGOUT
# ══════════════════════════════════════════════════════════════════════════════

def _do_login(udid:str,elements:list[dict],email:str,pwd:str) -> bool:
    logger.info("  Login screen — filling credentials.")
    ef = next((e for e in elements
               if _field_id(e) in _EMAIL_IDS
               and e.get("type") in ("TextField","SecureTextField")),None)
    if not ef:
        logger.warning("  Email field not found.")
        return False
    ok,_ = fill(udid,_field_id(ef),email)
    if not ok:
        logger.warning("  fill() failed on email.")
        return False
    pf = next((e for e in elements
               if _field_id(e) in _PASSWORD_IDS
               and e.get("type") in ("TextField","SecureTextField")),None)
    if not pf:
        logger.warning("  Password field not found.")
        return False
    ok,_ = fill(udid,_field_id(pf),pwd)
    if not ok:
        logger.warning("  fill() failed on password.")
        return False
    time.sleep(0.5)
    sb = next((e for e in elements
               if _field_id(e) in _SUBMIT_IDS
               and e.get("type") in ("Button","GenericElement")),None)
    if sb:
        ok,_ = tap_el(udid,sb,elements)
        if ok:
            time.sleep(_LOGIN_WAIT_S)
            return True
    for lbl in ("Sign In","Log In","Login","signIn","Continue"):
        ok,_ = tap(udid,[lbl],elements)
        if ok:
            time.sleep(_LOGIN_WAIT_S)
            return True
    return False


def _do_logout(udid:str,raw_dir:Path,role:str,cur_hash:str) -> bool:
    logger.info("  Ending %s — logging out.",role)
    for _ in range(3):
        els = describe_all(udid)
        if not els:
            break
        logout_el = next((e for e in els if _is_logout(e)),None)
        if logout_el:
            ok,_ = tap_el(udid,logout_el,els)
            if ok:
                time.sleep(2.0)
                new_els  = describe_all(udid)
                new_hash = _nav_hash(new_els) if new_els else "login"
                _log_transition(cur_hash,"logout",new_hash,role,"LOGOUT",raw_dir)
                return True
        for tab in ("Settings","Profile","Account","More"):
            ok,_ = tap(udid,[tab],els)
            if ok:
                time.sleep(_SETTLE_S)
                break
        else:
            break
    logger.warning("  Could not log out.")
    return False


# ══════════════════════════════════════════════════════════════════════════════
# PAGE STATE
# ══════════════════════════════════════════════════════════════════════════════

class _PageState:
    __slots__ = ("nav_hash","tapped","v_scrolls","h_scrolls","family_tapped")
    def __init__(self, nav_hash: str):
        self.nav_hash   = nav_hash
        self.tapped:    set[str] = set()
        self.v_scrolls: int = 0
        self.h_scrolls: int = 0
        # How many members of each known interchangeable-label family (see
        # _family_of) have been tapped on THIS page. Capped at
        # _MAX_FAMILY_TAPS so e.g. a time-slot picker is sampled, not
        # exhaustively tapped one slot at a time.
        self.family_tapped: dict[str,int] = {}


# ══════════════════════════════════════════════════════════════════════════════
# CORE SESSION
# ══════════════════════════════════════════════════════════════════════════════

def explore_session(
    udid: str, bundle: str, role: str,
    email: str, pwd: str,
    raw_dir: Path, session_index: int,
    logout_at_end: bool = False,
    dry_run: bool = False,
) -> dict[str,int]:

    logger.info("═══ Starting %s (udid=%s bundle=%s) ═══",
                role.upper(),udid[:8],bundle)
    raw_dir.mkdir(parents=True,exist_ok=True)

    logged_in = False
    if not dry_run:
        _launch(udid,bundle)
        logger.info("  Waiting for app to render...")
        _wait_for_render(udid)

        if not _ensure_foreground(udid,bundle):
            return {"screens_found":0,"transitions_logged":0,
                    "steps_taken":0,"skipped":0,
                    "terminated_cleanly":False,"relaunch_recoveries":0}

        startup   = describe_all(udid)
        logged_in = not _is_login_screen(startup)
        logger.info("  Already logged in: %s",logged_in)

    # visited is session-local — do NOT pre-populate from disk.
    # _save_screen() is idempotent so we never overwrite existing files,
    # but screens_found must reflect this run's discoveries.
    visited: set[str] = set()

    # Global tapped set — persists across nav_hash changes.
    global_tapped: set[str] = set()

    # Session-wide family counts, for families that open a NEW page per tap
    # (a calendar day chip changes nav_hash, unlike a time-slot/keyboard key,
    # which don't) -- page.family_tapped resets for every new _PageState, so
    # it cannot cap a family that walks forward into a fresh page each time.
    # Confirmed: a 31-day-per-page calendar walked past a full year, one day
    # at a time, before this existed. Deliberately separate from
    # page.family_tapped (used for same-page families) rather than changing
    # that cap's scope, which some other family may rely on per-page.
    global_family_tapped: dict[str,int] = {}
    _GLOBAL_SCOPED_FAMILIES = {"calendar_day"}

    # Pages confirmed fully explored (candidates exhausted, scrolls exhausted,
    # successfully backed out of). Reached again via a DIFFERENT parent, the
    # live dfs_stack has no memory of this — it was popped off once already —
    # so without this set the page is treated as brand new and fully
    # re-walked. duration1..duration4 all opening the identical destination
    # screen is exactly this: only the first should be explored.
    exhausted_hashes: set[str] = set()

    dfs_stack:  list[_PageState] = []
    steps          = 0
    screens_found  = 0
    transitions    = 0
    skipped        = 0
    relaunch_recoveries = 0
    empty_modal_retries = 0
    # Session-wide, NOT the inner 3-attempt cycle's own counter: the login
    # check re-fires on every step now (checked every step so a mid-session
    # force-logout can be recovered from — see below), which means a login
    # that "succeeds" (credentials submitted, _do_login returns True) but
    # bounces straight back to the login screen (e.g. rejected server-side
    # after the fact) re-enters the whole 3-attempt cycle again next step,
    # forever, with nothing bounding the OUTER repetition. Confirmed live:
    # "Login screen — filling credentials." 8+ times with no terminal error.
    login_cycles = 0
    _MAX_LOGIN_CYCLES = 5
    # Flips to False at any abnormal exit (empty tree, login failure, crash,
    # or a dead end that exhausted its relaunch-recovery budget). Stays True
    # only when the loop reaches genuine DFS exhaustion of the home page.
    terminated_cleanly  = True

    if not dry_run:
        time.sleep(1.5)

    while steps < _MAX_STEPS:
        steps += 1

        if not dry_run:
            time.sleep(0.3)
            elements = describe_all(udid)
        else:
            elements = []

        # ── Empty tree retry ───────────────────────────────────────────────────
        if not elements and not dry_run:
            for retry in range(5):
                time.sleep(2.0)
                elements = describe_all(udid)
                if elements:
                    break
            if not elements:
                logger.error("  Step %d: empty tree — ending.",steps)
                terminated_cleanly = False
                break

        # ── SpringBoard detection — relaunch if needed ─────────────────────────
        if not dry_run and _is_springboard(elements):
            logger.warning("  Step %d: SpringBoard detected — relaunching.",steps)
            if not _ensure_foreground(udid,bundle):
                break
            time.sleep(3.0)
            continue

        # ── Permission dialog ──────────────────────────────────────────────────
        if not dry_run and _is_permission_dialog(elements):
            _dismiss_permission(udid,elements)
            continue

        # ── LogBox / crash ─────────────────────────────────────────────────────
        if not dry_run:
            lb = logbox_buttons(elements)
            if lb:
                tap_el(udid,lb[0],elements,scroll=False)
                time.sleep(0.5)
                continue
            cr = crash_text(elements)
            if cr:
                logger.warning("  Step %d: crash — %s",steps,cr[:80])
                terminated_cleanly = False
                break

        # ── Loading spinner ────────────────────────────────────────────────────
        if not dry_run and busy(elements):
            time.sleep(1.5)
            continue

        # ── Empty modal detection ──────────────────────────────────────────────
        # idb sometimes returns a near-empty tree when a native modal is open
        # (same class of screen AGENTS.md notes idb cannot always see into: the
        # table modal, Stripe fields, the duration slider). Try to dismiss it —
        # but this must be BOUNDED. It runs before the DFS stack is touched, so
        # an unreadable modal with no close button and no working back gesture
        # used to retry here forever, never reaching the dead-end recovery
        # below.
        if not dry_run and _is_empty_modal(elements):
            empty_modal_retries += 1
            if empty_modal_retries > _MAX_EMPTY_MODAL_RETRIES:
                if relaunch_recoveries >= _MAX_RELAUNCH_RECOVERIES:
                    logger.error(
                        "  Step %d: stuck on an unreadable modal and the "
                        "relaunch-recovery budget (%d) is exhausted — ending "
                        "session early.",steps,_MAX_RELAUNCH_RECOVERIES)
                    terminated_cleanly = False
                    break
                relaunch_recoveries += 1
                empty_modal_retries = 0
                logger.warning(
                    "  Step %d: unreadable modal after %d attempts — "
                    "relaunch-recovering to home (%d/%d).",
                    steps,_MAX_EMPTY_MODAL_RETRIES,
                    relaunch_recoveries,_MAX_RELAUNCH_RECOVERIES)
                _terminate(udid,bundle)
                _launch(udid,bundle)
                if not _ensure_foreground(udid,bundle):
                    logger.error(
                        "  Step %d: app did not come back after "
                        "relaunch-recovery — ending session.",steps)
                    terminated_cleanly = False
                    break
                _wait_for_render(udid)
                # Unlike the OTHER relaunch-recovery site (Case B3), this one
                # can fire on the very FIRST iteration — before Reconcile-stack
                # has ever run, so dfs_stack may still be empty (no "home" to
                # return to yet). dfs_stack[:1] of [] is still [], so indexing
                # [0] unconditionally crashed here (IndexError) the first time
                # a waiter session opened straight onto an unreadable modal.
                dfs_stack = dfs_stack[:1]
                home_hash = dfs_stack[0].nav_hash if dfs_stack else "unknown"
                _log_transition("unreadable_modal","relaunch_recover",
                                home_hash,role,"RELAUNCH",raw_dir)
                transitions += 1
                continue
            logger.info("  Step %d: empty modal — trying to dismiss (%d/%d).",
                        steps,empty_modal_retries,_MAX_EMPTY_MODAL_RETRIES)
            close = _find_close_button(elements)
            if close:
                tap_el(udid,close,elements,scroll=False)
            else:
                _press_back(udid,elements)
            time.sleep(_SETTLE_S)
            continue
        empty_modal_retries = 0

        # ── Login ──────────────────────────────────────────────────────────────
        # Checked every step, not gated on the startup-only `logged_in` flag:
        # the backend can force-logout mid-session (confirmed live — a
        # kitchen session got "[auth:401] session rejected by server,
        # logging out" from a real API call, landing the app back on the
        # login screen). Gating this on `logged_in` meant that, once True at
        # startup, a later real login screen was never recognised again —
        # the explorer just treated it as a dead page and burned its whole
        # relaunch-recovery budget instead of logging back in.
        if not dry_run and _is_login_screen(elements):
            login_cycles += 1
            if login_cycles > _MAX_LOGIN_CYCLES:
                logger.error(
                    "  Still on the login screen after %d login cycles — "
                    "credentials are being submitted but the app keeps "
                    "bouncing back to login; ending session.",
                    _MAX_LOGIN_CYCLES)
                terminated_cleanly = False
                break
            for attempt in range(3):
                ok = _do_login(udid,elements,email,pwd)
                if ok:
                    logged_in = True
                    break
                time.sleep(2.0)
                elements = describe_all(udid)
            if not logged_in:
                logger.error("  Login failed — ending.")
                terminated_cleanly = False
                break
            elements  = describe_all(udid)
            new_hash  = _nav_hash(elements)
            _log_transition("login","login_submit",new_hash,role,"LOGIN",raw_dir)
            transitions += 1
            continue

        # ── Current navigation hash ────────────────────────────────────────────
        cur_nav = _nav_hash(elements) if elements else f"empty_{steps}"

        # ── Reconcile DFS stack ────────────────────────────────────────────────
        match_idx = None
        for i in range(len(dfs_stack)-1,-1,-1):
            if dfs_stack[i].nav_hash==cur_nav:
                match_idx=i
                break
        if match_idx is not None:
            dfs_stack = dfs_stack[:match_idx+1]
        else:
            dfs_stack.append(_PageState(cur_nav))
        page = dfs_stack[-1]

        # ── Save screen ────────────────────────────────────────────────────────
        if not dry_run:
            is_new = _save_screen(
                cur_nav,elements,udid,role,session_index,raw_dir,visited)
            if is_new:
                visited.add(cur_nav)
                screens_found += 1
                logger.info("  Step %d [NEW] %s (%d elements)",
                            steps,cur_nav[:12],len(elements))

        # ── Profile edit form — back out immediately ───────────────────────────
        if _is_profile_edit_screen(elements):
            logger.info("  Step %d: profile edit — backing out.",steps)
            if not dry_run:
                _press_back(udid,elements)
                _log_transition(cur_nav,"back","__prev__",role,"BACK",raw_dir)
                transitions += 1
            dfs_stack.pop()
            continue

        # ── Build candidates ───────────────────────────────────────────────────
        candidates = []
        for el in _visible_tappable(elements):
            key = _element_key(el,elements)
            if key in page.tapped or key in global_tapped:
                continue
            if _is_denied(el):
                page.tapped.add(key)
                global_tapped.add(key)
                skipped += 1
                continue
            if _is_logout(el):
                page.tapped.add(key)
                global_tapped.add(key)
                continue
            if _field_id(el) in _PROFILE_EDIT_MARKERS:
                page.tapped.add(key)
                global_tapped.add(key)
                skipped += 1
                continue
            fam = _family_of(_field_id(el) or "")
            if fam:
                count = (global_family_tapped.get(fam,0)
                         if fam in _GLOBAL_SCOPED_FAMILIES
                         else page.family_tapped.get(fam,0))
                if count >= _MAX_FAMILY_TAPS:
                    page.tapped.add(key)
                    global_tapped.add(key)
                    skipped += 1
                    continue
            candidates.append(el)

        # Try restaurant/listing cards before chrome and category candidates
        # on this same page — see _restaurant_like. A stable sort, so order
        # within each group (top-to-bottom) is unchanged.
        candidates.sort(key=lambda e: 0 if _restaurant_like(e,elements) else 1)

        # ══════════════════════════════════════════════════════════════════════
        # CASE A — tap the first visible candidate
        # ══════════════════════════════════════════════════════════════════════
        if candidates:
            target = candidates[0]
            key    = _element_key(target,elements)
            label  = _field_id(target) or "<unnamed>"
            page.tapped.add(key)
            global_tapped.add(key)
            fam = _family_of(label)
            if fam:
                page.family_tapped[fam] = page.family_tapped.get(fam,0) + 1
                if fam in _GLOBAL_SCOPED_FAMILIES:
                    global_family_tapped[fam] = global_family_tapped.get(fam,0) + 1

            if dry_run:
                logger.info("  [dry-run] Step %d: would tap %r",steps,label)
                continue

            # Refresh from live tree.
            fresh = describe_all(udid)
            if fresh:
                live = next((e for e in _visible_tappable(fresh)
                             if _element_key(e,fresh)==key),None)
                if live:
                    target   = live
                    elements = fresh

            before_content = _content_hash(elements)
            before_nav     = _nav_hash(elements)

            ok,how = tap_el(udid,target,elements,scroll=False)

            # Off-screen VERTICALLY (e.g. a restaurant card below the fold on
            # Home — AGENTS.md gotcha #2): idb_driver.tap_el already scrolls an
            # element into view when allowed to. We call scroll=False above so
            # our own logging/state stays in front of idb_driver's; retry with
            # it enabled before falling back to anything custom.
            if not ok:
                logger.info(
                    "  Step %d: %r not hittable — trying vertical scroll-into-view.",
                    steps,label)
                ok,how = tap_el(udid,target,elements,scroll=True)

            # Still not hittable? Try horizontal scroll — category chips and
            # horizontal product rows, which idb_driver's own bring-into-view
            # deliberately leaves to the caller (it is vertical-only).
            if not ok:
                logger.info(
                    "  Step %d: %r still not hittable — trying horizontal scroll.",
                    steps,label)
                scrolled = _try_scroll_element_into_view(udid,target,elements)
                if scrolled:
                    fresh2 = describe_all(udid)
                    ok,how = tap_el(udid,scrolled,fresh2 or elements,scroll=False)
                    if fresh2:
                        elements = fresh2

            if not ok:
                logger.info("  Step %d: could not tap %r — skipping.",steps,label)
                continue

            time.sleep(_SETTLE_S)
            after_els     = describe_all(udid)
            after_content = _content_hash(after_els or elements)
            after_nav     = _nav_hash(after_els or elements)

            _log_transition(before_nav,label,after_nav,role,"TAP",raw_dir)
            transitions += 1

            if after_nav==before_nav:
                if after_content!=before_content:
                    logger.info(
                        "  Step %d: tapped %r (%s) — in-page content change.",
                        steps,label,how)
                    page.v_scrolls = 0
                else:
                    logger.info(
                        "  Step %d: tapped %r (%s) — same logical page/no navigation.",
                        steps,label,how)
                    # A plain tap did nothing — on a list-row element this is
                    # exactly the shape of an order line item whose actions
                    # (void/comp/split) are hidden behind a swipe, not a tap.
                    # Try once; if the row reveals new content, treat it like
                    # any other in-page change so the revealed buttons become
                    # candidates on the next pass.
                    if target.get("type") in _ROW_REVEAL_TYPES:
                        swiped = _swipe_element_reveal(udid,target,"right")
                        if swiped:
                            reveal_els = describe_all(udid)
                            reveal_content = _content_hash(reveal_els or elements)
                            if reveal_content != after_content:
                                logger.info(
                                    "  Step %d: swiped %r — revealed new content "
                                    "(void/comp/split row).",steps,label)
                                _log_transition(after_nav,f"{label} (swipe right)",
                                                after_nav,role,"SWIPE",raw_dir)
                                transitions += 1
                                page.v_scrolls = 0
                                if reveal_els:
                                    elements = reveal_els
            elif after_nav in exhausted_hashes:
                # We already fully explored this destination via a different
                # parent (duration1/duration2/duration3/duration4 all opening
                # the SAME screen is exactly this). Leave immediately instead
                # of re-walking it.
                logger.info(
                    "  Step %d: tapped %r (%s) — led to already-exhausted "
                    "page %s; backing out immediately.",
                    steps,label,how,after_nav[:12])
                back_ok   = _press_back(udid,after_els or elements)
                check_els = describe_all(udid) or (after_els or elements)
                if not back_ok or _nav_hash(check_els)==after_nav:
                    # Back did not actually leave — fall through and track it
                    # normally rather than silently losing it off the stack.
                    dfs_stack.append(_PageState(after_nav))
            else:
                # Check if we navigated to a known existing page (back-navigation).
                existing = next(
                    (p for p in dfs_stack if p.nav_hash==after_nav),None)
                if existing:
                    logger.info(
                        "  Step %d: tapped %r (%s) — returned to existing page %s.",
                        steps,label,how,after_nav[:12])
                else:
                    logger.info(
                        "  Step %d: tapped %r (%s) — OPENED child %s → %s.",
                        steps,label,how,before_nav[:12],after_nav[:12])
                    dfs_stack.append(_PageState(after_nav))
            continue

        # ══════════════════════════════════════════════════════════════════════
        # CASE B — no visible candidates; reveal more content
        # ══════════════════════════════════════════════════════════════════════
        if dry_run:
            break

        before_sig = _content_hash(elements)

        # B1 — Vertical scroll
        if page.v_scrolls < _MAX_V_SCROLLS:
            _scroll(udid,elements,"up")
            page.v_scrolls += 1
            fresh = describe_all(udid)
            if fresh and _content_hash(fresh)!=before_sig:
                logger.info("  Step %d: vertical scroll %d/%d — new content.",
                            steps,page.v_scrolls,_MAX_V_SCROLLS)
                continue
            logger.info("  Step %d: vertical scroll %d/%d — no new content.",
                        steps,page.v_scrolls,_MAX_V_SCROLLS)
            continue

        # B2 — Scroll back to top then try horizontal
        logger.info("  Step %d: vertical content exhausted — moving to horizontal check.",steps)
        for _ in range(page.v_scrolls):
            _scroll(udid,elements,"down")

        if page.h_scrolls < _MAX_H_SCROLLS:
            _scroll(udid,elements,"left")
            page.h_scrolls += 1
            fresh = describe_all(udid)
            if fresh and _content_hash(fresh)!=before_sig:
                logger.info("  Step %d: horizontal scroll %d/%d — new content.",
                            steps,page.h_scrolls,_MAX_H_SCROLLS)
                continue
            logger.info("  Step %d: horizontal scroll %d/%d — no new content.",
                        steps,page.h_scrolls,_MAX_H_SCROLLS)
            continue

        # B3 — Truly exhausted — reset horizontal and back out
        for _ in range(page.h_scrolls):
            _scroll(udid,elements,"right")

        logger.info("  Step %d: horizontal content exhausted — backing out.",steps)
        logger.info("  Step %d: page %s exhausted — backing out.",
                    steps,cur_nav[:12])

        if len(dfs_stack)<=1:
            logger.info(
                "  Step %d: cannot go back from root/home — session complete.",steps)
            break

        backed = _press_back(udid,elements)
        after_els  = describe_all(udid) or elements
        after_nav2 = _nav_hash(after_els)
        _log_transition(cur_nav,"back",after_nav2,role,"BACK",raw_dir)
        transitions += 1

        if not backed or after_nav2==cur_nav:
            logger.warning(
                "  Step %d: back did not change screen — stopping branch.",steps)
            # Try a close button before giving up on this branch.
            close = _find_close_button(after_els)
            if close:
                tap_el(udid,close,after_els,scroll=False)
                time.sleep(_SETTLE_S)
                continue

            # A genuine dead end (no back control, no close button) must NOT
            # end the whole session — the home page can still have unexplored
            # buttons elsewhere. Relaunch (resets the app's navigation stack)
            # and resume from home, keeping home's already-tapped state so
            # nothing already explored is re-walked. Bounded, so a truly
            # broken app still ends the session rather than looping forever.
            if relaunch_recoveries >= _MAX_RELAUNCH_RECOVERIES:
                logger.error(
                    "  Step %d: dead end and relaunch-recovery budget (%d) "
                    "exhausted — ending session early (home may be "
                    "incompletely explored).",
                    steps,_MAX_RELAUNCH_RECOVERIES)
                terminated_cleanly = False
                break

            relaunch_recoveries += 1
            logger.warning(
                "  Step %d: dead end — relaunch-recovering to home (%d/%d).",
                steps,relaunch_recoveries,_MAX_RELAUNCH_RECOVERIES)
            _terminate(udid,bundle)
            _launch(udid,bundle)
            if not _ensure_foreground(udid,bundle):
                logger.error(
                    "  Step %d: app did not come back after relaunch-recovery "
                    "— ending session.",steps)
                terminated_cleanly = False
                break
            _wait_for_render(udid)
            dfs_stack = dfs_stack[:1]      # dfs_stack[0] is always home
            _log_transition(cur_nav,"relaunch_recover",
                            dfs_stack[0].nav_hash,role,"RELAUNCH",raw_dir)
            transitions += 1
            continue

        logger.info("  Step %d: backed to %s.",steps,after_nav2[:12])
        exhausted_hashes.add(cur_nav)
        dfs_stack.pop()

    # Fell out of the loop by hitting the step cap, rather than by breaking
    # out at genuine home-page exhaustion — home was not fully explored.
    if steps>=_MAX_STEPS:
        logger.warning("  Hit the %d-step cap before home was fully "
                       "explored.",_MAX_STEPS)
        terminated_cleanly = False

    # ── End of session ─────────────────────────────────────────────────────────
    last_nav = dfs_stack[-1].nav_hash if dfs_stack else "unknown"
    if logout_at_end and not dry_run:
        els = describe_all(udid)
        if els:
            _do_logout(udid,raw_dir,role,last_nav)

    if not dry_run:
        _terminate(udid,bundle)

    summary = {
        "screens_found":       screens_found,
        "transitions_logged":  transitions,
        "steps_taken":         steps,
        "skipped":             skipped,
        # True only when the loop ended via genuine DFS exhaustion of the
        # home page. False for any early/abnormal exit — check this before
        # treating a run as "home fully explored".
        "terminated_cleanly":  terminated_cleanly,
        "relaunch_recoveries": relaunch_recoveries,
    }
    logger.info(
        "═══ %s done: %d screens  %d transitions  %d steps  %d skipped  "
        "clean=%s  recoveries=%d ═══",
        role.upper(),screens_found,transitions,steps,skipped,
        terminated_cleanly,relaunch_recoveries)
    return summary


# ══════════════════════════════════════════════════════════════════════════════
# FULL RUN
# ══════════════════════════════════════════════════════════════════════════════

def explore_all(
    raw_dir: Path = _RAW_DIR,
    roles: Optional[list[str]] = None,
    dry_run: bool = False,
) -> dict[str,Any]:
    cfg     = load_config()
    creds   = cfg.get("credentials",{})
    devices = cfg.get("devices",{})
    sessions = [
        {"role":"consumer","udid":devices.get("consumer",""),
         "bundle":_CONSUMER_BUNDLE,
         "email":creds.get("consumer",{}).get("email",""),
         "pwd":  creds.get("consumer",{}).get("password",""),
         "logout":False},
        {"role":"waiter","udid":devices.get("waiter",""),
         "bundle":_BUSINESS_BUNDLE,
         "email":creds.get("waiter",{}).get("email",""),
         "pwd":  creds.get("waiter",{}).get("password",""),
         "logout":True},
        {"role":"kitchen","udid":devices.get("kitchen",""),
         "bundle":_BUSINESS_BUNDLE,
         "email":creds.get("kitchen",{}).get("email",""),
         "pwd":  creds.get("kitchen",{}).get("password",""),
         "logout":True},
    ]
    if roles:
        sessions = [s for s in sessions if s["role"] in roles]

    total: dict[str,int] = {
        "screens_found":0,"transitions_logged":0,
        "steps_taken":0,"skipped":0,
    }
    for idx,s in enumerate(sessions):
        if not s["udid"]:
            logger.error("No UDID for %r — skipping.",s["role"])
            continue
        if not s["email"] or not s["pwd"]:
            logger.error("No credentials for %r — skipping.",s["role"])
            continue
        result = explore_session(
            udid=s["udid"],bundle=s["bundle"],role=s["role"],
            email=s["email"],pwd=s["pwd"],raw_dir=raw_dir,
            session_index=idx,logout_at_end=s["logout"],dry_run=dry_run,
        )
        for k in total:
            total[k] += result.get(k,0)

    logger.info("All sessions done — %d screens  %d transitions  %d steps  %d skipped.",
                total["screens_found"],total["transitions_logged"],
                total["steps_taken"],total["skipped"])
    return total


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--role",choices=["consumer","waiter","kitchen"])
    p.add_argument("--raw-dir",default=str(_RAW_DIR))
    p.add_argument("--max-steps",type=int,default=_MAX_STEPS)
    p.add_argument("--dry-run",action="store_true")
    args = p.parse_args()
    _MAX_STEPS = args.max_steps
    result = explore_all(
        raw_dir=Path(args.raw_dir),
        roles=[args.role] if args.role else None,
        dry_run=args.dry_run,
    )
    sys.exit(0 if result["screens_found"]>0 or args.dry_run else 1)