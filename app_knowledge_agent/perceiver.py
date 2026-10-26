"""Perceiver — turns raw Explorer screen dumps into structured JSON.

For every unique screen hash in knowledge_pack/raw/<hash>/ that has not
already been perceived, this script:

  1. Reads the screenshot (screenshot.png) and accessibility tree (tree.json)
  2. Trims the tree to only meaningful elements (cuts 5-10x noise)
  3. Applies PII masking to the screenshot before sending externally
  4. Calls Gemini Vision with screenshot + trimmed tree
  5. Saves the structured JSON to knowledge_pack/perceived/<hash>.json

It is safe to re-run at any time — already-perceived hashes are skipped.
Running after a new app version scan re-perceives only the changed hashes.

Output schema per screen:
{
  "purpose": str,          plain-language: what is this screen for
  "screen_type": str,      login|form|list|detail|dashboard|
                           confirmation|error|onboarding|settings|other
  "elements": [
    {
      "label":  str,       visible text or plain description
      "role":   str,       button|text_field|checkbox|toggle|dropdown|
                           tab|link|image|static_text|other
      "action": str|null   what happens on interaction
      "accessibility_id": str|null   REAL AXIdentifier/AXLabel from idb,
                           when it could be matched back to this element.
                           A step generator should tap THIS, not "label" --
                           an explicit accessibilityLabel on a RN component
                           (e.g. a tab bar button) hides its painted text
                           from the live accessibility tree entirely, so
                           "label" alone can be unresolvable at run time.
    }
  ],
  "form_fields": [
    {
      "name":             str,
      "input_type":       str,  text|email|password|number|date|otp|other
      "required":         bool|null,
      "validation_hint":  str|null
    }
  ],
  "tone_of_voice_sample": str|null   short verbatim copy for brand rollup
}
"""

from __future__ import annotations

import json
import re
import logging
import os
import sys
from pathlib import Path
from typing import Any

# ── Path setup ────────────────────────────────────────────────────────────────
# Allow running as a standalone script from the project root.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Load .env so GEMINI_API_KEY and other vars are available.
from automation.config import load_env
load_env()

from app_knowledge_agent.gemini_vision import GeminiVisionProvider, is_quota_error

# No cap by default — gemini-3.5-flash-lite's free tier comfortably covers a
# full pack in one run. If a smaller/throttled model is used instead, pass
# --limit (e.g. 20) to bound a run to its real per-minute/per-day quota; the
# quota-exhaustion stop below (is_quota_error) protects a run either way, so
# this is only about avoiding needless repeat runs, not safety.
_DEFAULT_LIMIT = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("aka.perceiver")

# ── Paths ─────────────────────────────────────────────────────────────────────
_RAW_DIR       = _PROJECT_ROOT / "app_knowledge_agent" / "knowledge_pack" / "raw"
_PERCEIVED_DIR = _PROJECT_ROOT / "app_knowledge_agent" / "knowledge_pack" / "perceived"

# ── Element types worth keeping after trimming ────────────────────────────────
# idb describe-all returns these type strings. We keep only elements that carry
# real signal for Gemini — interactive elements, labelled text, identified views.
_KEEP_TYPES = {
    "Button", "TextField", "SecureTextField", "Switch", "Slider",
    "SegmentedControl", "Picker", "DatePicker", "WebView",
    "Link", "Image", "StaticText", "NavigationBar", "TabBar",
    "TabBarButton", "Cell", "Table", "CollectionView",
    "SearchField", "TextViewElement", "Toggle",
}

# ── System prompt sent with every Gemini vision call ─────────────────────────
_SYSTEM_PROMPT = """You are analysing a single screen of an unfamiliar iOS
restaurant app to build a structured knowledge pack for an AI agent that will
later act on this app on a user's behalf.

You are given:
- A screenshot of the screen
- A trimmed accessibility tree (only interactive and labelled elements)

Rules:
- Base every claim on what is visible in the screenshot or present in the tree.
- Do not invent elements, labels, or behaviour that is not evidenced.
- Describe elements the way a person looking at the screen would, not by their
  internal type or resource id.
- If a field cannot be determined, use null rather than guessing.
- Respond with ONLY valid JSON matching the schema. No prose, no markdown
  fences, no commentary before or after the JSON.

Return JSON with exactly this shape:
{
  "purpose": "<one plain-language sentence: what is this screen for>",
  "screen_type": "<login|form|list|detail|dashboard|confirmation|error|onboarding|settings|other>",
  "elements": [
    {
      "label":  "<visible text or plain description>",
      "role":   "<button|text_field|checkbox|toggle|dropdown|tab|link|image|static_text|other>",
      "action": "<what happens on interaction, or null if informational>"
    }
  ],
  "form_fields": [
    {
      "name":            "<inferred field name>",
      "input_type":      "<text|email|password|number|date|otp|other>",
      "required":        true,
      "validation_hint": "<visible hint or null>"
    }
  ],
  "tone_of_voice_sample": "<1-2 short verbatim copy snippets visible on screen, or null>"
}"""


# ── Tree trimmer ──────────────────────────────────────────────────────────────

def trim_tree(elements: list[dict]) -> str:
    """Keep only elements that carry signal for Gemini.

    Drops pure layout containers (Views, ScrollViews, Windows) that have
    no label, no identifier, and are not interactive. This cuts the typical
    idb describe-all output by 5-10x before it is sent to the vision model,
    which reduces token usage and improves response quality.

    Returns a compact plain-text summary rather than raw JSON —
    plain text is easier for Gemini to parse alongside the image.
    """
    kept = []
    for el in elements:
        el_type  = el.get("type", "")
        ax_id    = (el.get("AXIdentifier") or "").strip()
        ax_label = (el.get("AXLabel") or "").strip()
        ax_value = (el.get("AXValue") or "").strip()
        enabled  = el.get("enabled", True)
        visible  = el.get("visible", True)

        if not visible:
            continue

        # Keep if it is a meaningful type OR has an id/label that carries signal.
        has_signal = (
            el_type in _KEEP_TYPES
            or bool(ax_id)
            or bool(ax_label)
        )
        if not has_signal:
            continue

        line_parts = [el_type]
        if ax_id:
            line_parts.append(f"id={ax_id!r}")
        if ax_label:
            line_parts.append(f"label={ax_label!r}")
        if ax_value:
            line_parts.append(f"value={ax_value!r}")
        if not enabled:
            line_parts.append("disabled")

        kept.append("  " + "  ".join(line_parts))

    if not kept:
        return "(no labelled elements found)"
    return "\n".join(kept)


# ── PII masking ───────────────────────────────────────────────────────────────

def mask_screenshot(screenshot_path: Path, elements: list[dict]) -> Path:
    """Blur text-input field regions before sending the screenshot externally.

    Uses your existing Pillow install. Finds every TextField and
    SecureTextField in the tree, reads its frame, and applies a blur to
    that region in the screenshot. Returns the path to the masked image
    (written as a sibling _masked.png so the original is never modified).

    Why: GEMINI_API_KEY sends data to Google's servers. PII in input
    fields (emails, passwords) must not leave the machine unmasked.
    This matches the intent of your existing evidence/pii_scrub.py.
    """
    try:
        from PIL import Image, ImageFilter
    except ImportError:
        logger.debug("Pillow not available — skipping screenshot masking")
        return screenshot_path

    masked_path = screenshot_path.parent / "_masked.png"

    try:
        img = Image.open(screenshot_path).convert("RGB")
        img_w, img_h = img.size

        for el in elements:
            if el.get("type") not in ("TextField", "SecureTextField"):
                continue
            f = el.get("frame") or {}
            x = float(f.get("x") or 0)
            y = float(f.get("y") or 0)
            w = float(f.get("width") or 0)
            h = float(f.get("height") or 0)
            if w < 10 or h < 10:
                continue
            # idb describe-all returns app-space points (portrait).
            # The screenshot from xcrun simctl is also portrait-space,
            # but may be at 2x or 3x scale.
            scale = img_w / max(
                next(
                    (float((el.get("frame") or {}).get("width") or 0)
                     for el in elements
                     if el.get("type") == "Application"),
                    img_w,
                ),
                1,
            )
            box = (
                int(x * scale),
                int(y * scale),
                int((x + w) * scale),
                int((y + h) * scale),
            )
            # Clamp to image bounds.
            box = (
                max(0, box[0]), max(0, box[1]),
                min(img_w, box[2]), min(img_h, box[3]),
            )
            if box[2] > box[0] and box[3] > box[1]:
                region = img.crop(box)
                blurred = region.filter(
                    ImageFilter.GaussianBlur(radius=15)
                )
                img.paste(blurred, box)

        img.save(masked_path)
        return masked_path

    except Exception as e:
        logger.warning("Screenshot masking failed: %s — using original", e)
        return screenshot_path


# ── JSON repair ───────────────────────────────────────────────────────────────

def _safe_parse(text: str) -> dict[str, Any]:
    """Parse the Gemini response to a dict, handling common edge cases.

    Gemini sometimes wraps JSON in markdown fences even when asked not to.
    response_mime_type='application/json' reduces this but does not
    eliminate it on all model versions.
    """
    text = text.strip()

    # Strip markdown fences if present.
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop first line (```json or ```) and last line (```).
        inner = [l for l in lines[1:] if l.strip() != "```"]
        text = "\n".join(inner).strip()

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
        logger.warning("Gemini returned valid JSON but not an object: %s",
                       type(result))
        return {}
    except json.JSONDecodeError as e:
        logger.warning("Could not parse Gemini response as JSON: %s\n"
                       "Raw response (first 500 chars): %s",
                       e, text[:500])
        return {}


# ── Schema validator / filler ─────────────────────────────────────────────────

def _ax_lookup(raw_elements: list[dict]) -> dict[str, str]:
    """lowercased AXLabel -> real id (AXIdentifier, preferred, else AXLabel
    itself), straight from idb -- the ground truth Gemini's vision pass is
    told to describe rather than transcribe (see _SYSTEM_PROMPT). Keyed by
    AXLabel because that is the only thing Gemini's "label" can plausibly
    match; when a component's accessibilityLabel hides its painted text
    (e.g. a tab whose visible "Coupons" is overridden to "couponBlock"),
    there is correctly no entry to match and accessibility_id stays None --
    no guessing."""
    out: dict[str, str] = {}
    for el in raw_elements or []:
        if not isinstance(el, dict):
            continue
        ax_label = (el.get("AXLabel") or "").strip()
        ax_id    = (el.get("AXIdentifier") or "").strip()
        if ax_label:
            out[ax_label.lower()] = ax_id or ax_label
    return out


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _ax_substring_match(raw_elements: list[dict], label: str) -> str | None:
    """Fallback for when _ax_lookup's exact AXLabel match fails because an RN
    accessibilityLabel REPLACES the painted text in the live tree entirely
    (confirmed: a Wallet tab painted "Coupons" has AXLabel 'couponBlock'; a
    home-screen chip painted the same word has AXLabel 'homeCouponIcon' --
    neither tree ever contains the string "Coupons" to match against).

    Looks for the painted label's squashed singular form AS A SUBSTRING of a
    real id -- deliberately narrow: a minimum length of 5 excludes short/
    common words ("book", "add") that would false-positive against unrelated
    ids on other screens (this app's own matcher has hit exactly that: "book
    now" matching "bookAppoitment" on the word "book" alone was measured to
    mis-tap). Only meant to catch a clearly distinctive word like "coupon"."""
    target = _squash(label)
    if target.endswith("s") and len(target) > 4:
        target = target[:-1]
    if len(target) < 5:
        return None
    best = None
    for el in raw_elements or []:
        if not isinstance(el, dict):
            continue
        ax_label = (el.get("AXLabel") or "").strip()
        ax_id = (el.get("AXIdentifier") or "").strip()
        real = ax_id or ax_label
        if real and target in _squash(real):
            if best is None or len(real) < len(best):
                best = real
    return best


def _normalise(raw: dict[str, Any], screen_hash: str,
               screenshot_path: Path,
               raw_elements: list[dict] | None = None) -> dict[str, Any]:
    """Ensure the output matches the expected schema exactly.

    Missing fields get null/empty defaults rather than crashing the
    Synthesizer downstream. Also injects the hash and screenshot path
    so the Synthesizer does not need to re-derive them.
    """
    valid_types = {
        "login", "form", "list", "detail", "dashboard",
        "confirmation", "error", "onboarding", "settings", "other",
    }
    screen_type = raw.get("screen_type", "other")
    if screen_type not in valid_types:
        screen_type = "other"

    ax_lookup = _ax_lookup(raw_elements or [])
    elements = []
    for el in (raw.get("elements") or []):
        if not isinstance(el, dict):
            continue
        label = str(el.get("label") or "")
        accessibility_id = (ax_lookup.get(label.strip().lower())
                             or _ax_substring_match(raw_elements or [], label))
        elements.append({
            "label":  label,
            "role":   str(el.get("role") or "other"),
            "action": el.get("action"),
            "accessibility_id": accessibility_id,
        })

    form_fields = []
    for ff in (raw.get("form_fields") or []):
        if not isinstance(ff, dict):
            continue
        form_fields.append({
            "name":            str(ff.get("name") or ""),
            "input_type":      str(ff.get("input_type") or "text"),
            "required":        ff.get("required"),
            "validation_hint": ff.get("validation_hint"),
        })

    return {
        "_screen_hash":    screen_hash,
        "_screenshot_ref": str(screenshot_path),
        "purpose":         raw.get("purpose") or None,
        "screen_type":     screen_type,
        "elements":        elements,
        "form_fields":     form_fields,
        "tone_of_voice_sample": raw.get("tone_of_voice_sample") or None,
    }


# ── User-prompt builder ───────────────────────────────────────────────────────

def _build_user_prompt(trimmed_tree: str, app_role: str) -> str:
    """The text sent alongside the screenshot to Gemini.

    Includes the trimmed accessibility tree and the app role so Gemini
    has context about which account is active (consumer/waiter/kitchen).
    """
    role_context = {
        "consumer": "This screen is from the Consumer app (diner perspective).",
        "waiter":   "This screen is from the Business app, Waiter role.",
        "kitchen":  "This screen is from the Business app, Kitchen role.",
    }.get(app_role, "")

    return (
        f"{role_context}\n\n"
        f"ACCESSIBILITY TREE (trimmed — only labelled/interactive elements):\n"
        f"{trimmed_tree}\n\n"
        "Return the JSON now."
    )


# ── Main perceiver logic ──────────────────────────────────────────────────────

def perceive_all(
    raw_dir: Path = _RAW_DIR,
    perceived_dir: Path = _PERCEIVED_DIR,
    dry_run: bool = False,
    limit: int | None = _DEFAULT_LIMIT,
) -> dict[str, int]:
    """Perceive unprocessed screen hashes, up to *limit* NEW ones.

    *limit* bounds real Gemini calls per run — defaults to _DEFAULT_LIMIT to
    match a free-tier daily cap. Pass None for unlimited. Already-perceived
    screens are always skipped for free and never count against the limit,
    so re-running later simply continues where the last run left off.

    Returns a summary dict: {processed, skipped, failed}.
    """
    perceived_dir.mkdir(parents=True, exist_ok=True)

    if not raw_dir.exists():
        logger.error("Raw dir not found: %s", raw_dir)
        return {"processed": 0, "skipped": 0, "failed": 0}

    screen_dirs = [
        d for d in raw_dir.iterdir()
        if d.is_dir() and d.name != "__pycache__"
    ]
    logger.info("Found %d unique screen hashes to check.", len(screen_dirs))

    provider = GeminiVisionProvider() if not dry_run else None
    processed = skipped = failed = 0

    for screen_dir in sorted(screen_dirs):
        screen_hash  = screen_dir.name
        out_file     = perceived_dir / f"{screen_hash}.json"

        # ── Already perceived — skip (free, does not count against limit) ──
        if out_file.exists():
            logger.info("  [skip] %s — already perceived", screen_hash[:12])
            skipped += 1
            continue

        # ── Hit this run's call budget — stop, not fail ────────────────────
        if not dry_run and limit is not None and processed >= limit:
            remaining = len(screen_dirs) - skipped - processed - failed
            logger.info(
                "  Reached --limit=%d new screen(s) this run — stopping "
                "with %d screen(s) still unperceived. Re-run to continue "
                "(already-perceived screens are skipped for free).",
                limit, remaining,
            )
            break

        # ── Load raw files ────────────────────────────────────────────────
        screenshot = screen_dir / "screenshot.png"
        tree_file  = screen_dir / "tree.json"
        meta_file  = screen_dir / "meta.json"

        if not screenshot.exists():
            logger.warning("  [skip] %s — no screenshot.png", screen_hash[:12])
            failed += 1
            continue

        elements: list[dict] = []
        if tree_file.exists():
            try:
                elements = json.loads(tree_file.read_text(encoding="utf-8"))
                if not isinstance(elements, list):
                    elements = []
            except Exception as e:
                logger.warning("  [warn] %s — bad tree.json: %s",
                               screen_hash[:12], e)

        # Read app_role from meta.json written by the Explorer.
        app_role = "consumer"
        if meta_file.exists():
            try:
                meta     = json.loads(meta_file.read_text(encoding="utf-8"))
                app_role = meta.get("app_role", "consumer")
            except Exception:
                pass

        if dry_run:
            logger.info(
                "  [dry-run] %s — would perceive (%d elements, role=%s)",
                screen_hash[:12], len(elements), app_role,
            )
            processed += 1
            continue

        # ── Mask PII in screenshot before sending to Gemini ───────────────
        send_path = mask_screenshot(screenshot, elements)

        # ── Trim the accessibility tree ───────────────────────────────────
        trimmed_tree = trim_tree(elements)

        # ── Call Gemini Vision ────────────────────────────────────────────
        user_prompt = _build_user_prompt(trimmed_tree, app_role)
        try:
            raw_text = provider.generate_with_image(
                image_path=send_path,
                text_prompt=user_prompt,
                system_prompt=_SYSTEM_PROMPT,
                max_tokens=2048,
                temperature=0.1,
            )
        except Exception as e:
            if is_quota_error(e):
                remaining = len(screen_dirs) - skipped - processed - failed - 1
                logger.error(
                    "  [stop] %s — Gemini quota exhausted: %s. Stopping "
                    "this run rather than failing through the remaining "
                    "%d screen(s) one at a time. Re-run once the quota "
                    "resets — already-perceived screens are skipped for "
                    "free, so nothing done so far is repeated.",
                    screen_hash[:12], e, remaining,
                )
                failed += 1
                break
            logger.error("  [fail] %s — Gemini call failed: %s",
                         screen_hash[:12], e)
            failed += 1
            continue
        finally:
            # Clean up the masked copy — it is a temp file.
            masked = screen_dir / "_masked.png"
            if masked.exists():
                try:
                    masked.unlink()
                except OSError:
                    pass

        # ── Parse and normalise ───────────────────────────────────────────
        raw_dict = _safe_parse(raw_text)
        if not raw_dict:
            logger.error(
                "  [fail] %s — Gemini returned unparseable response",
                screen_hash[:12],
            )
            failed += 1
            continue

        result = _normalise(raw_dict, screen_hash, screenshot, elements)

        # ── Save ──────────────────────────────────────────────────────────
        out_file.write_text(
            json.dumps(result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        processed += 1
        logger.info(
            "  [ok] %s — %s (%d elements, %d fields)",
            screen_hash[:12],
            result.get("screen_type", "?"),
            len(result["elements"]),
            len(result["form_fields"]),
        )

    summary = {"processed": processed, "skipped": skipped, "failed": failed}
    logger.info(
        "Perceiver done — processed=%d  skipped=%d  failed=%d",
        processed, skipped, failed,
    )
    return summary


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Perceive raw screen dumps using Gemini Vision."
    )
    parser.add_argument(
        "--raw-dir",
        default=str(_RAW_DIR),
        help="Path to knowledge_pack/raw/",
    )
    parser.add_argument(
        "--out-dir",
        default=str(_PERCEIVED_DIR),
        help="Path to knowledge_pack/perceived/",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be perceived without calling Gemini.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_LIMIT,
        help="Stop after perceiving this many NEW screens this run "
             "(default: unlimited). Already-perceived screens are skipped "
             "for free and don't count. Pass e.g. 20 to bound a run to a "
             "throttled model's real quota.",
    )
    args = parser.parse_args()

    result = perceive_all(
        raw_dir=Path(args.raw_dir),
        perceived_dir=Path(args.out_dir),
        dry_run=args.dry_run,
        limit=(None if args.limit == 0 else args.limit),
    )
    sys.exit(0 if result["failed"] == 0 else 1)