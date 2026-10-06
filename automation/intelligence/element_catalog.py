"""Automation identifier catalog — how an element was found, and how sure we are.

An app screen is not automatable only when it has testIDs. Most of this app's
controls carry no identifier at all: the product rows inside `products-list` are
XCUIElementTypeOther with no testID and no accessibilityLabel, and the only thing
distinguishing one from another is the text they render ("TagliatellealMonti
5.1 ★ 17.51 €"). Blocking on that would mean no product can ever be added, and
therefore that cart, checkout and payment can never be tested.

So the framework names them itself. Every element resolved by something weaker
than a real identifier gets a TEMPORARY id minted here (`auto-product-…`), cached
for the session so the same element resolves to the same name twice, and recorded
with:

  * the fallback METHOD that found it (hierarchy / text / partial-id / container)
  * a CONFIDENCE score, so a guess never reads like a certainty
  * the SCREEN it was on, so the report can tell developers where to add testIDs

The temporary ids are a crutch, not a fix — they are derived from rendered text,
so they break the moment a product is renamed or translated. That is exactly why
`report()` exists: it is the technical-debt list for the app team.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# How the element was located, best first. The score is the confidence attached
# to anything found that way.
METHOD_EXACT_ID = "exact-accessibility-id"      # a real testID / accessibilityLabel
METHOD_CONTAINER = "container-subtree"          # a known tappable wrapper
METHOD_PARTIAL_ID = "partial-id-match"          # name CONTAINS keyword
METHOD_TEXT = "text-match"                      # matched the text it renders
METHOD_HIERARCHY = "hierarchy-scan"             # found by walking a parent's children

CONFIDENCE: Dict[str, float] = {
    METHOD_EXACT_ID: 1.00,      # the app named it; not a guess
    METHOD_CONTAINER: 0.85,     # a real wrapper id + a keyword hit inside it
    METHOD_PARTIAL_ID: 0.70,    # the id merely contains the word
    METHOD_TEXT: 0.60,          # rendered text — breaks on rename/translation
    METHOD_HIERARCHY: 0.50,     # position in the tree — breaks on any re-layout
}

# Real identifiers, so the report can say which screens are actually covered.
_REAL_METHODS = {METHOD_EXACT_ID}


@dataclass
class CatalogEntry:
    """One element the framework had to name for itself."""
    auto_id: str
    method: str
    confidence: float
    screen: str
    text: str                       # what the element rendered when we found it
    step: str                       # the scenario step that needed it
    hint: str = ""                  # what the developer should add

    def to_dict(self) -> dict:
        return {
            "auto_id": self.auto_id,
            "method": self.method,
            "confidence": round(self.confidence, 2),
            "screen": self.screen,
            "element_text": self.text[:80],
            "needed_by_step": self.step,
            "suggested_fix": self.hint,
        }


def slugify(text: str, prefix: str = "auto") -> str:
    """A stable, readable temporary id from an element's own text.

    "TagliatellealMonti 5.1 ★ 17.51 €" -> "auto-tagliatellealmonti-5-1-17-51"
    Stable within a session, so the same row resolves to the same name twice.
    """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return f"{prefix}-{cleaned[:48]}" if cleaned else f"{prefix}-unnamed"


class AutoIdCatalog:
    """Session-scoped store of generated identifiers and how they were found."""

    def __init__(self) -> None:
        self._entries: Dict[str, CatalogEntry] = {}     # auto_id -> entry
        self._by_text: Dict[str, str] = {}              # element text -> auto_id
        self._resolutions: List[dict] = []              # every resolution, incl. real ids

    # ── recording ────────────────────────────────────────────────────────────

    def record_resolution(self, step: str, method: str, identifier: str,
                          screen: str, confidence: Optional[float] = None) -> None:
        """Log EVERY resolution — real ids included — so the report can show
        coverage, not just gaps."""
        self._resolutions.append({
            "step": step,
            "method": method,
            "identifier": identifier,
            "screen": screen,
            "confidence": confidence if confidence is not None else CONFIDENCE.get(method, 0.5),
            "real_id": method in _REAL_METHODS,
        })

    def mint(self, text: str, method: str, screen: str, step: str,
             prefix: str = "auto", hint: str = "") -> CatalogEntry:
        """Name an element the app never named. Reuses the id for the same text."""
        existing_id = self._by_text.get(text)
        if existing_id:
            return self._entries[existing_id]

        auto_id = slugify(text, prefix)
        # Same text, different element — keep ids unique.
        n = 2
        base = auto_id
        while auto_id in self._entries:
            auto_id = f"{base}-{n}"
            n += 1

        entry = CatalogEntry(
            auto_id=auto_id,
            method=method,
            confidence=CONFIDENCE.get(method, 0.5),
            screen=screen,
            text=text,
            step=step,
            hint=hint or f'Add accessibilityLabel="{auto_id}" to this element.',
        )
        self._entries[auto_id] = entry
        self._by_text[text] = auto_id
        self.record_resolution(step, method, auto_id, screen, entry.confidence)
        return entry

    def lookup(self, text: str) -> Optional[CatalogEntry]:
        auto_id = self._by_text.get(text)
        return self._entries.get(auto_id) if auto_id else None

    # ── reporting ────────────────────────────────────────────────────────────

    def report(self) -> dict:
        """Technical-debt report for the development team."""
        generated = [e.to_dict() for e in self._entries.values()]
        total = len(self._resolutions)
        real = sum(1 for r in self._resolutions if r["real_id"])

        # Which screens leaned on invented ids the most — where testIDs pay off.
        by_screen: Dict[str, int] = {}
        for e in self._entries.values():
            by_screen[e.screen] = by_screen.get(e.screen, 0) + 1

        methods: Dict[str, int] = {}
        for r in self._resolutions:
            methods[r["method"]] = methods.get(r["method"], 0) + 1

        confidences = [e.confidence for e in self._entries.values()]
        return {
            "summary": {
                "elements_resolved": total,
                "with_real_identifier": real,
                "needed_generated_identifier": len(generated),
                "coverage_pct": round(100.0 * real / total, 1) if total else 100.0,
                "lowest_confidence": round(min(confidences), 2) if confidences else None,
            },
            "fallback_methods_used": methods,
            "screens_needing_accessibility_ids": [
                {"screen": s, "elements_without_ids": n}
                for s, n in sorted(by_screen.items(), key=lambda kv: -kv[1])
            ],
            "generated_identifiers": generated,
            "resolutions": self._resolutions,
        }

    def render(self) -> str:
        """The report as text, for a log or a PR comment."""
        r = self.report()
        s = r["summary"]
        out = [
            "AUTOMATION IDENTIFIER REPORT",
            "=" * 64,
            f"  elements resolved      : {s['elements_resolved']}",
            f"  had a real identifier  : {s['with_real_identifier']} ({s['coverage_pct']}%)",
            f"  framework had to invent: {s['needed_generated_identifier']}",
            f"  lowest confidence      : {s['lowest_confidence']}",
            "",
            "FALLBACK METHODS USED",
        ]
        for m, n in sorted(r["fallback_methods_used"].items(), key=lambda kv: -kv[1]):
            out.append(f"  {m:<26} {n:>3}   (confidence {CONFIDENCE.get(m, 0.5):.2f})")

        if r["generated_identifiers"]:
            out += ["", "TECHNICAL DEBT — elements with no accessibility identifier"]
            for e in r["generated_identifiers"]:
                out.append(f"  [{e['confidence']:.2f}] {e['auto_id']}")
                out.append(f"         screen : {e['screen']}")
                out.append(f"         found  : {e['method']} on {e['element_text']!r}")
                out.append(f"         step   : {e['needed_by_step']}")
                out.append(f"         fix    : {e['suggested_fix']}")

        if r["screens_needing_accessibility_ids"]:
            out += ["", "SCREENS DEVELOPERS SHOULD ADD IDENTIFIERS TO"]
            for row in r["screens_needing_accessibility_ids"]:
                out.append(f"  {row['screen']:<28} {row['elements_without_ids']} element(s)")
        return "\n".join(out)
