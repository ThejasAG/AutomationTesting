"""Fast, VERIFIED screen actions through idb -- the flows' first choice.

Measured on the business iPad (Intel Mac, iOS 26.5), per operation:

    idb describe-all (whole screen)   ~1.0s     Appium find_elements     1-6s
    idb describe-point                ~0.45s    Appium element.click()   1.2-12s
    idb tap                           ~0.4s     Appium .clear()          ~12s
    idb text 'probe'                  ~0.4s     Appium get value         0.8-12s

Typing 'roopa' into a field through Appium cost ~44s (find + click + clear +
send_keys + read-back); a waiter demo spent 6.5 of its 7 minutes in element
commands. The same actions through idb take 2-5s.

idb has two traps, and every action here is built around them:

1. Its coordinates are CONTENT-space. An element scrolled out of its scroll
   view still reports a frame -- measured: the 16:15 time chip reported a frame
   that, on screen, belonged to the SAVE BUTTON. A blind tap there books a
   reservation. So nothing is tapped until `describe-point` at that exact point
   returns the target itself (or one of its children). If it does not, the
   column is swiped to bring the element into view and checked again.

2. It cannot see the software keyboard (a separate process on iOS 26). A point
   under the keyboard still "hits" the button behind it. So a fill closes the
   keyboard (Return) before handing back, and the caller never taps under it.

Anything idb cannot confirm returns False, and the caller falls back to Appium
-- slower, but it scrolls elements into view itself. Nothing here raises.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from typing import List, Optional, Sequence, Tuple

from automation.scenarios.idb_coords import to_device
from automation.scenarios.idb_path import idb_binary

logger = logging.getLogger(__name__)

FIELD_TYPES = ("TextField", "SecureTextField", "TextView", "SearchField")
# HID usage ids (idb ui key / key-sequence)
KEY_RETURN, KEY_BACKSPACE, KEY_DELETE_FORWARD, KEY_SHIFT = "40", "42", "76", "225"


def _idb(args: Sequence[str], timeout: float = 20.0) -> str:
    return subprocess.run([idb_binary(), *args], capture_output=True, text=True,
                          timeout=timeout).stdout


def describe_all(udid: str) -> List[dict]:
    """Every element on screen as idb's raw dicts ([] if idb is unavailable)."""
    try:
        out = _idb(["ui", "describe-all", "--udid", udid])
        return json.loads(out) if out.strip().startswith("[") else []
    except Exception as e:
        logger.debug("describe-all %s: %s", udid[:8], e)
        return []


def describe_point(udid: str, px: int, py: int) -> dict:
    try:
        out = _idb(["ui", "describe-point", "--udid", udid, str(px), str(py)])
        hit = json.loads(out or "{}")
        return hit if isinstance(hit, dict) else {}
    except Exception:
        return {}


def name(e: Optional[dict]) -> str:
    e = e or {}
    return (e.get("AXIdentifier") or e.get("AXLabel") or "").strip()


def frame(e: dict) -> Tuple[float, float, float, float]:
    f = e.get("frame") or {}
    return (float(f.get("x") or 0), float(f.get("y") or 0),
            float(f.get("width") or 0), float(f.get("height") or 0))


def centre(e: dict) -> Tuple[float, float]:
    x, y, w, h = frame(e)
    return x + w / 2, y + h / 2


def app_size(els: List[dict]) -> Tuple[float, float]:
    app = next((e for e in els if e.get("type") == "Application"), None)
    if app:
        return frame(app)[2:]
    return (max((frame(e)[2] for e in els), default=0.0),
            max((frame(e)[3] for e in els), default=0.0))


def _is_target(hit: dict, target: dict) -> bool:
    """describe-point landed on *target* itself, or on an UNNAMED part of it.

    A NAMED element inside the target's frame is something else drawn over it,
    not part of it: an RN control marked accessible reports as one element, so
    a hit on it returns its own name. Measured: the NylaiKitchen2 card image
    (y 803-963) runs under the tab bar (y 780-840); a hit at its centre after a
    short scroll returned 'Wallet', which sits inside the image's frame -- and
    accepting it opened the Wallet instead of the restaurant."""
    if not hit:
        return False
    if name(target) and name(hit) == name(target):
        return True
    if name(hit):
        return False
    hx, hy, hw, hh = frame(hit)
    tx, ty, tw, th = frame(target)
    if hw <= 0 or hh <= 0:
        return False
    return (hx >= tx - 1 and hy >= ty - 1
            and hx + hw <= tx + tw + 1 and hy + hh <= ty + th + 1)


def _probe(udid: str, e: dict) -> dict:
    px, py = to_device(udid, *centre(e))
    return describe_point(udid, px, py)


def hittable(udid: str, e: dict) -> bool:
    return _is_target(_probe(udid, e), e)


def _is_overlay_strip(hit: dict, els: List[dict]) -> bool:
    """A collapsed LogBox strip: full-width, ~48pt tall. It covers the element
    rather than the element being scrolled away -- swiping will not help."""
    w, _h = app_size(els)
    _x, _y, hw, hh = frame(hit)
    return bool(w) and hw >= 0.9 * w and 40 <= hh <= 60


def swipe(udid: str, x1: float, y1: float, x2: float, y2: float,
          duration: float = 0.25) -> None:
    """A drag between two APP-space points (converted for a landscape iPad).

    Never raises: a swipe idb could not finish is a swipe that did not scroll.
    Measured 2026-10-01: one hung for 20s on the iPad and the TimeoutExpired took
    down a whole waiter segment ("segment error") before it had done anything."""
    p1, p2 = to_device(udid, x1, y1), to_device(udid, x2, y2)
    try:
        _idb(["ui", "swipe", "--udid", udid, "--duration", str(duration),
              str(p1[0]), str(p1[1]), str(p2[0]), str(p2[1])])
    except Exception as ex:
        logger.warning("idb swipe on %s did not complete: %s", udid[:8], ex)


def swipe_screen(udid: str, direction: str, els: Optional[List[dict]] = None) -> bool:
    """XCUITest's `mobile: swipe` on the whole app: *direction* is the way the
    finger travels, from the middle of the app."""
    els = els if els is not None else describe_all(udid)
    w, h = app_size(els)
    if not w or not h:
        return False
    cx, cy = w / 2, h / 2
    dx, dy = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}[direction]
    reach_x, reach_y = w * 0.25, h * 0.25
    swipe(udid, cx - dx * reach_x, cy - dy * reach_y, cx + dx * reach_x, cy + dy * reach_y)
    return True


def _find(els: List[dict], names: Sequence[str]) -> List[dict]:
    want = {n for n in names if n}
    return [e for e in els if name(e) in want and frame(e)[2] > 0 and frame(e)[3] > 0]


def _bring_into_view(udid: str, e: dict, els: List[dict],
                     max_swipes: int = 3) -> Optional[dict]:
    """Swipe the element's own column until describe-point hits it.

    Vertical only: a chip off the end of a HORIZONTAL row is left to Appium,
    which scrolls it properly. Returns the element's fresh dict, or None."""
    w, h = app_size(els)
    target = name(e)
    if _is_overlay_strip(_probe(udid, e), els):
        return None
    for _ in range(max_swipes):
        cx, cy = centre(e)
        if not w or not h or not (0 <= cx <= w):
            return None
        mid = h * 0.5
        if abs(cy - mid) < h * 0.08:
            return None                     # already mid-screen and still not hit: covered
        # A SLOW drag moves the list 1:1; a fast one flings it. Measured on the
        # consumer list, a 200pt drag moved it 532pt in 0.25s, 343pt in 0.6s and
        # 190pt in 1.0s or more -- the fast version overshot the card from below
        # the fold to off the top, then back, and never landed. So drag at
        # <= 200pt/s by exactly the distance needed.
        travel = min(max(abs(cy - mid), 80), h * 0.4)
        dur = max(1.0, travel / 200.0)
        if cy > mid:                        # below: finger travels UP
            swipe(udid, cx, h * 0.72, cx, h * 0.72 - travel, dur)
        else:                               # above: finger travels DOWN
            swipe(udid, cx, h * 0.28, cx, h * 0.28 + travel, dur)
        settled = _settle(udid, target, els)
        if settled is None:
            return None
        if frame(settled) == frame(e):
            return None                     # did not move: covered, not scrolled out
        e = settled
        if hittable(udid, e):
            return e
    return None


def _settle(udid: str, target: str, els: List[dict], tries: int = 6) -> Optional[dict]:
    """Wait until the list has STOPPED moving, then return the target's dict.

    A swipe leaves the list gliding (momentum), and on iOS a tap on a moving
    scroll view only stops it -- the item is never pressed. Measured: a
    restaurant card scrolled into view, verified under the point and tapped
    mid-glide, and the restaurant never opened. So the frame must read the same
    twice in a row before anything is tapped."""
    last = None
    for _ in range(tries):
        time.sleep(0.35)
        els[:] = describe_all(udid)
        again = [x for x in _find(els, [target]) if frame(x)[2] > 0]
        if len(again) != 1:
            return None
        if last is not None and frame(again[0]) == frame(last):
            return again[0]
        last = again[0]
    return last


def locate(udid: str, names: Sequence[str], els: Optional[List[dict]] = None,
           scroll: bool = True) -> Tuple[Optional[dict], str]:
    """(element, how) for the ONE on-screen element named in *names* that a tap
    at its centre would really reach -- or (None, why not)."""
    els = els if els is not None else describe_all(udid)
    if not els:
        return None, "idb unavailable"
    # *names* are ALTERNATIVES in priority order (an id, then this build's other
    # spelling of it, then its visible text) -- not one pool: 'menuBtn' and its
    # 'Menu' caption are both on screen, and pooling them read as "ambiguous".
    why = "not on screen"
    for nm in names:
        cands = _find(els, [nm])
        if not cands:
            continue
        hits = [c for c in cands[:3] if hittable(udid, c)]
        if len(hits) == 1:
            return hits[0], "idb"
        if len(hits) > 1:
            return None, f"{len(hits)} elements named {nm!r} -- ambiguous"
        if len(cands) == 1 and scroll:
            e = _bring_into_view(udid, cands[0], els)
            if e is not None:
                return e, "idb, scrolled into view"
        why = "not reachable (covered or scrolled out)"
    return None, why


def tap(udid: str, names: Sequence[str], els: Optional[List[dict]] = None,
        scroll: bool = True) -> Tuple[bool, str]:
    """Tap the element named in *names*, only once it is verified under the point."""
    try:
        e, how = locate(udid, names, els, scroll)
        if e is None:
            return False, how
        px, py = to_device(udid, *centre(e))
        _idb(["ui", "tap", "--udid", udid, str(px), str(py)])
        return True, how
    except Exception as ex:
        return False, f"idb error: {type(ex).__name__}"


def tap_el(udid: str, e: dict, els: Optional[List[dict]] = None,
           scroll: bool = True) -> Tuple[bool, str]:
    """Tap THIS element (not "the one named X") -- for rows that can repeat,
    like the same dish on two kitchen tickets. Same verification as tap()."""
    try:
        els = els if els is not None else describe_all(udid)
        how = "idb"
        if not hittable(udid, e):
            if not scroll:
                return False, "not reachable"
            moved = _bring_into_view(udid, e, list(els))
            if moved is None:
                return False, "not reachable (covered or scrolled out)"
            e, how = moved, "idb, scrolled into view"
        px, py = to_device(udid, *centre(e))
        _idb(["ui", "tap", "--udid", udid, str(px), str(py)])
        return True, how
    except Exception as ex:
        return False, f"idb error: {type(ex).__name__}"


def tap_point(udid: str, x: float, y: float) -> Tuple[bool, str]:
    """Tap an app-space point (e.g. the centre of text read off the screen)."""
    try:
        px, py = to_device(udid, x, y)
        _idb(["ui", "tap", "--udid", udid, str(px), str(py)])
        return True, "idb"
    except Exception as ex:
        return False, f"idb error: {type(ex).__name__}"


def press_return(udid: str) -> None:
    """Return key: closes the keyboard on a single-line field (what Appium's
    hideKeyboard presses too). Measured on the iPad: keyboard gone in 0.4s."""
    try:
        _idb(["ui", "key", KEY_RETURN, "--udid", udid], timeout=10)
    except Exception:
        pass


def _value(udid: str, field: str) -> Optional[str]:
    vals = [e.get("AXValue") for e in describe_all(udid)
            if name(e) == field and e.get("type") in FIELD_TYPES]
    return vals[0] if len(vals) == 1 else None


def _landed(got: Optional[str], text: str, secure: bool) -> bool:
    if got is None:
        return False
    if got == text:
        return True
    # A masked field reads back as bullets of the same length -- and idb can report
    # it as a plain TextField (measured: the Business sign-in's passwordValue is
    # secureTextEntry yet typed 'TextField'), so the bullets decide, not the type.
    return bool(got) and len(got) == len(text) and set(got) <= {"•", "●", "*"}


def fill(udid: str, field: str, text: str, close_keyboard: bool = True,
         info: Optional[dict] = None) -> Tuple[bool, str]:
    """Type *text* into the field named *field* and VERIFY it holds exactly that.

    (ok, what the field holds). The field is cleared first (the apps run with
    noReset, so it can hold a previous value) with delete keys on both sides of
    the caret, so where the tap put the caret does not matter.

    Shift is pressed once before typing. A field with auto-capitalisation arms
    the keyboard's shift at the start (measured: 'probe' -> 'Probe',
    'emp2A@...' -> 'Emp2A@...'); one Shift press cancels that, and pressed when
    nothing is armed it does nothing (measured: mid-text 'x' stayed 'x').

    Efficient on purpose (the sign-in took several attempts per field):
      * a field that already holds exactly *text* is left alone -- no clear, no
        retype (a visible value only; a masked one cannot be read, so it is typed);
      * after typing, the read-back WAITS for the app to update the field (up to
        ~1.2s). Reading once after 0.2s saw the field mid-update, called a correct
        entry wrong, and cleared and retyped it.
    *info*, when given, receives {'attempts': n, 'skipped': bool}."""
    info = info if info is not None else {}
    info.update(attempts=0, skipped=False)
    try:
        els = describe_all(udid)
        fields = [e for e in _find(els, [field]) if e.get("type") in FIELD_TYPES]
        if len(fields) != 1:
            return False, ""
        e, _how = locate(udid, [field], els)
        if e is None or e.get("type") not in FIELD_TYPES:
            return False, ""
        secure = e.get("type") == "SecureTextField"
        got: Optional[str] = e.get("AXValue") or ""
        if got == text:                              # already right: leave it alone
            info["skipped"] = True
            return True, got
        px, py = to_device(udid, *centre(e))
        _idb(["ui", "tap", "--udid", udid, str(px), str(py)])
        time.sleep(0.35)
        for attempt in (1, 2, 3):
            info["attempts"] = attempt
            n = min(max(len(got or ""), len(text)) + 2, 120)
            _idb(["ui", "key-sequence", "--udid", udid,
                  *([KEY_DELETE_FORWARD] * n + [KEY_BACKSPACE] * n)])
            _idb(["ui", "key", KEY_SHIFT, "--udid", udid], timeout=10)
            if attempt < 3:
                _idb(["ui", "text", text, "--udid", udid])
            else:
                # Last try: small bursts, which the RN input keeps up with.
                for i in range(0, len(text), 3):
                    _idb(["ui", "text", text[i:i + 3], "--udid", udid])
                    time.sleep(0.05)
            for _read in range(4):                   # let the field catch up
                time.sleep(0.3)
                got = _value(udid, field)
                if _landed(got, text, secure):
                    if close_keyboard:
                        press_return(udid)
                        time.sleep(0.3)
                    return True, got or ""
                if got is not None and len(got) >= len(text):
                    break                            # complete but wrong: retype
        return False, got or ""
    except Exception as ex:
        logger.debug("idb fill %s on %s: %s", field, udid[:8], ex)
        return False, ""


# -- screen checks (one describe-all instead of several Appium predicates) --

_CRASH_MARKERS = ("Render Error", "RCTFatal", "No bundle URL", "RedBox")


def busy(els: List[dict]) -> bool:
    """A spinner or a 'loading…' text is up."""
    for e in els:
        t = e.get("type") or ""
        label = (e.get("AXLabel") or "").lower()
        if "ActivityIndicator" in t or "ProgressIndicator" in t or label == "in progress":
            return True
        if t == "StaticText" and ("loading" in label or "please wait" in label):
            return True
    return False


def crash_text(els: List[dict]) -> Optional[str]:
    for e in els:
        label = e.get("AXLabel") or ""
        if (e.get("type") == "StaticText"
                and any(m in label for m in _CRASH_MARKERS)):
            return label[:160]
    return None


def logbox_buttons(els: List[dict]) -> List[dict]:
    return [e for e in els if name(e) in ("Dismiss", "Minimize")]


# -- pixels (for state accessibility does not expose) --------------------------

def sample_colors(udid: str, points: Sequence[Tuple[float, float]],
                  els: Optional[List[dict]] = None, radius: float = 1.5
                  ) -> List[Optional[Tuple[int, int, int]]]:
    """Mean RGB around each APP-space point, from ONE simulator screenshot.

    For state the accessibility tree does not carry -- measured on the kitchen
    card: a product radio reports the same attributes selected or not, and Ready
    reports enabled=True while it is visibly disabled. The pixels do change: the
    radio's dot fills, Ready turns from pale to dark purple.

    The screenshot is the PORTRAIT device framebuffer, so points go through the
    same mapping as taps (to_device), then the framebuffer's scale."""
    import os
    import tempfile
    try:
        from PIL import Image
    except Exception:
        return [None] * len(points)
    els = els if els is not None else describe_all(udid)
    w, h = app_size(els)
    if not w or not h:
        return [None] * len(points)
    dev_w = h if w > h else w                 # portrait device width, in points
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        subprocess.run(["xcrun", "simctl", "io", udid, "screenshot", path],
                       capture_output=True, timeout=20)
        img = Image.open(path).convert("RGB")
    except Exception:
        return [None] * len(points)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    scale = img.width / dev_w
    out: List[Optional[Tuple[int, int, int]]] = []
    for x, y in points:
        px, py = to_device(udid, x, y)
        cx, cy = px * scale, py * scale
        r = max(1, int(radius * scale))
        box = img.crop((int(cx - r), int(cy - r), int(cx + r) + 1, int(cy + r) + 1))
        pix = list(box.getdata())
        if not pix:
            out.append(None)
            continue
        out.append(tuple(int(sum(c[i] for c in pix) / len(pix)) for i in range(3)))
    return out


def is_dark(rgb: Optional[Tuple[int, int, int]], threshold: int = 140) -> Optional[bool]:
    """Filled/active (the app's purple) vs empty/disabled (white, pale lilac)."""
    if rgb is None:
        return None
    return (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) < threshold
