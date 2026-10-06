"""Turn a running app's accessibility tree into two things:

1. a CATALOG of real, usable locators (elements that expose an accessibility id) —
   this is what grounds the AI script generator so it stops inventing testIDs;
2. a GAP REPORT of interactive elements that have NO id of their own — the exact
   list the app team must instrument, formatted as ready-to-file issues.

The parser is a pure function over Appium's page_source XML, so it is testable
without a device. `collect_from_driver` is the thin live wrapper.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# iOS element types a user actually interacts with — an unlabelled one of these is
# a real automation gap. Static text / images without ids are usually fine.
_INTERACTIVE = {
    "XCUIElementTypeButton",
    "XCUIElementTypeTextField",
    "XCUIElementTypeSecureTextField",
    "XCUIElementTypeSearchField",
    "XCUIElementTypeSwitch",
    "XCUIElementTypeSlider",
    "XCUIElementTypeCell",
    "XCUIElementTypeLink",
    # Android equivalents, so the same tool serves both platforms.
    "android.widget.Button",
    "android.widget.EditText",
    "android.widget.ImageButton",
    "android.widget.CheckBox",
}


@dataclass
class CatalogEntry:
    testid: str
    type: str
    label: Optional[str]


@dataclass
class Gap:
    type: str
    label: Optional[str]           # visible text, if any
    nearest_text: Optional[str]    # a sibling/child label to help name it
    parent_id: Optional[str]
    x: Optional[int]
    y: Optional[int]
    suggested_testid: str


@dataclass
class Inspection:
    catalog: List[CatalogEntry] = field(default_factory=list)
    gaps: List[Gap] = field(default_factory=list)
    # testIDs that exist in the React tree but were folded into a parent's name —
    # detectable, but NOT individually tappable (need accessible={true}).
    merged: set = field(default_factory=set)


def _id_of(el: ET.Element) -> str:
    # iOS exposes the accessibility id as `name`; Android as `resource-id`.
    return (el.get("name") or el.get("resource-id") or "").strip()


def is_atomic_testid(value: str) -> bool:
    """True for a real, targetable testID — not a merged parent label or LogBox text.

    iOS sets a container's `name` to the space-joined ids of its children when they
    aren't individually accessible ("locationModal homeRestuarantBar …"), and
    LogBox error panels expose long sentences as `name`. Neither is a usable
    locator. A real testID has no whitespace and a sane length.
    """
    if not value or any(c.isspace() for c in value):
        return False
    return 2 <= len(value) <= 40


_TESTID_SHAPE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:[A-Z][A-Za-z0-9]*|[-_][A-Za-z0-9]+)+$")


def _looks_like_testid(token: str) -> bool:
    """A token shaped like a real testID: camelCase or kebab/snake, alphanumerics
    only. Rejects dictionary words ('the', 'valid'), punctuation, URLs, emoji —
    which is what LogBox error text is made of."""
    return bool(_TESTID_SHAPE.match(token))


def is_merged_parent(value: str) -> bool:
    """A `name` that is several REAL testIDs joined by spaces — the signature of
    children folded into their parent (present in the tree, but NOT individually
    tappable). Every token must look like a testID, so a LogBox sentence (full of
    lowercase words and punctuation) is not mistaken for one."""
    parts = value.split()
    return len(parts) >= 2 and all(_looks_like_testid(p) for p in parts)


def _label_of(el: ET.Element) -> Optional[str]:
    lbl = (el.get("label") or el.get("value") or el.get("text") or "").strip()
    return lbl or None


def _center(el: ET.Element):
    try:
        x, y = int(el.get("x", "")), int(el.get("y", ""))
        w, h = int(el.get("width", "")), int(el.get("height", ""))
        return x + w // 2, y + h // 2
    except (ValueError, TypeError):
        return None, None


def _suggest_testid(el_type: str, hint: Optional[str]) -> str:
    """Best-effort camelCase testID from a label/type. A SUGGESTION the team
    confirms — never authoritative, since an unlabelled control has no real name."""
    base = hint or el_type.replace("XCUIElementType", "").replace("android.widget.", "")
    words = re.findall(r"[A-Za-z0-9]+", base)
    if not words:
        return "elementButton"
    head = words[0].lower()
    camel = head + "".join(w.capitalize() for w in words[1:])
    kind = "Button" if "Button" in el_type else ""
    return camel if camel.endswith(kind) or not kind else camel + kind


def parse_accessibility_tree(xml: str) -> Inspection:
    """Pure parse: XML page_source -> catalog + gaps."""
    result = Inspection()
    root = ET.fromstring(xml)

    # Map every element to its parent so a gap can report its parent's id.
    parent_of: Dict[ET.Element, ET.Element] = {
        child: el for el in root.iter() for child in el
    }

    for el in root.iter():
        el_type = el.tag
        tid = _id_of(el)

        if tid:
            # Only atomic ids are real, targetable locators. Merged parent labels
            # and LogBox sentences carry an id string but can't be used as one.
            if is_atomic_testid(tid):
                result.catalog.append(
                    CatalogEntry(testid=tid, type=el_type, label=_label_of(el))
                )
            elif is_merged_parent(tid):
                # Children were folded into this parent — they exist in the React
                # tree but have no XCUIElement of their own, so they are detectable
                # yet NOT tappable. Record the parent so the report can flag them.
                for child_id in tid.split():
                    if is_atomic_testid(child_id):
                        result.merged.add(child_id)
            continue

        # No id. Only interactive elements count as gaps worth filing.
        if el_type not in _INTERACTIVE:
            continue

        label = _label_of(el)
        # A child label often carries the human meaning ("Filter" text inside a button).
        nearest = label or next(
            (_label_of(c) for c in el.iter() if _label_of(c)), None
        )
        parent = parent_of.get(el)
        x, y = _center(el)
        result.gaps.append(
            Gap(
                type=el_type,
                label=label,
                nearest_text=nearest,
                parent_id=_id_of(parent) if parent is not None else None,
                x=x, y=y,
                suggested_testid=_suggest_testid(el_type, nearest),
            )
        )
    return result


def format_gap_report(gaps: List[Gap]) -> str:
    """Render gaps as ready-to-file issues (the format the user asked for)."""
    if not gaps:
        return "No accessibility gaps found on this screen — every interactive element has an id."
    out: List[str] = []
    for g in gaps:
        name = g.nearest_text or f"Unlabelled {g.type.split('Type')[-1]}"
        loc = f" at ({g.x},{g.y})" if g.x is not None else ""
        out.append(
            f"Element: {name}{loc}\n"
            f"\nIssue Type:\n- Missing Accessibility Identifier\n"
            f"\nDescription:\n- Visible on screen but exposes no accessibility id "
            f"(testID/accessibilityIdentifier). Appium cannot reliably locate it.\n"
            f"\nParent Information:\n- Parent accessibility identifier: "
            f"{g.parent_id or 'Not Found'}\n"
            f"\nImpact:\n- Cannot be targeted reliably by automated tests.\n"
            f"- AI navigation falls back to XPath/coordinates, making tests fragile.\n"
            f"\nRecommendation:\n- Add:\n"
            f"    accessible={{true}}\n"
            f'    testID="{g.suggested_testid}"\n'
            + "-" * 60
        )
    return "\n".join(out)


def collect_from_driver(driver) -> Inspection:
    """Live wrapper: dump the current screen and parse it."""
    return parse_accessibility_tree(driver.page_source)


# ── runnable self-check (no device needed) ───────────────────────────────────
if __name__ == "__main__":
    SAMPLE = """
    <AppiumAUT><XCUIElementTypeApplication name="app">
      <XCUIElementTypeButton name="homeTab" label="Home" x="0" y="800" width="80" height="40"/>
      <XCUIElementTypeOther name="searchWrap" x="10" y="100" width="300" height="44">
        <XCUIElementTypeButton label="Filter" x="270" y="110" width="30" height="30"/>
        <XCUIElementTypeTextField x="20" y="110" width="240" height="30"/>
      </XCUIElementTypeOther>
      <XCUIElementTypeOther name="homeSearchBar homeFilter homeCouponIcon"/>
      <XCUIElementTypeOther name="Each child in a list should have a unique key prop"/>
      <XCUIElementTypeStaticText label="ignored, not interactive"/>
    </XCUIElementTypeApplication></AppiumAUT>
    """
    insp = parse_accessibility_tree(SAMPLE)
    ids = {c.testid for c in insp.catalog}
    assert "homeTab" in ids, ids
    assert "searchWrap" in ids, ids
    assert len(insp.gaps) == 2, [g.type for g in insp.gaps]  # button + textfield, not the static text
    filter_gap = next(g for g in insp.gaps if g.nearest_text == "Filter")
    assert filter_gap.parent_id == "searchWrap", filter_gap.parent_id
    assert filter_gap.suggested_testid == "filterButton", filter_gap.suggested_testid
    # folded children detected, LogBox sentence NOT mistaken for testIDs
    assert insp.merged == {"homeSearchBar", "homeFilter", "homeCouponIcon"}, insp.merged
    print(f"folded (detectable, not tappable): {sorted(insp.merged)}")
    print(f"catalog: {len(insp.catalog)} usable ids -> {sorted(ids)}")
    print(f"gaps:    {len(insp.gaps)}")
    print()
    print(format_gap_report(insp.gaps))
    print("\nself-check passed ✓")
