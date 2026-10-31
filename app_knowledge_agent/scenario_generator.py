"""Scenario Generator — builds large cross-app end-to-end scenarios.

Reads:
  knowledge_pack/app_knowledge_pack.json   (screens, journeys, manual_journeys)
  e2e_spec.txt                             (the client's phase-structured spec)
  automation.scenarios.cross_app_flows.FLOWS  (only to avoid id collisions)

Writes exactly one CrossAppFlowEdit row (id ai_e2e_<slug>) whose segments
follow the spec phase by phase: Consumer pre-order -> Business (waiter) table
assignment/modify/send -> Kitchen -> comp -> more consumer orders -> payment
-> invoices -> review. "Business App" in the spec == the "waiter" role.

Running it WILL complete real payments / bookings on the staging backend.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# ── Path setup ─────────────────────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from automation.config import load_env
load_env()

from automation.database.config import SessionLocal
from automation.database.models import CrossAppFlowEdit
from automation.scenarios.cross_app_flows import FLOWS
from app_knowledge_agent.gemini_vision import GeminiVisionProvider, is_quota_error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("aka.scenario_generator")

# ── Paths ──────────────────────────────────────────────────────────────────────
_AKA_DIR   = _PROJECT_ROOT / "app_knowledge_agent"
_PACK_PATH = _AKA_DIR / "knowledge_pack" / "app_knowledge_pack.json"

# ── Constants ──────────────────────────────────────────────────────────────────
_ROLES = ("consumer", "waiter", "kitchen")

_MAX_OUTPUT_TOKENS = 16384

# Default location of the client's phase-structured spec (override with --spec).
_E2E_SPEC_PATH = _AKA_DIR / "e2e_spec.txt"

_STEP_VOCAB = """Each step must be one plain-language sentence using ONLY this vocabulary:
  - tap / click / press / touch / open / select / choose / pick / book / place / add / reserve / confirm / submit / proceed / continue <label>
  - type / enter / input / fill / search <field> with <value>
    - swipe / scroll <direction>
    - drag <control> to <position or value>
  - wait <duration or condition>
  - verify / check / confirm / ensure <label> is visible   (use for assertions)
  - go back
Refer to on-screen elements by the exact label/purpose text given to you below -- never invent a label that was not given."""



# ══════════════════════════════════════════════════════════════════════════════
# 1. PACK LOADING / TRIMMING
# ══════════════════════════════════════════════════════════════════════════════

def load_pack(pack_path: Path) -> dict[str, Any]:
    if not pack_path.exists():
        raise FileNotFoundError(
            f"No pack at {pack_path} — run the Synthesizer first."
        )
    return json.loads(pack_path.read_text(encoding="utf-8"))


def _trim_screen(screen: dict[str, Any]) -> dict[str, Any]:
    return {
        "id":          screen.get("id"),
        "purpose":     screen.get("purpose"),
        "screen_type": screen.get("screen_type"),
        "elements": [
            {"label": e.get("label"), "role": e.get("role"), "action": e.get("action"),
             "accessibility_id": e.get("accessibility_id")}
            for e in (screen.get("elements") or [])
        ],
        "form_fields": [
            {"name": f.get("name"), "input_type": f.get("input_type"),
             "required": f.get("required")}
            for f in (screen.get("form_fields") or [])
        ],
    }


def _journey_with_purposes(journey: dict[str, Any],
                            screen_lookup: dict[str, dict]) -> dict[str, Any]:
    return {
        "name": journey["name"],
        "path": [
            screen_lookup.get(h, {}).get("purpose") or h[:12]
            for h in journey["steps"]
        ],
        "via": journey.get("via", []),
    }


def build_role_data(role: str, pack: dict[str, Any]) -> Optional[dict[str, Any]]:
    screens = [s for s in pack.get("screens", []) if s.get("app_role") == role]
    if not screens:
        return None
    screen_lookup = {s["id"]: s for s in pack.get("screens", [])}

    journeys = [
        _journey_with_purposes(j, screen_lookup)
        for j in pack.get("journeys", [])
        if j.get("role") == role
    ]
    manual_journeys = [
        {"name": j["name"], "steps": j["steps"]}
        for j in pack.get("manual_journeys", [])
        if j.get("role") == role
    ]

    return {
        "role":            role,
        "screens":         [_trim_screen(s) for s in screens],
        "journeys":        journeys,
        "manual_journeys": manual_journeys,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 2. HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "flow"


_STOPWORDS = {
    "the","a","an","and","or","of","to","in","on","for","is","are","with",
    "this","that","these","those","it","as","by","at","be","will","should",
    "use","using","all","each","per","from","not","if","or","then","also",
}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower())
            if len(w) > 2 and w not in _STOPWORDS}


_MAX_SCREENS_PER_PHASE = 15   # consumer (134 screens) and waiter (125) dominate
                              # token cost when their FULL screen set is resent
                              # on every phase that touches them; most phases
                              # only need a fraction of it. Small roles (e.g.
                              # kitchen's 9) are left untouched -- see below.
                              # Multi-role phases divide this further (see
                              # _generate_e2e_phase) since 3 roles x 25 screens
                              # was the single biggest quota driver (measured
                              # ~97K input tokens for one phase alone).


def _relevant_screens(screens: list[dict[str, Any]], phase_text: str,
                       cap: int = _MAX_SCREENS_PER_PHASE) -> list[dict[str, Any]]:
    """Trim a role's screen list to the ones this phase's spec text is
    actually likely to need, instead of resending the role's ENTIRE screen
    set on every phase that touches it (confirmed the main driver of this
    generator's Gemini token usage: most of the ~12 phases touch 'waiter',
    so its full 69-screen set -- every element, every form field -- was
    being sent on ~8 separate calls, and consumer's 134-screen set likewise).

    Scored by word overlap between the phase text and each screen's purpose +
    element labels; always includes the top `cap` by score. If the role has
    fewer than `cap` screens to begin with, nothing is filtered -- there is
    nothing to save and no reason to risk losing grounding."""
    if len(screens) <= cap:
        return screens
    phase_words = _tokens(phase_text)
    if not phase_words:
        return screens[:cap]

    def score(s: dict[str, Any]) -> int:
        words = _tokens(s.get("purpose") or "")
        for e in (s.get("elements") or []):
            words |= _tokens(e.get("label") or "")
        return len(words & phase_words)

    ranked = sorted(screens, key=score, reverse=True)
    return ranked[:cap]


# ══════════════════════════════════════════════════════════════════════════════
# 3. E2E LIFECYCLE FLOW — one long flow generated phase-by-phase from a spec
# ══════════════════════════════════════════════════════════════════════════════
#
# Why phase-by-phase instead of one call: the spec is ~12 phases, each with
# many distinct operations (assign / split / void / comp / VAT mixes ...). One
# call would compress them into a handful of vague steps. One call per phase
# keeps every operation explicit, and a running "state ledger" is carried
# forward so later phases refer to the same guests/items/table the earlier
# phases created (e.g. Phase 6 comps an item Phase 2 split).

# "Business App" in the spec == the waiter role in this pipeline.
_APP_TAG_TO_ROLE = {
    "consumer app": "consumer",
    "business app": "waiter",
    "kitchen app":  "kitchen",
}

_PHASE_RE     = re.compile(r"^[ \t]*Phase[ \t]+(\d+)[ \t]*[–—-][ \t]*(.+?)[ \t]*$",
                           re.IGNORECASE | re.MULTILINE)
_SETUP_RE     = re.compile(r"^[ \t]*Test Setup[ \t]*$", re.IGNORECASE | re.MULTILINE)
_CHECKLIST_RE = re.compile(r"^[ \t]*Overall Validation Checklist[ \t]*$",
                           re.IGNORECASE | re.MULTILINE)


@dataclass
class SpecPhase:
    number: int
    title:  str
    body:   str
    roles:  tuple[str, ...]   # roles whose screen data is sent with this phase


def _infer_phase_roles(heading: str, body: str) -> tuple[str, ...]:
    """Which roles' screens does this phase need? Driven by the explicit
    '<X> App' mentions in the spec text. Untagged phases (Assign/Split,
    Comp, Notify Payment, Invoice Validation) are Business-App work, and a
    Kitchen phase always includes waiter data because the split/assign it
    mentions happens on the Business side."""
    blob = f"{heading}\n{body}".lower()
    roles = {role for tag, role in _APP_TAG_TO_ROLE.items() if tag in blob}
    if not roles:
        roles = {"waiter"}
    if "kitchen" in roles:
        roles.add("waiter")
    return tuple(r for r in _ROLES if r in roles)


def parse_e2e_spec(text: str) -> dict[str, Any]:
    """Split the spec document into: title, setup text, ordered phases, and
    the Overall Validation Checklist items."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    title = lines[0] if lines else "End-to-End Flow"

    phase_matches = list(_PHASE_RE.finditer(text))
    if not phase_matches:
        raise ValueError("No 'Phase N – Title' headings found in the E2E spec.")
    checklist_m = _CHECKLIST_RE.search(text)
    checklist_start = checklist_m.start() if checklist_m else len(text)

    setup_m = _SETUP_RE.search(text)
    setup_text = ""
    if setup_m and setup_m.end() <= phase_matches[0].start():
        setup_text = text[setup_m.end():phase_matches[0].start()].strip()

    phases: list[SpecPhase] = []
    if setup_text:
        phases.append(SpecPhase(0, "Test Setup", setup_text,
                                _infer_phase_roles("Test Setup", setup_text)))
    for i, m in enumerate(phase_matches):
        end = phase_matches[i + 1].start() if i + 1 < len(phase_matches) else checklist_start
        body = text[m.end():end].strip()
        heading = m.group(2).strip()
        phases.append(SpecPhase(int(m.group(1)), heading, body,
                                _infer_phase_roles(heading, body)))

    checklist: list[str] = []
    if checklist_m:
        for raw in text[checklist_m.end():].splitlines():
            item = raw.strip().lstrip("-•*·– ").strip()
            if item:
                checklist.append(item)

    return {"title": title, "setup": setup_text, "phases": phases, "checklist": checklist}


_E2E_SYSTEM_PROMPT = """You are a senior QA engineer writing ONE PHASE of a long end-to-end \
test for a restaurant platform with three apps sharing one backend: \
consumer, waiter and kitchen. (The spec's "Business App" is the WAITER role.)

{step_vocab}

You are given: the phase spec text, the test setup, the screens/journeys of \
the roles this phase likely touches, a "state ledger" describing everything \
that earlier phases already did, and the overall validation checklist.

Rules:
1. SEGMENTS. Output one or more segments for THIS phase only. A segment is a \
contiguous stay in one app. Every time the spec says to go to a different \
app ("Using Consumer App", "Back in Business App", "Go to Kitchen App", \
"Open Kitchen App"), start a NEW segment with that app's role. A phase that \
stays in one app is a single segment. Role must be consumer, waiter or kitchen.
2. NO COMPRESSION. Every operation in the spec must appear as its own explicit \
steps, in the spec's order. If the spec says "Use quantity 2, 3 and 5" or \
"Perform Assign, Split, Mixed assignments", write separate steps for each \
quantity and each operation. "Do not simply add one product per guest" means \
you must include multi-quantity items assigned/split across several guests. \
Never write a vague step like "split the items" -- name the item, the \
quantity, and which guests receive which part.
3. CONCRETE DATA. Product names given in the spec (e.g. Coca-Cola, Pizza) may \
be used verbatim. For any other product, use a label that appears in the \
screen data; if none exists, describe it by property ("the first product \
that has options") and add the need to "gaps". Keep the same guests/items \
that the state ledger already uses.
4. SCREEN GROUNDING. Never invent a screen, button or field label that is \
not in the data. When an element has a non-null "accessibility_id", your \
step MUST name THAT exact string, not "label" -- "label" is only what is \
visually painted, and some components hide that text from the real device \
(an explicit accessibilityLabel overrides it), so a step written against \
"label" alone can be untappable even though it looks right. Only fall back \
to "label" when "accessibility_id" is null. If the spec requires something \
you cannot find a label or id for (e.g. switching between Host and \
Participant accounts on the one consumer device, a swipe-to-split gesture, \
a Food Voucher payment mode), write the closest steps you can using real \
labels/ids and list the missing piece in "gaps".
5. VERIFICATION. The runner can only assert visibility, so for every \
validation in the spec (coupon applied, tip amount, VAT per rate, totals, \
discount on the overall payable, invoice contents) write \
"verify <real on-screen label> is visible" steps. Put the exact numeric rule \
to check by hand/automation in "checks", e.g. "tip = tip% x subtotal after \
discount", "VAT 14% line = sum of 14% items' net x 0.14", "discount applied \
to the whole payment total, not one item".
6. REAL EXECUTION. These run unattended against staging; sending to kitchen, \
paying, voiding, comping and closing the table genuinely happen. Spell out \
each tap/fill/select individually.
7. "state_after": 2-6 sentences describing the state once this phase is done: \
which guest holds which items and quantities, which are sent/served/voided/\
comped (and how many units), event/table status, payments made. The next \
phase is written from this alone.
8. "checklist_covered": the validation-checklist items (copied verbatim from \
the list given) that this phase actually exercises.
9. PAGE NAVIGATION. Never assume a tap navigated, a modal closed, or a page \
loaded. Write every tap, swipe, text entry, and confirmation as its own step. \
After each action that changes screens, verify a real label/id from the \
destination screen is visible before interacting with it. When a segment is \
required to end Home, explicitly navigate there, verify Home, and make that the \
segment's final step. A later segment must explicitly navigate from Home to its \
starting screen. Do not leave the app parked on a dialog or intermediate page.
10. VENUE. This entire scenario is for Nylai Kitchen2 (also shown in ids as \
NylaiKitchen2). Keep every consumer, waiter, and kitchen segment on that same \
restaurant. Do not select or substitute another restaurant or kitchen.
11. SETUP ORDER. Follow every numbered Test Setup action in exact order. In \
particular, after Add Guest, include seven separate guest-selection steps, then \
Invite; drag the duration slider to 1 hr, select the first available time \
slot (naming its exact on-screen label), press bookAppoitment, \
press ORDER LATER, and return Home. Do not combine, reorder, or omit them.
The duration control is a drag-only slider: do not tap duration markers or ids.

Return ONLY a JSON object of exactly this shape, no prose:
{{
  "segments": [
    {{
      "name":   "short title of this segment",
      "role":   "consumer|waiter|kitchen",
      "steps":  ["tap ...", "fill ... with ...", "verify ... is visible", ...],
      "checks": ["expected-value rule", ...],
      "gaps":   ["spec requirement with no matching screen/label", ...]
    }}
  ],
  "state_after":       "...",
  "checklist_covered": ["...", ...]
}}"""


def _validate_phase_segments(raw: Any) -> list[dict[str, Any]]:
    out = []
    for s in (raw or []):
        steps = [x for x in (s.get("steps") or []) if isinstance(x, str) and x.strip()]
        if s.get("role") not in _ROLES or not steps:
            logger.warning("    [skip] invalid segment (role=%r, steps=%d)",
                           s.get("role"), len(steps))
            continue
        out.append({
            "name":   s.get("name", ""),
            "role":   s["role"],
            "steps":  steps,
            "checks": [c for c in (s.get("checks") or []) if isinstance(c, str)],
            "gaps":   [g for g in (s.get("gaps") or []) if isinstance(g, str)],
        })
    return out


def _setup_segment_is_complete(segments: list[dict[str, Any]]) -> bool:
    """Reject setup output unless the required booking path is explicit."""
    if not segments or segments[0].get("role") != "consumer":
        return False

    steps = [step.strip().lower() for step in segments[0].get("steps", [])]
    normalized = [re.sub(r"[^a-z0-9]+", "", step) for step in steps]
    venue = next((i for i, step in enumerate(normalized)
                  if "nylaikitchen2" in step), None)
    add_guest = next((i for i, step in enumerate(normalized)
                      if "guestadd" in step or "addguest" in step), None)
    if venue is None or add_guest is None or venue >= add_guest:
        return False
    invite = next((i for i in range(add_guest + 1, len(steps))
                   if "invite" in normalized[i]
                   and re.match(r"^(?:(?:step\s*)?\d+[.): -]*\s*)?(open|tap|click|press|select|choose|pick)\b",
                                steps[i])), None)
    if invite is None:
        return False

    guest_selections = [
        step for step in steps[add_guest + 1:invite]
        if re.match(r"^(?:(?:step\s*)?\d+[.): -]*\s*)?(select|tap|choose|pick)\b", step)
    ]
    if len(guest_selections) != 7:
        return False

    cursor = invite
    duration = next((i for i in range(cursor + 1, len(steps))
                     if any(marker in normalized[i]
                            for marker in ("1hr", "1hour", "onehour", "duration1"))
                     and any(verb in normalized[i] for verb in ("drag", "swipe"))), None)
    if duration is None:
        return False

    time_slot = next((i for i in range(duration + 1, len(steps))
                      if (("first" in normalized[i]
                          and any(word in normalized[i] for word in ("slot", "time")))
                         or re.search(r"\b\d{1,2}:\d{2}\b", steps[i]))
                      and re.match(r"^(?:(?:step\s*)?\d+[.): -]*\s*)?(tap|click|press|select|choose|pick)\b",
                                steps[i])), None)
    if time_slot is None:
        return False

    booking = next((i for i in range(time_slot + 1, len(steps))
                    if any(marker in normalized[i]
                      for marker in ("bookappoitment", "bookappointment", "bookreservation"))
                  and re.match(r"^(?:(?:step\s*)?\d+[.): -]*\s*)?(tap|click|press|book|reserve|confirm)\b",
                         steps[i])), None)
    order_later = next((i for i in range((booking or 0) + 1, len(steps))
                   if "orderlater" in normalized[i]
                   and re.match(r"^(?:(?:step\s*)?\d+[.): -]*\s*)?(tap|click|press|choose|select)\b",
                          steps[i])), None)
    if booking is None or order_later is None:
        return False

    final = len(steps) - 1
    return (final > order_later
            and "home" in normalized[final]
            and any(verb in normalized[final] for verb in ("verify", "check", "confirm", "ensure")))


def _generate_e2e_phase(
    phase: SpecPhase,
    spec: dict[str, Any],
    by_role: dict[str, dict[str, Any]],
    ledger: str,
    provider: GeminiVisionProvider,
    attempts: int = 2,
) -> Optional[dict[str, Any]]:
    """One Gemini call for one phase. Quota errors raise; a malformed/empty
    response is retried once, then returns None (caller aborts the flow so a
    half-built lifecycle is never written silently)."""
    system_prompt = _E2E_SYSTEM_PROMPT.format(step_vocab=_STEP_VOCAB)
    phase_text = f"{phase.title}\n{phase.body}"
    # Multi-role phases (e.g. consumer+waiter+kitchen) would otherwise send
    # the per-role cap to EACH role, tripling the call's token cost -- split
    # the cap across however many roles this phase actually needs.
    screen_cap = max(8, _MAX_SCREENS_PER_PHASE // max(1, len(phase.roles)))
    roles_payload = {}
    for r in phase.roles:
        if r not in by_role:
            continue
        rd = by_role[r]
        trimmed = _relevant_screens(rd["screens"], phase_text, cap=screen_cap)
        if len(trimmed) < len(rd["screens"]):
            logger.info("    %s screens trimmed %d -> %d for this phase",
                        r, len(rd["screens"]), len(trimmed))
        roles_payload[r] = {**rd, "screens": trimmed}
    payload = {
        "phase": {"number": phase.number, "title": phase.title, "spec_text": phase.body},
        "test_setup": spec["setup"],
        "likely_roles": list(phase.roles),
        "state_ledger_before_this_phase": ledger or "Nothing has happened yet.",
        "validation_checklist": spec["checklist"],
        "roles": roles_payload,
    }
    user_prompt = json.dumps(payload, indent=2, ensure_ascii=False)
    setup_retry_feedback = ""

    for attempt in range(1, attempts + 1):
        try:
            response = provider.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt + setup_retry_feedback,
                json_schema={"segments": []},
                max_tokens=_MAX_OUTPUT_TOKENS,
            )
        except Exception as e:
            if is_quota_error(e):
                raise
            logger.error("    Gemini call failed (phase %s, attempt %d): %s",
                         phase.number, attempt, e)
            continue
        try:
            data = json.loads(response.content)
        except Exception as e:
            logger.error("    Unparseable response (phase %s, attempt %d): %s",
                         phase.number, attempt, e)
            continue
        segments = _validate_phase_segments(data.get("segments"))
        if not segments:
            logger.error("    No valid segments (phase %s, attempt %d).",
                         phase.number, attempt)
            continue
        if phase.number == 0 and not _setup_segment_is_complete(segments):
            logger.error("    Setup is missing required guest or booking steps "
                         "(phase %s, attempt %d).", phase.number, attempt)
            setup_retry_feedback = (
                "\n\nRETRY CORRECTION: The previous setup segment failed validation. "
                "Return the full Consumer setup segment with Nylai Kitchen2, Add Guest, "
                "seven separate guest-selection actions before Invite, drag to 1 hr, "
                "select the first available time slot using its exact HH:MM label, "
                "bookAppoitment, ORDER LATER, then return Home and verify/check/ensure "
                "Home is visible as the final step. Keep each action as a separate step."
            )
            continue
        return {
            "segments":          segments,
            "state_after":       str(data.get("state_after") or "").strip(),
            "checklist_covered": [c for c in (data.get("checklist_covered") or [])
                                  if isinstance(c, str)],
        }
    return None


def generate_e2e_flow(
    spec: dict[str, Any],
    pack: dict[str, Any],
    provider: GeminiVisionProvider,
    allow_partial: bool = False,
) -> Optional[dict[str, Any]]:
    """Walk the spec phase-by-phase, carrying a state ledger forward, and
    assemble ONE cross-app flow. Returns None if any phase failed and
    allow_partial is False."""
    by_role = {r: d for r in _ROLES if (d := build_role_data(r, pack)) is not None}
    missing = {r for ph in spec["phases"] for r in ph.roles} - set(by_role)
    if missing:
        logger.warning("Pack has no screens for role(s) %s — phases using them "
                       "will be generated without that app's screen data.",
                       sorted(missing))

    ledger = ""
    segments: list[dict[str, Any]] = []
    covered: set[str] = set()
    failed_phases: list[int] = []

    for phase in spec["phases"]:
        logger.info("  Phase %s — %s (roles: %s)", phase.number, phase.title,
                    ", ".join(phase.roles))
        result = _generate_e2e_phase(phase, spec, by_role, ledger, provider)
        if result is None:
            failed_phases.append(phase.number)
            if allow_partial and phase.number != 0:
                logger.error("    Phase %s failed — continuing (--allow-partial).",
                             phase.number)
                continue
            logger.error("    Phase %s failed — aborting E2E flow (nothing written).",
                         phase.number)
            return None

        for seg in result["segments"]:
            label = f"P{phase.number} {phase.title}"
            seg["name"] = f"{label} — {seg['name']}" if seg["name"] else label
            segments.append(seg)
            logger.info("    segment [%s] %s: %d steps, %d checks, %d gaps",
                        seg["role"], seg["name"], len(seg["steps"]),
                        len(seg["checks"]), len(seg["gaps"]))
            for g in seg["gaps"]:
                logger.warning("      GAP: %s", g)

        if result["state_after"]:
            ledger += f"\n[After phase {phase.number}] {result['state_after']}"
        covered.update(c.strip().lower() for c in result["checklist_covered"])

    if not segments:
        return None

    uncovered = [c for c in spec["checklist"] if c.strip().lower() not in covered]
    if uncovered:
        logger.warning("Validation-checklist items not tagged by any phase (%d): %s",
                       len(uncovered), "; ".join(uncovered))
    else:
        logger.info("All %d validation-checklist items are exercised.",
                    len(spec["checklist"]))
    if failed_phases:
        logger.warning("Flow is PARTIAL — failed phases: %s", failed_phases)

    for i, seg in enumerate(segments):
        seg["num"] = str(i + 1)

    return {
        "name":        spec["title"],
        "description": ("Generated end-to-end lifecycle from spec: "
                        + " → ".join(f"P{p.number}" for p in spec["phases"])
                        + (f" (PARTIAL, missing {failed_phases})" if failed_phases else "")),
        "segments":    segments,
    }


def write_e2e_flow(
    db,
    flow: dict[str, Any],
    replace: bool = False,
    dry_run: bool = False,
) -> int:
    """Insert the flow, keeping earlier generated variants unless replacing."""
    base_name = flow["name"]
    base_slug = _slug(base_name)
    variant = 1
    while True:
        if variant == 1:
            flow_name = base_name
            flow_id = f"ai_e2e_{base_slug[:60]}"
        else:
            suffix = f"_v{variant}"
            flow_name = f"{base_name} (Variant {variant})"
            flow_id = f"ai_e2e_{base_slug[:60 - len(suffix)]}{suffix}"

        row = db.query(CrossAppFlowEdit).filter(
            CrossAppFlowEdit.id == flow_id
        ).first()
        if flow_id in FLOWS:
            variant += 1
            continue
        if row and not (replace and variant == 1):
            variant += 1
            continue
        break

    n_steps = sum(len(s["steps"]) for s in flow["segments"])
    if dry_run:
        logger.info("  [dry-run] would %s E2E flow %r (%s): %d segments, %d steps, roles %s",
                    "replace" if row else "create", flow_id, flow_name,
                    len(flow["segments"]), n_steps,
                    sorted({s["role"] for s in flow["segments"]}))
        return 1
    if row:
        row.name        = flow_name
        row.description = flow["description"]
        row.segments    = flow["segments"]
    else:
        db.add(CrossAppFlowEdit(
            id          = flow_id,
            name        = flow_name,
            description = flow["description"],
            segments    = flow["segments"],
        ))
    db.commit()
    logger.info("  E2E flow %r (%s) %s: %d segments, %d steps.",
                flow_id, flow_name, "replaced" if row else "created",
                len(flow["segments"]), n_steps)
    return 1


# ══════════════════════════════════════════════════════════════════════════════
# 4. ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════════════

def generate_all(
    spec_path: Path = _E2E_SPEC_PATH,
    pack_path: Path = _PACK_PATH,
    dry_run: bool = False,
    replace: bool = False,
    allow_partial: bool = False,
) -> dict[str, int]:
    """Build ONE large cross-app flow from the client's spec and store it."""
    pack = load_pack(pack_path)
    spec_path = Path(spec_path)
    if not spec_path.exists():
        raise FileNotFoundError(f"E2E spec not found at {spec_path} — pass --spec.")
    spec = parse_e2e_spec(spec_path.read_text(encoding="utf-8"))
    logger.info("Parsed %d phase(s) and %d checklist item(s) from %s.",
                len(spec["phases"]), len(spec["checklist"]), spec_path)

    provider = GeminiVisionProvider()
    try:
        flow = generate_e2e_flow(spec, pack, provider, allow_partial=allow_partial)
    except Exception as e:
        if is_quota_error(e):
            logger.error("Gemini quota exhausted — nothing written.")
            return {"e2e": 0}
        raise
    if not flow:
        return {"e2e": 0}

    db = SessionLocal()
    try:
        return {"e2e": write_e2e_flow(db, flow, replace=replace, dry_run=dry_run)}
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════════════════
# 5. CLI ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=("Generate a large end-to-end cross-app flow from the spec. "
                    "Existing flows are preserved; later runs create numbered variants.")
    )
    parser.add_argument("--spec", default=str(_E2E_SPEC_PATH),
                        help="Path to the phase-structured E2E spec (txt/md).")
    parser.add_argument("--pack", default=str(_PACK_PATH),
                        help="Path to app_knowledge_pack.json")
    parser.add_argument("--replace", action="store_true",
                        help="Overwrite the existing E2E flow with the same id.")
    parser.add_argument("--allow-partial", action="store_true",
                        help="Write the flow even if some phases failed to generate.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be created without writing to the database.")
    args = parser.parse_args()

    result = generate_all(
        spec_path     = Path(args.spec),
        pack_path     = Path(args.pack),
        dry_run       = args.dry_run,
        replace       = args.replace,
        allow_partial = args.allow_partial,
    )
    logger.info("Done — %s", result)