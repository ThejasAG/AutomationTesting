"""Which elements are covered by something else — the check that would have caught
three of the four bugs that ate 2026-09-02.

Every one of them looked like "the automation can't find/tap it", and every one was
actually "something is drawn on top of it":

    saveBtn   y 685..735   toast y 708..756   -> Save tapped, form never closed
    slot 17:00 y=730       toast y 726..774   -> tap opened the LogBox VIEWER
    NylaiKitchen2 cy=882   screen height 852  -> off-screen, "no element matches"

Each cost hours to find by reading frames by hand. It is a rectangle intersection.

Pure functions over the element dicts idb already returns, so this is testable with
no device and can run against a live tree or a stored one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h

    def overlap_area(self, other: "Rect") -> float:
        ox = max(0.0, min(self.x + self.w, other.x + other.w) - max(self.x, other.x))
        oy = max(0.0, min(self.y + self.h, other.y + other.h) - max(self.y, other.y))
        return ox * oy


def rect_of(el: Dict) -> Optional[Rect]:
    """Rect from an idb element dict, or None if it has no usable frame."""
    f = el.get("frame") or {}
    try:
        r = Rect(float(f.get("x", 0)), float(f.get("y", 0)),
                 float(f.get("width", 0)), float(f.get("height", 0)))
    except (TypeError, ValueError):
        return None
    return r if r.area > 0 else None


def label_of(el: Dict) -> str:
    return (el.get("AXLabel") or el.get("label") or "").strip()


def off_screen(el: Dict, screen_w: float, screen_h: float,
               safe_margin: float = 0.0) -> bool:
    """Is the point a coordinate tap would use outside the usable area?

    *safe_margin* excludes a band at the top and bottom: an element technically on
    screen but under the header is still untappable. Measured — a card scrolled to
    y=29 was 'visible' and its tap hit the status bar.
    """
    r = rect_of(el)
    if r is None:
        return False
    return not (0 <= r.cx <= screen_w and safe_margin <= r.cy <= screen_h - safe_margin)


def covered_by(target: Dict, others: Sequence[Dict],
               min_fraction: float = 0.05) -> List[Tuple[Dict, float]]:
    """Elements drawn over *target*, with the fraction of it they cover.

    Sorted worst first. A tap uses the CENTRE, so anything containing the centre is
    reported regardless of *min_fraction* — that is the case that actually breaks
    taps, and by area alone a thin strip across a button looks harmless.

    *min_fraction* defaults to 5%: on a real screen almost everything clips its
    neighbour by a pixel, and reporting those produced 60 findings of "covers 0% of
    it" — noise that buries the two that matter.
    """
    tr = rect_of(target)
    if tr is None:
        return []
    hits: List[Tuple[Dict, float]] = []
    for other in others:
        if other is target:
            continue
        orr = rect_of(other)
        if orr is None:
            continue
        # A parent/ancestor is one that ENCLOSES the target — that is layout, not
        # occlusion. Judging it by area alone was wrong: the phone's toast
        # (373x48) is 5x the area of a time slot (72x48) but sits ACROSS it, and
        # an area rule silently dropped the exact bug this exists to catch.
        if (orr.x <= tr.x and orr.y <= tr.y
                and orr.x + orr.w >= tr.x + tr.w
                and orr.y + orr.h >= tr.y + tr.h):
            continue
        frac = orr.overlap_area(tr) / tr.area if tr.area else 0.0
        if frac <= 0:
            continue
        if orr.contains(tr.cx, tr.cy) or frac >= min_fraction:
            hits.append((other, round(frac, 3)))
    hits.sort(key=lambda h: h[1], reverse=True)
    return hits


def report(elements: Sequence[Dict], screen_w: float, screen_h: float,
           safe_margin: float = 0.0, only_labelled: bool = True) -> List[Dict]:
    """Every labelled element that is covered or out of reach.

    This is the whole feature: run it on a screen and it names what cannot be tapped
    and why, instead of a person diffing frames by hand.
    """
    subjects = [e for e in elements if (not only_labelled or label_of(e)) and rect_of(e)]
    out: List[Dict] = []
    for el in subjects:
        r = rect_of(el)
        issues = []
        if off_screen(el, screen_w, screen_h, safe_margin):
            issues.append({"kind": "off_screen",
                           "detail": f"tap point ({int(r.cx)}, {int(r.cy)}) is outside "
                                     f"the usable area {int(screen_w)}x{int(screen_h)}"
                                     + (f" (margin {int(safe_margin)})" if safe_margin else "")})
        for other, frac in covered_by(el, subjects):
            issues.append({"kind": "covered",
                           "by": label_of(other) or "(unlabelled)",
                           "fraction": frac,
                           "detail": f"covers {int(frac * 100)}% of it"
                                     + (" INCLUDING its tap point" if
                                        (rect_of(other) or Rect(0, 0, 0, 0)).contains(r.cx, r.cy)
                                        else "")})
        if issues:
            out.append({"label": label_of(el),
                        "frame": {"x": int(r.x), "y": int(r.y),
                                  "w": int(r.w), "h": int(r.h)},
                        "issues": issues})
    return out
