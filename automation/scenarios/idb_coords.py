"""Turn a point from `idb ui describe-all` into the point `idb ui tap` expects.

On a portrait device they are the same. On a landscape app (the business iPad)
they are not: describe-all reports landscape app coordinates (1210 x 834) while
tap -- and describe-point -- take portrait device coordinates (834 x 1210).
Which way the landscape is turned depends on how the simulator was last
rotated, so no fixed formula is right. Measured on the same iPad:

    homeBtn  describe-all (48, 65)   ->  tap (834 - 65, 48)    [today]
    addNewEvent (725, 723)           ->  tap (723, 1210 - 725) [earlier]

A fixed formula made every iPad idb tap land ~240pt off: 'Save does nothing',
'form never opened', a tap on Table opening History.

So the direction is MEASURED: take an element whose describe-all frame is
known, convert its centre both ways, and ask describe-point which one hits that
same element. The answer is cached per simulator for a short while.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from typing import Dict, List, Optional, Tuple

from automation.scenarios.idb_path import idb_binary
from automation.scenarios import idb_fast as _idb_fast

logger = logging.getLogger(__name__)

# The direction only changes when someone rotates the simulator, so re-measuring
# every 2 minutes bought nothing and kept re-measuring mid-form (see _measure).
TTL = 600.0
# After a FAILED measurement, try again this soon rather than living with a guess.
RETRY_AFTER = 5.0
_CACHE: Dict[str, Tuple[float, str, float, float]] = {}   # udid -> (t, mode, w, h)
_GOOD: Dict[str, str] = {}      # udid -> last direction a describe-point CONFIRMED

# _GOOD is also kept ON DISK. A screen covered by a dialog (an RN Modal: idb sees
# just the app and one block) cannot be measured, and right after a backend
# restart there was no confirmed direction in memory -- so it guessed, and the
# Inspector showed the iPad upside down (measured 2026-10-06, the split dialog).
_GOOD_FILE = os.path.join(os.path.expanduser("~"), ".vya-platform", "idb_rotation.json")


def _load_good() -> None:
    try:
        with open(_GOOD_FILE) as f:
            data = json.load(f)
        _GOOD.update({k: v for k, v in data.items() if v in ("ccw", "cw")})
    except (OSError, ValueError):
        pass


def _save_good(udid: str, mode_: str) -> None:
    if _GOOD.get(udid) == mode_:
        return
    _GOOD[udid] = mode_
    try:
        os.makedirs(os.path.dirname(_GOOD_FILE), exist_ok=True)
        tmp = _GOOD_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_GOOD, f)
        os.replace(tmp, _GOOD_FILE)
    except OSError:
        pass


_load_good()

# mode -> device point for an app point (x, y) on a w x h landscape app
_MODES = {
    "same": lambda x, y, w, h: (x, y),
    "ccw": lambda x, y, w, h: (h - y, x),
    "cw": lambda x, y, w, h: (y, w - x),
}


def _run_json(args: List[str], timeout: float = 15):
    out = _idb_fast.subprocess_run([idb_binary(), *args], capture_output=True, text=True,
                         timeout=timeout).stdout
    return json.loads(out or "null")


def _name(e) -> str:
    if isinstance(e, list):
        e = e[0] if e else {}
    e = e or {}
    return (e.get("AXIdentifier") or e.get("AXLabel") or "").strip()


def _measure(udid: str, elements: Optional[list] = None) -> Tuple[str, float, float]:
    els = elements if elements is not None else _run_json(
        ["ui", "describe-all", "--udid", udid])
    app = next((e for e in els or [] if e.get("type") == "Application"), None)
    f = (app or {}).get("frame") or {}
    w, h = float(f.get("width") or 0), float(f.get("height") or 0)
    if not w or w <= h:
        return "same", w, h
    # Small, named, distinct elements make unambiguous probes.
    # ON SCREEN only: a scrolled card reports content-space frames (measured:
    # y=-294 on the iPad payment card), and probing those hit nothing, so the
    # measurement fell back to a guess with good probes further down the list.
    def _on_screen(e):
        f = e.get("frame") or {}
        return (0 < f.get("width", 0) < w / 2 and 0 <= f.get("x", -1)
                and f.get("x", 0) + f.get("width", 0) <= w
                and 0 <= f.get("y", -1) and f.get("y", 0) + f.get("height", 0) <= h)
    probes = [e for e in els if e.get("type") != "Application" and _name(e)
              and _on_screen(e)]
    names = [_name(e) for e in probes]
    # TOP LAYER FIRST. describe-all lists what is drawn last (a form, a sheet, a
    # modal) at the END. With the New Appointment form open, the first six named
    # elements were all BEHIND its dimmed backdrop: every probe hit the backdrop,
    # the measurement "failed", and the guess (cw) was the wrong way round -- so
    # every iPad tap missed for the next 2 minutes and @first_time_slot failed with
    # '12:05Btn' plainly on screen (2026-10-06).
    probes = [e for e in probes if names.count(_name(e)) == 1][::-1][:12]
    for e in probes:
        pf = e["frame"]
        cx, cy = pf["x"] + pf["width"] / 2, pf["y"] + pf["height"] / 2
        for mode in ("ccw", "cw"):
            px, py = _MODES[mode](cx, cy, w, h)
            try:
                hit = _run_json(["ui", "describe-point", "--udid", udid,
                                 str(int(px)), str(int(py))])
            except Exception:
                continue
            if _name(hit) == _name(e):
                return mode, w, h
    return "", w, h          # unmeasured: the caller decides (see to_device)


def to_device(udid: str, x: float, y: float,
              elements: Optional[list] = None) -> Tuple[int, int]:
    """Device point for idb tap/swipe from a describe-all (app-space) point.

    Never raises: if anything fails, the point is returned unchanged."""
    try:
        now = time.time()
        c = _CACHE.get(udid)
        if not c or now - c[0] > TTL:
            mode, w, h = _measure(udid, elements)
            if mode:
                if mode != "same":
                    _save_good(udid, mode)
                _CACHE[udid] = c = (now, mode, w, h)
            elif w > h and _measure_by_text(udid):
                # No element to probe (a dialog covers the screen): the screenshot
                # decides -- the turn whose text reads is the right one.
                mode = _measure_by_text.last
                _save_good(udid, mode)
                _CACHE[udid] = c = (now, mode, w, h)
            else:
                # Never trust a guess over a direction we have SEEN work: keep the
                # last confirmed one, and measure again in a few seconds instead of
                # living with an unconfirmed answer for the whole TTL.
                mode = _GOOD.get(udid, "cw")
                logger.warning("idb_coords: could not measure rotation on %s; using %s (%s)",
                               udid[:8], mode, "last confirmed" if udid in _GOOD else "a guess")
                _CACHE[udid] = c = (now - TTL + RETRY_AFTER, mode, w, h)
        _, mode, w, h = c
        px, py = _MODES[mode](x, y, w, h)
        return int(px), int(py)
    except Exception as e:
        logger.debug("idb_coords.to_device(%s): %s", udid[:8], e)
        return int(x), int(y)


def _measure_by_text(udid: str) -> bool:
    """Which landscape turn makes the screen's TEXT readable ('ccw' or 'cw')?

    For a screen with nothing to probe: a dialog (an RN Modal) leaves idb just the
    app and one block, centred, so a describe-point probe cannot tell the two turns
    apart (they differ by 180°). Text recognition can: it reads upright text and
    gets almost nothing from upside-down text. Measured 2026-10-06 on the split
    dialog, after the simulator had been turned: the remembered direction was
    wrong and the Inspector showed the iPad upside down. Sets
    _measure_by_text.last; False when it cannot tell."""
    _measure_by_text.last = ""
    try:
        import tempfile
        from PIL import Image
        from automation.scenarios import screen_text
        binary = screen_text.ocr_binary()
        if not binary:
            return False
        fd, shot = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        crop = shot.replace(".png", "-t.png")
        try:
            subprocess.run(["xcrun", "simctl", "io", udid, "screenshot", shot],
                           capture_output=True, timeout=20)
            raw = Image.open(shot).convert("L")
            raw = raw.resize((raw.width // 2, raw.height // 2))
            score = {}
            for turn in ("ccw", "cw"):
                upright(raw, turn).save(crop)
                out = subprocess.run([binary, crop], capture_output=True, text=True,
                                     timeout=30).stdout
                # Count REAL words: upside-down text still "reads", as gibberish
                # (measured: 'Aiddv', 's isang' vs 'ORDER SUMMARY', 'Select All').
                words = _dictionary()
                score[turn] = sum(
                    1 for line in out.splitlines() if line.count("\t") >= 4
                    for tok in re.findall(r"[A-Za-z]{3,}", line.split("\t")[-1])
                    if tok.lower() in words)
        finally:
            for f in (shot, crop):
                try:
                    os.remove(f)
                except OSError:
                    pass
        best = max(score, key=score.get)
        other = min(score, key=score.get)
        if score[best] >= 3 and score[best] >= 2 * max(score[other], 1):
            _measure_by_text.last = best
            logger.info("idb_coords: %s measured %s from the screen text (%s)",
                        udid[:8], best, score)
            return True
        return False
    except Exception as e:
        logger.debug("idb_coords: text measurement on %s failed: %s", udid[:8], e)
        return False


_measure_by_text.last = ""
_WORDS: set = set()


def _dictionary() -> set:
    """English words (macOS ships /usr/share/dict/words), plus this app's UI words."""
    if not _WORDS:
        try:
            with open("/usr/share/dict/words") as f:
                _WORDS.update(w.strip().lower() for w in f if len(w.strip()) >= 3)
        except OSError:
            pass
        _WORDS.update({"guest", "apply", "select", "order", "summary", "pasta", "home",
                       "history", "menu", "total", "table", "filter", "bookings", "orders",
                       "serve", "reserved", "payment", "confirm", "cash", "voucher", "void"})
    return _WORDS


def mode(udid: str, elements: Optional[list] = None) -> str:
    """Which way the app is turned on the device: 'same', 'ccw' or 'cw'
    (measured, then cached like to_device)."""
    to_device(udid, 1, 1, elements)
    c = _CACHE.get(udid)
    if c:
        return c[1]
    # The measurement itself errored (no cache entry): the last direction seen
    # working beats "same", which turns a landscape screenshot by nothing at all.
    return _GOOD.get(udid, "same")


def upright(image, mode_: str):
    """A PIL image of the device framebuffer (always portrait) turned the way the
    app is, so it lines up with describe-all frames."""
    return image.rotate({"ccw": 90, "cw": 270}.get(mode_, 0), expand=True)


def forget(udid: str = "") -> None:
    """Drop the cached direction (e.g. after the simulator was rotated)."""
    # Only the MEASUREMENT is dropped. The last confirmed direction (_GOOD) stays as
    # the fallback: the Inspector calls this on every Inspect, and a failed probe
    # right after must fall back to what was last seen working, not a bare guess.
    # The next successful measurement overwrites it anyway.
    if udid:
        _CACHE.pop(udid, None)
    else:
        _CACHE.clear()
