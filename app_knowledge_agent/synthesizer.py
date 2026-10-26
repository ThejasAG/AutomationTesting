"""Synthesizer — merges perceived screens into the App Knowledge Pack.

Reads:
  knowledge_pack/perceived/<hash>.json   (one per unique screen)
  knowledge_pack/raw/transitions.jsonl   (one line per tap, from Explorer)
  knowledge_pack/raw/<hash>/meta.json    (app_role per screen)
  knowledge_pack/manual_flows.json       (human-described flows the Explorer
                                           cannot walk live -- see below)

Writes:
  knowledge_pack/app_knowledge_pack.json   (the full pack, current version)
  knowledge_pack/app_knowledge_pack_<timestamp>.json  (archived version)

The pack schema:
{
  "version":      ISO timestamp string,
  "app_name":     str,
  "generated_at": ISO datetime,
  "screens": [
    {
      "id":             screen hash,
      "purpose":        plain-language summary,
      "screen_type":    login|form|list|...,
      "app_role":       consumer|waiter|kitchen,
      "elements":       [{label, role, action}],
      "form_fields":    [{name, input_type, required, validation_hint}],
      "screenshot_ref": file path (never inlined bytes)
    }
  ],
  "journeys": [
    {
      "name":  str,
      "role":  consumer|waiter|kitchen,
      "steps": [hash, hash, ...],
      "via":   [element_label, ...]   (one fewer than steps)
    }
  ],
  "design": {
    "colors":                 [hex strings],
    "components":             [distinct element roles seen across all screens],
    "tone_of_voice_samples":  [str],
    "supports_dark_mode":     bool|null
  },
  "coverage": {
    "total_screens":          int,
    "screens_by_role":        {consumer: int, waiter: int, kitchen: int},
    "screens_by_type":        {login: int, form: int, ...},
    "unreachable_screens":    []   (populated by Explorer when it skips)
  },
  "diff": null | {
    "previous_version":  str,
    "new_screens":       [hash],
    "removed_screens":   [hash],
    "changed_screens":   [hash],
    "unchanged_screens": [hash]
  },
  "manual_journeys": [
    {
      "name":   str,
      "role":   consumer|waiter|kitchen,
      "steps":  [plain-language step, ...],
      "source": "manual"
    }
  ]
}

Why a separate manual_journeys, not more journeys[]?
journeys[] steps are screen HASHES the Explorer actually visited -- the
Scenario Generator can look each one up in screens[]. Some flows (placing a
real preorder, paying, sending an order to the kitchen) can never be walked
live: the Explorer's safety rules forbid completing a payment or sending an
order to the kitchen. manual_journeys carries what a human describes
instead, as plain steps with no screen hash to look up, tagged
source: "manual" so the Scenario Generator knows to treat anything built
from it as MANUAL_ONLY rather than something it could try to replay.

Why one file instead of many small files?
The Scenario Generator sends the full pack to Gemini in one call so it
can reason about the whole app at once. Keeping it in one file makes
that call simple and keeps the pack self-contained for the viewer.
Screenshots are file paths, never inlined bytes, so the file stays
compact regardless of how many screens were found.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── Path setup ─────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from automation.config import load_env
load_env()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("aka.synthesizer")

# ── Paths ──────────────────────────────────────────────────────────────────────
_AKA_DIR       = _PROJECT_ROOT / "app_knowledge_agent"
_RAW_DIR       = _AKA_DIR / "knowledge_pack" / "raw"
_PERCEIVED_DIR = _AKA_DIR / "knowledge_pack" / "perceived"
_PACK_PATH     = _AKA_DIR / "knowledge_pack" / "app_knowledge_pack.json"
_MANUAL_FLOWS_PATH = _AKA_DIR / "knowledge_pack" / "manual_flows.json"

# ── Constants ──────────────────────────────────────────────────────────────────
_ROLES          = ("consumer", "waiter", "kitchen")
_MAX_JOURNEY_DEPTH  = 8
_MAX_JOURNEYS_PER_ROLE = 5
_COLOR_SAMPLE_SCREENS  = 12
_COLOR_CLUSTERS        = 6

# A real screen hash is always 32 lowercase hex chars (sha256, truncated —
# see explorer.py's _nav_hash). The Explorer also logs a handful of
# transitions through pseudo-nodes that are NOT screens -- "login" (the
# login screen itself isn't hashed), "unreadable_modal" and "unknown" (relaunch-
# recovery bookkeeping). None of those should ever become a journey step: the
# Scenario Generator expects every journeys[].steps entry to be a real
# screens[].id it can look up.
_HASH_RE = re.compile(r"^[0-9a-f]{32}$")


# ══════════════════════════════════════════════════════════════════════════════
# 1. SCREEN LOADER
# ══════════════════════════════════════════════════════════════════════════════

def load_screens(
    perceived_dir: Path,
    raw_dir: Path,
) -> list[dict[str, Any]]:
    """Load all perceived screen JSONs and enrich with app_role from meta.json.

    Each returned dict matches the pack's screens[] schema exactly.
    Screens whose perceived JSON is missing or malformed are skipped
    with a warning rather than crashing the whole synthesis.
    """
    screens = []

    for f in sorted(perceived_dir.glob("*.json")):
        screen_hash = f.stem
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("  [skip] %s — bad perceived JSON: %s",
                           screen_hash[:12], e)
            continue

        # app_role comes from the Explorer's meta.json for this hash.
        app_role = data.get("_app_role", "consumer")
        meta_file = raw_dir / screen_hash / "meta.json"
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                app_role = meta.get("app_role", app_role)
            except Exception:
                pass

        screenshot_ref = data.get("_screenshot_ref") or str(
            raw_dir / screen_hash / "screenshot.png"
        )

        screens.append({
            "id":             screen_hash,
            "purpose":        data.get("purpose"),
            "screen_type":    data.get("screen_type", "other"),
            "app_role":       app_role,
            "elements":       data.get("elements") or [],
            "form_fields":    data.get("form_fields") or [],
            "screenshot_ref": screenshot_ref,
            "tone_of_voice_sample": data.get("tone_of_voice_sample"),
        })

    logger.info("Loaded %d perceived screens.", len(screens))
    return screens


# ══════════════════════════════════════════════════════════════════════════════
# 2. TRANSITION LOADER
# ══════════════════════════════════════════════════════════════════════════════

def load_transitions(raw_dir: Path) -> list[dict[str, Any]]:
    """Read transitions.jsonl written by the Explorer.

    Each line is:
      {"from_hash": str, "element_label": str, "to_hash": str,
       "app_role": str, "action_type": str}

    BACK transitions are kept but marked — the journey builder uses
    forward taps only for constructing named paths.
    """
    trans_file = raw_dir / "transitions.jsonl"
    if not trans_file.exists():
        logger.warning(
            "transitions.jsonl not found at %s — journeys will be empty.",
            trans_file,
        )
        return []

    transitions = []
    for line_no, line in enumerate(
        trans_file.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = line.strip()
        if not line:
            continue
        try:
            t = json.loads(line)
            transitions.append(t)
        except json.JSONDecodeError as e:
            logger.warning(
                "  transitions.jsonl line %d skipped: %s", line_no, e
            )

    logger.info("Loaded %d transitions.", len(transitions))
    return transitions


# ══════════════════════════════════════════════════════════════════════════════
# 3. JOURNEY BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def build_journeys(
    transitions: list[dict[str, Any]],
    screens: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reconstruct named journeys from the transition graph.

    Strategy:
      - Build a directed graph: from_hash -> [(element_label, to_hash, role)]
      - Find entry points per role: screens with no incoming forward edges
        in that role's subgraph
      - Walk forward from each entry point up to MAX_JOURNEY_DEPTH steps
      - Name each journey from the entry screen's purpose

    Why per role?
    The Consumer and Business (Waiter/Kitchen) apps are separate binaries.
    A Consumer journey that accidentally includes a Waiter screen would
    confuse the Scenario Generator. Keeping them separate also matches
    how the platform's SavedScenario model works — one role per scenario.
    """
    if not transitions:
        return []

    screen_lookup = {s["id"]: s for s in screens}

    # Build per-role forward edge maps.
    # forward[role][from_hash] = [(element_label, to_hash)]
    forward: dict[str, dict[str, list]] = {
        role: defaultdict(list) for role in _ROLES
    }
    has_incoming: dict[str, set] = {role: set() for role in _ROLES}

    for t in transitions:
        if t.get("action_type") == "BACK":
            continue    # back gestures do not form journey steps
        role     = t.get("app_role", "consumer")
        from_h   = t.get("from_hash", "")
        to_h     = t.get("to_hash", "")
        label    = t.get("element_label", "")
        if not from_h or not to_h or role not in _ROLES:
            continue
        if not _HASH_RE.match(from_h) or not _HASH_RE.match(to_h):
            continue    # pseudo-node ("login", "unreadable_modal", "unknown"),
                        # not a real screen -- see _HASH_RE's comment above
        forward[role][from_h].append((label, to_h))
        has_incoming[role].add(to_h)

    journeys = []

    for role in _ROLES:
        role_forward = forward[role]
        if not role_forward:
            continue

        # Entry points: reachable screens that no forward tap leads TO.
        # Sorted -- iterating a set() directly is order-randomized per
        # Python process, which silently picked a DIFFERENT 5 journeys out
        # of the same transitions.jsonl on every run.
        all_sources = sorted(role_forward.keys())
        entry_points = [
            h for h in all_sources
            if h not in has_incoming[role]
        ]
        # Fallback: if everything has an incoming edge (a loop),
        # pick the login screen or the first discovered screen.
        if not entry_points:
            login = next(
                (h for h in all_sources
                 if screen_lookup.get(h, {}).get("screen_type") == "login"),
                None,
            )
            entry_points = [login] if login else [next(iter(all_sources))]

        count = 0
        for start in entry_points:
            if count >= _MAX_JOURNEYS_PER_ROLE:
                break

            steps  = [start]
            via    = []
            current = start
            visited = {start}

            for _ in range(_MAX_JOURNEY_DEPTH):
                options = role_forward.get(current)
                if not options:
                    break
                # Pick the first-discovered edge (the Explorer's BFS order).
                label, nxt = options[0]
                if nxt in visited:
                    break   # avoid cycles
                via.append(label)
                steps.append(nxt)
                visited.add(nxt)
                current = nxt

            if len(steps) < 2:
                continue    # single-screen "journeys" are not useful

            entry_purpose = (
                screen_lookup.get(start, {}).get("purpose")
                or start[:12]
            )
            journeys.append({
                "name":  f"{role.capitalize()}: {entry_purpose}",
                "role":  role,
                "steps": steps,
                "via":   via,
            })
            count += 1

    logger.info("Built %d journeys across %d roles.", len(journeys), len(_ROLES))
    return journeys


# ══════════════════════════════════════════════════════════════════════════════
# 3b. MANUAL JOURNEYS — human-described flows the Explorer cannot walk live
# ══════════════════════════════════════════════════════════════════════════════

def load_manual_flows(path: Path) -> list[dict[str, Any]]:
    """Load knowledge_pack/manual_flows.json, or [] if it doesn't exist.

    The file is optional -- most runs of the Synthesizer won't have one yet
    -- so a missing file is normal, not an error. A malformed one is logged
    and skipped rather than crashing the whole synthesis, same as a bad
    perceived screen JSON.
    """
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Could not read %s — skipping manual flows: %s", path, e)
        return []

    flows = []
    for f in data.get("flows", []):
        role  = f.get("role")
        steps = f.get("steps") or []
        if role not in _ROLES or not steps:
            logger.warning(
                "  [skip] manual flow %r — missing/invalid role or steps.",
                f.get("name", "<unnamed>"),
            )
            continue
        flows.append({
            "name":   f.get("name") or f"{role.capitalize()}: manual flow",
            "role":   role,
            "steps":  steps,
            "source": "manual",
        })

    logger.info("Loaded %d manual flow(s) from %s.", len(flows), path.name)
    return flows


# ══════════════════════════════════════════════════════════════════════════════
# 4. DESIGN ROLLUP
# ══════════════════════════════════════════════════════════════════════════════

def _sample_screenshots(
    screens: list[dict[str, Any]],
    limit: int,
) -> list[Path]:
    """Pick up to `limit` screenshot paths that actually exist on disk."""
    paths = []
    for s in screens:
        ref = s.get("screenshot_ref")
        if not ref:
            continue
        p = Path(ref)
        if p.exists():
            paths.append(p)
        if len(paths) >= limit:
            break
    return paths


def _extract_colors(screenshots: list[Path]) -> list[str]:
    """K-means color extraction across a sample of screenshots.

    Uses scikit-learn KMeans. Returns up to _COLOR_CLUSTERS hex strings
    representing the dominant colors across the app's screens.
    If scikit-learn or Pillow is not available, returns an empty list
    rather than crashing — colors are a nice-to-have, not critical.
    """
    if not screenshots:
        return []
    try:
        import numpy as np
        from PIL import Image
        from sklearn.cluster import KMeans
    except ImportError as e:
        logger.warning(
            "Color extraction skipped — missing package: %s", e
        )
        return []

    pixels: list = []
    for path in screenshots:
        try:
            img = (
                Image.open(path)
                .convert("RGB")
                .resize((60, 60))   # fast downsample
            )
            pixels.extend(list(img.getdata()))
        except Exception as e:
            logger.debug("Could not open screenshot for color extraction: %s", e)

    if len(pixels) < _COLOR_CLUSTERS:
        return []

    try:
        arr = np.array(pixels, dtype=np.float32)
        km  = KMeans(
            n_clusters=_COLOR_CLUSTERS,
            n_init=4,
            random_state=0,
        ).fit(arr)
        hex_colors = []
        for center in km.cluster_centers_:
            r, g, b = (max(0, min(255, round(float(c)))) for c in center)
            hex_colors.append(f"#{r:02x}{g:02x}{b:02x}")
        return hex_colors
    except Exception as e:
        logger.warning("KMeans color clustering failed: %s", e)
        return []


def build_design(
    screens: list[dict[str, Any]],
    raw_dir: Path,
) -> dict[str, Any]:
    """Roll up design tokens across all screens.

    Colors    — k-means on a sample of screenshots
    Components — distinct element roles seen across every screen
    Tone      — tone_of_voice_sample strings collected per screen
    Dark mode — not inferrable from idb tree alone; left null
    """
    # Collect tone samples from all screens.
    tone_samples = [
        s["tone_of_voice_sample"]
        for s in screens
        if s.get("tone_of_voice_sample")
    ]

    # Collect all distinct component roles.
    component_roles: set[str] = set()
    for s in screens:
        for el in (s.get("elements") or []):
            role = el.get("role")
            if role:
                component_roles.add(role)

    screenshots = _sample_screenshots(screens, _COLOR_SAMPLE_SCREENS)
    colors      = _extract_colors(screenshots)

    logger.info(
        "Design rollup: %d colors, %d component roles, %d tone samples.",
        len(colors), len(component_roles), len(tone_samples),
    )

    return {
        "colors":                colors,
        "components":            sorted(component_roles),
        "tone_of_voice_samples": tone_samples[:20],
        "supports_dark_mode":    None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 5. COVERAGE SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

def build_coverage(screens: list[dict[str, Any]]) -> dict[str, Any]:
    """Count screens by role and type for the coverage report.

    This surfaces coverage gaps immediately — roles with few screens
    signal that the Explorer did not fully explore that account.
    """
    by_role: dict[str, int] = defaultdict(int)
    by_type: dict[str, int] = defaultdict(int)

    for s in screens:
        by_role[s.get("app_role", "unknown")] += 1
        by_type[s.get("screen_type", "other")] += 1

    return {
        "total_screens":       len(screens),
        "screens_by_role":     dict(by_role),
        "screens_by_type":     dict(by_type),
        "unreachable_screens": [],  # populated by Explorer in a future pass
    }


# ══════════════════════════════════════════════════════════════════════════════
# 6. DIFF — compare against previous pack
# ══════════════════════════════════════════════════════════════════════════════

def diff_against_previous(
    new_screens: list[dict[str, Any]],
    pack_path: Path,
) -> Optional[dict[str, Any]]:
    """Compare new screen hashes against the previous pack version.

    Returns a diff dict, or None if no previous pack exists.
    The diff is stored inside the new pack so the version-change handler
    can read it without re-computing it, and the dashboard can display it.
    """
    if not pack_path.exists():
        logger.info("No previous pack found — this is the first scan.")
        return None

    try:
        old_pack   = json.loads(pack_path.read_text(encoding="utf-8"))
        old_version = old_pack.get("version", "unknown")
        old_hashes  = {s["id"] for s in old_pack.get("screens", [])}
    except Exception as e:
        logger.warning("Could not read previous pack for diff: %s", e)
        return None

    new_hashes = {s["id"] for s in new_screens}

    new_screens_list       = sorted(new_hashes - old_hashes)
    removed_screens_list   = sorted(old_hashes - new_hashes)
    unchanged_screens_list = sorted(old_hashes & new_hashes)
    # Changed = present in both but purpose or elements differ.
    # We compare by re-reading the perceived JSON for matching hashes.
    changed_screens_list: list[str] = []
    for h in unchanged_screens_list:
        old_screen = next(
            (s for s in old_pack.get("screens", []) if s["id"] == h), {}
        )
        new_screen = next(
            (s for s in new_screens if s["id"] == h), {}
        )
        if (old_screen.get("purpose") != new_screen.get("purpose")
                or old_screen.get("screen_type") != new_screen.get("screen_type")
                or len(old_screen.get("elements", []))
                   != len(new_screen.get("elements", []))):
            changed_screens_list.append(h)

    truly_unchanged = [
        h for h in unchanged_screens_list
        if h not in changed_screens_list
    ]

    diff = {
        "previous_version":  old_version,
        "new_screens":       new_screens_list,
        "removed_screens":   removed_screens_list,
        "changed_screens":   changed_screens_list,
        "unchanged_screens": truly_unchanged,
    }

    logger.info(
        "Diff vs %s: +%d new  -%d removed  ~%d changed  =%d unchanged",
        old_version,
        len(new_screens_list),
        len(removed_screens_list),
        len(changed_screens_list),
        len(truly_unchanged),
    )
    return diff


# ══════════════════════════════════════════════════════════════════════════════
# 7. ARCHIVE previous pack
# ══════════════════════════════════════════════════════════════════════════════

def _archive_previous(pack_path: Path) -> None:
    """Keep the previous pack as a timestamped backup.

    This lets the version-change handler roll back if needed and lets
    the dashboard show pack history. Archives are kept in the same
    directory as the current pack.
    """
    if not pack_path.exists():
        return
    try:
        old    = json.loads(pack_path.read_text(encoding="utf-8"))
        old_v  = old.get("version", "unknown").replace(":", "-").replace(" ", "_")
        archive_path = pack_path.parent / f"app_knowledge_pack_{old_v}.json"
        archive_path.write_text(
            pack_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        logger.info("Archived previous pack to %s", archive_path.name)
    except Exception as e:
        logger.warning("Could not archive previous pack: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# 8. MAIN SYNTHESIZE FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def synthesize(
    raw_dir: Path           = _RAW_DIR,
    perceived_dir: Path     = _PERCEIVED_DIR,
    pack_path: Path         = _PACK_PATH,
    manual_flows_path: Path = _MANUAL_FLOWS_PATH,
    app_name: str           = "Vya",
    dry_run: bool           = False,
) -> dict[str, Any]:
    """Build the App Knowledge Pack from perceived screens + transitions.

    Returns the full pack dict (also written to pack_path unless dry_run).
    """
    # ── Load inputs ───────────────────────────────────────────────────────────
    screens     = load_screens(perceived_dir, raw_dir)
    transitions = load_transitions(raw_dir)

    if not screens:
        logger.error(
            "No perceived screens found in %s. "
            "Run the Explorer then the Perceiver first.",
            perceived_dir,
        )
        return {}

    # ── Build sections ────────────────────────────────────────────────────────
    journeys        = build_journeys(transitions, screens)
    manual_journeys = load_manual_flows(manual_flows_path)
    design          = build_design(screens, raw_dir)
    coverage        = build_coverage(screens)

    # ── Diff against the previous pack ───────────────────────────────────────
    diff = diff_against_previous(screens, pack_path)

    # ── Assemble the pack ─────────────────────────────────────────────────────
    version = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Strip internal fields (prefixed with _) before writing.
    clean_screens = []
    for s in screens:
        clean_screens.append({
            k: v for k, v in s.items()
            if not k.startswith("_") and k != "tone_of_voice_sample"
        })

    pack: dict[str, Any] = {
        "version":         version,
        "app_name":        app_name,
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "screens":         clean_screens,
        "journeys":        journeys,
        "manual_journeys": manual_journeys,
        "design":          design,
        "coverage":        coverage,
        "diff":            diff,
    }

    if dry_run:
        logger.info(
            "[dry-run] Pack would contain: %d screens, %d journeys, "
            "%d manual journeys, %d colors, %d tone samples.",
            len(clean_screens),
            len(journeys),
            len(manual_journeys),
            len(design["colors"]),
            len(design["tone_of_voice_samples"]),
        )
        return pack

    # ── Archive previous pack before overwriting ──────────────────────────────
    _archive_previous(pack_path)

    # ── Write the new pack ────────────────────────────────────────────────────
    pack_path.parent.mkdir(parents=True, exist_ok=True)
    pack_path.write_text(
        json.dumps(pack, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    size_kb = pack_path.stat().st_size / 1024
    logger.info(
        "Pack written to %s (%.1f KB) — %d screens, %d journeys, %d manual journeys.",
        pack_path, size_kb, len(clean_screens), len(journeys), len(manual_journeys),
    )
    return pack


# ══════════════════════════════════════════════════════════════════════════════
# 9. CLI ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Synthesize the App Knowledge Pack from perceived screens."
    )
    parser.add_argument(
        "--raw-dir",
        default=str(_RAW_DIR),
        help="Path to knowledge_pack/raw/",
    )
    parser.add_argument(
        "--perceived-dir",
        default=str(_PERCEIVED_DIR),
        help="Path to knowledge_pack/perceived/",
    )
    parser.add_argument(
        "--out",
        default=str(_PACK_PATH),
        help="Output path for app_knowledge_pack.json",
    )
    parser.add_argument(
        "--manual-flows",
        default=str(_MANUAL_FLOWS_PATH),
        help="Path to manual_flows.json (human-described flows, e.g. "
             "payment/order-placement, the Explorer cannot walk live).",
    )
    parser.add_argument(
        "--app-name",
        default="Vya",
        help="Name of the app under test.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what the pack would contain without writing it.",
    )
    args = parser.parse_args()

    result = synthesize(
        raw_dir           = Path(args.raw_dir),
        perceived_dir     = Path(args.perceived_dir),
        pack_path         = Path(args.out),
        manual_flows_path = Path(args.manual_flows),
        app_name          = args.app_name,
        dry_run           = args.dry_run,
    )
    sys.exit(0 if result else 1)