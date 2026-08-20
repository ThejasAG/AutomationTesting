"""Tickets: paste an issue, link it to a PR + scenarios, and auto-test it.

When a linked PR is pushed the platform runs the ticket's scenarios and flips
its status (see webhooks integration). Also runnable on demand via /run.
"""

from __future__ import annotations

import logging
import threading
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from automation.auth.security import get_current_user
from automation.database.config import get_db, SessionLocal
from automation.database.models import Ticket, SavedScenario, TestProject

logger = logging.getLogger("tickets")
router = APIRouter(prefix="/tickets", tags=["tickets"])


class TicketIn(BaseModel):
    title: str
    description: str = ""
    project_id: Optional[str] = None
    pr_number: Optional[str] = None
    pr_url: Optional[str] = None
    match_key: Optional[str] = None   # e.g. "NEWVYA-1134" — links to the PR before it exists
    scenario_ids: List[str] = []
    setup_scenario_id: Optional[str] = None
    ticket_type: str = "ui"


@router.post("")
def _norm_pr(v: Optional[str]) -> Optional[str]:
    """Store the bare PR number ('535'), so the GitHub webhook — which matches on
    str(pr_number) — links correctly whether the user typed '535', '#535' or 'PR 535'."""
    import re
    if not v:
        return None
    m = re.search(r"\d+", str(v))
    return m.group(0) if m else (str(v).strip() or None)


def create_ticket(body: TicketIn, db: Session = Depends(get_db),
                  current_user=Depends(get_current_user)):
    t = Ticket(
        title=body.title, description=body.description, project_id=body.project_id,
        pr_number=_norm_pr(body.pr_number), pr_url=body.pr_url,
        match_key=(body.match_key or "").strip() or None,
        scenario_ids=body.scenario_ids or [], ticket_type=body.ticket_type or "ui",
        setup_scenario_id=body.setup_scenario_id or None,
    )
    db.add(t); db.commit(); db.refresh(t)
    return t.to_dict()


@router.get("")
def list_tickets(project_id: Optional[str] = None, db: Session = Depends(get_db),
                 current_user=Depends(get_current_user)):
    q = db.query(Ticket).order_by(Ticket.created_at.desc())
    if project_id:
        q = q.filter(Ticket.project_id == project_id)
    return {"tickets": [t.to_dict() for t in q.all()]}


@router.get("/{ticket_id}")
def get_ticket(ticket_id: str, db: Session = Depends(get_db),
               current_user=Depends(get_current_user)):
    t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return t.to_dict()


@router.put("/{ticket_id}")
def update_ticket(ticket_id: str, body: TicketIn, db: Session = Depends(get_db),
                  current_user=Depends(get_current_user)):
    t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Ticket not found")
    t.title = body.title
    t.description = body.description
    t.project_id = body.project_id
    t.pr_number = _norm_pr(body.pr_number)
    t.pr_url = body.pr_url
    t.match_key = (body.match_key or "").strip() or None
    t.scenario_ids = body.scenario_ids or []
    t.setup_scenario_id = body.setup_scenario_id or None
    t.ticket_type = body.ticket_type or t.ticket_type
    db.commit(); db.refresh(t)
    return t.to_dict()


@router.delete("/{ticket_id}")
def delete_ticket(ticket_id: str, db: Session = Depends(get_db),
                  current_user=Depends(get_current_user)):
    t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if t:
        db.delete(t); db.commit()
    return {"deleted": True}


class DraftIn(BaseModel):
    description: str
    title: str = ""
    project_id: str = ""


def _app_knowledge(project_id: str, db: Session) -> str:
    """Assemble grounding context so the drafter uses the app's REAL screens, verified
    navigation paths, element labels and API endpoints instead of guessing. Every
    source is best-effort and optional — this is the machine-readable form of the same
    cross-app flow/connection map humans see in Live Steps."""
    import os
    import re
    import subprocess
    parts: List[str] = []

    # 0) Curated, human-verified facts — AUTHORITATIVE domain knowledge that code and
    #    recorded flows cannot express (section names, terminology, XP amounts, the
    #    pre-book→cancel precondition). Per-project file overrides the default.
    try:
        kdir = os.path.join("automation", "knowledge")
        kfile = os.path.join(kdir, f"{project_id}.md") if project_id else ""
        if not (kfile and os.path.isfile(kfile)):
            kfile = os.path.join(kdir, "default.md")
        if os.path.isfile(kfile):
            with open(kfile, "r", errors="ignore") as fh:
                parts.append("CURATED APP FACTS (AUTHORITATIVE — override any guess or "
                             "ticket wording that conflicts):\n" + fh.read()[:3000])
    except Exception as e:
        logger.debug("app_knowledge curated: %s", e)

    # 0b) The full scenario library (70 flows extracted from the bot, verified). Inject
    #     a COMPACT index (id — name) so the drafter knows what domain flows exist and
    #     can name the right one, without bloating the prompt with every step.
    try:
        lib = os.path.join("automation", "knowledge", "vya_scenario_library.md")
        if os.path.isfile(lib):
            with open(lib, "r", errors="ignore") as fh:
                heads = re.findall(r"^###\s+(\S+)\s+—\s+(.+)$", fh.read(), re.M)
            if heads:
                idx = "; ".join(f"{sid}={name}" for sid, name in heads)
                parts.append("KNOWN SCENARIO LIBRARY (70 verified end-to-end flows — reference "
                             "the closest ones when a ticket touches them):\n" + idx[:2500])
    except Exception as e:
        logger.debug("app_knowledge library: %s", e)

    # 1) Recorded, working scenarios = proven navigation building blocks (highest
    #    fidelity — these steps/labels are known to resolve on the real app).
    try:
        q = db.query(SavedScenario)
        if project_id:
            q = q.filter((SavedScenario.project_id == project_id) | (SavedScenario.project_id.is_(None)))
        scs = q.limit(40).all()
        if scs:
            lines = [f'- "{s.name}": {"; ".join((s.steps or [])[:12])}' for s in scs]
            parts.append(
                "RECORDED, WORKING FLOWS — reuse these exact steps, labels and navigation "
                "paths as building blocks and as the SETUP/preconditions before a check "
                "(e.g. to cancel an event, first run the recorded 'book event' steps):\n"
                + "\n".join(lines))
    except Exception as e:
        logger.debug("app_knowledge scenarios: %s", e)

    # 2) Cross-app connection model + verified step vocabulary (who acts, in what
    #    order, and how a booking flows Consumer → Waiter → Kitchen → Waiter).
    try:
        from automation.scenarios.cross_app_flows import FLOWS
        flow_lines = [f"- {f['name']}: " + " → ".join(f"{s['role']}:{s['name']}" for s in f["segments"])
                      for f in FLOWS.values()]
        parts.append(
            "CROSS-APP CONNECTIONS (the platform's map of where actions go across apps):\n"
            + "\n".join(flow_lines))
    except Exception as e:
        logger.debug("app_knowledge flows: %s", e)

    # 3) App-repo profile: real screens, the ?query= RPC endpoints, curated CLAUDE.md.
    try:
        from automation.projects.repository import repository_manager
        repo = repository_manager.get_repo_path(project_id) if project_id else ""
        appdir = os.path.join(repo, "App") if repo else ""
        if appdir and os.path.isdir(appdir):
            sdir = os.path.join(appdir, "Screens")
            if os.path.isdir(sdir):
                screens = sorted(d for d in os.listdir(sdir)
                                 if os.path.isdir(os.path.join(sdir, d)) and not d.startswith("_"))
                if screens:
                    parts.append("REAL SCREENS in the app: " + ", ".join(screens[:60]))
            # ?query= RPC endpoints — grep is fast and stays inside App/ (no node_modules).
            try:
                out = subprocess.run(
                    ["grep", "-rhoE", r"/[a-zA-Z_]+/(api|auth)\?query=[a-zA-Z]+", appdir],
                    capture_output=True, text=True, timeout=20).stdout
                ops = sorted(set(out.split()))
                if ops:
                    parts.append("REAL API ENDPOINTS (?query= RPC — use these EXACT paths for "
                                 "'verify api …'; NEVER invent an endpoint):\n" + "\n".join(ops[:60]))
            except Exception:
                pass
            cm = os.path.join(repo, "CLAUDE.md")
            if os.path.isfile(cm):
                with open(cm, "r", errors="ignore") as fh:
                    body = re.sub(r"\n{3,}", "\n\n", fh.read())
                parts.append("APP NOTES (curated, from the repo's CLAUDE.md):\n" + body[:1800])
    except Exception as e:
        logger.debug("app_knowledge repo: %s", e)

    # 4) Durable knowledge graph (graphify) — the app's real navigation hubs by
    #    connectivity, so the drafter anchors flows on screens that actually exist.
    try:
        import json as _json
        gpath = os.path.join("graphify-out", "graph.json")
        if os.path.isfile(gpath):
            with open(gpath, encoding="utf-8") as gf:
                g = _json.load(gf)
            deg: dict = {}
            for e in (g.get("links") or g.get("edges") or []):  # NetworkX uses "links"
                for k in ("source", "target"):
                    v = e.get(k)
                    if v:
                        deg[v] = deg.get(v, 0) + 1
            id2label = {n.get("id"): (n.get("label") or n.get("id")) for n in g.get("nodes", [])}
            names: List[str] = []
            for nid, _d in sorted(deg.items(), key=lambda kv: kv[1], reverse=True):
                nm = str(id2label.get(nid, nid)).split("/")[-1].replace(".js", "")
                if nm and nm not in names:
                    names.append(nm)
                if len(names) >= 20:
                    break
            if names:
                parts.append("KEY SCREENS/MODULES (from the app knowledge graph, most-"
                             "connected first — the real navigation hubs; anchor flows on these):\n"
                             + ", ".join(names))
    except Exception as e:
        logger.debug("app_knowledge graph: %s", e)

    return "\n\n".join(parts)[:9000]


# Verbs whose object is a concrete on-screen target we can validate. Verbs like
# 'open app', 'verify app did not crash', 'verify api/calculation/bill' have no
# screen target, so they're never flagged.
_TARGET_VERBS = ("click ", "select ", "tap ", "go to ")
_STOPWORDS = {"the", "a", "an", "to", "in", "on", "is", "of", "and", "app", "page",
              "screen", "button", "tab", "section", "not", "visible", "for", "your"}


def _app_vocabulary(project_id: str, db: Session) -> set:
    """Every real, tappable/navigable token the app is known to expose — from the
    recorded scenarios, the knowledge-graph node labels, and the repo's testID /
    accessibilityLabel values. Used to flag drafted steps that reference something
    that doesn't exist (a hallucinated screen/button)."""
    import os
    import re
    import subprocess
    vocab: set = set()

    def _add(text: str):
        for w in re.findall(r"[a-zA-Z][a-zA-Z0-9]+", str(text).lower()):
            if len(w) > 2 and w not in _STOPWORDS:
                vocab.add(w)

    # Curated facts declare a REAL_ELEMENTS: allowlist — parse ONLY that line, not the
    # prose (the prose says things like "no Events section" and must NOT enter vocab).
    try:
        kdir = os.path.join("automation", "knowledge")
        kfile = os.path.join(kdir, f"{project_id}.md") if project_id else ""
        if not (kfile and os.path.isfile(kfile)):
            kfile = os.path.join(kdir, "default.md")
        if os.path.isfile(kfile):
            with open(kfile, "r", errors="ignore") as fh:
                m = re.search(r"^REAL_ELEMENTS:\s*(.+)$", fh.read(), re.M)
            if m:
                for name in m.group(1).split(","):
                    _add(name)
        vocab.add("xp")  # 2-letter term, added explicitly (below the len>2 filter)
    except Exception as e:
        logger.debug("vocab curated: %s", e)

    try:
        q = db.query(SavedScenario)
        if project_id:
            q = q.filter((SavedScenario.project_id == project_id) | (SavedScenario.project_id.is_(None)))
        for s in q.limit(200).all():
            for step in (s.steps or []):
                _add(step)
    except Exception as e:
        logger.debug("vocab scenarios: %s", e)

    try:
        import json as _json
        gpath = os.path.join("graphify-out", "graph.json")
        if os.path.isfile(gpath):
            with open(gpath, encoding="utf-8") as gf:
                g = _json.load(gf)
            for n in g.get("nodes", []):
                _add(n.get("label") or "")
    except Exception as e:
        logger.debug("vocab graph: %s", e)

    try:
        from automation.projects.repository import repository_manager
        repo = repository_manager.get_repo_path(project_id) if project_id else ""
        appdir = os.path.join(repo, "App") if repo else ""
        if appdir and os.path.isdir(appdir):
            out = subprocess.run(
                ["grep", "-rhoE", r'(testID|accessibilityLabel|accessibilityHint)=\{?"[^"]+"', appdir],
                capture_output=True, text=True, timeout=20).stdout
            for m in re.findall(r'"([^"]+)"', out):
                _add(m)
    except Exception as e:
        logger.debug("vocab repo: %s", e)

    return vocab


def _unverified_steps(steps: List[str], vocab: set) -> List[str]:
    """Drafted steps whose on-screen target isn't found anywhere in the app vocabulary
    → likely hallucinated; the reviewer should record/confirm them before saving."""
    import re
    if not vocab:
        return []
    flagged = []
    for step in steps:
        low = step.lower().strip()
        # Negative assertions ("… is not visible") EXPECT the target to be absent —
        # absence from the vocabulary is the point, so never flag them.
        if "not visible" in low or " is not " in low:
            continue
        target = ""
        if low.startswith(_TARGET_VERBS):
            target = re.sub(r"^(click|select|tap|go to)\s+", "", low)
        elif low.startswith("verify ") and (" is visible" in low or low.endswith(" visible")):
            target = re.sub(r"^verify\s+|\s+is\s+visible$|\s+visible$", "", low)
        else:
            continue  # no concrete screen target to validate
        words = [w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9]+", target)
                 if len(w) > 2 and w not in _STOPWORDS]
        if not words:
            continue
        # Grounded if ANY significant word of the target is in the app vocabulary.
        if not any(w in vocab for w in words):
            flagged.append(step)
    return flagged


_DRAFT_SYSTEM = (
    "You are a senior mobile QA engineer. Convert a ticket into runnable UI test "
    "scenarios for an Appium-driven app automation tool.\n"
    "Each scenario has a short `name` and an ordered list of plain-English `steps`.\n"
    "Allowed step verbs (keep steps short, one action each):\n"
    "  open app | click <label> | select <label> | type <text> in <field> |\n"
    "  go to <screen> | verify <label/text> is visible | verify <text> | \n"
    "  verify app did not crash | verify calculation <math> = <expected>\n"
    "     (e.g. 'verify calculation 10 - 1 = 9' for inventory, 'verify calculation total = 15.00')\n"
    "  capture <label> as <name>   (read a value now to compare later — use this for\n"
    "     before/after deltas instead of typing values into fields; e.g.\n"
    "     'capture XP balance as before', … , 'capture XP balance as after',\n"
    "     'verify calculation before - 30 = after').\n"
    "  verify api <path> <field> == <value>\n"
    "     (ONLY for backend values not on screen, e.g. 'verify api /inventory/itemA stock == 9')\n"
    "  verify width|height of <element> = <px>   (UI SIZE tickets, e.g. button 40→60)\n"
    "  verify bill                                (bill total = sum of items)\n"
    "  verify discount <original_total> <coupon_value>   (discount/coupon correct)\n"
    "Rules:\n"
    "- You may be given APP KNOWLEDGE (recorded working flows, cross-app connections, "
    "real screens & API endpoints). GROUND every step in it: reuse the recorded steps, "
    "labels and navigation paths as building blocks and preconditions, use the real "
    "screen names, and use the EXACT ?query= endpoints. NEVER invent an element, screen, "
    "label, amount or API path that is not supported by the ticket or the app knowledge; "
    "if a value like an XP amount is not given, assert it by BALANCE DELTA (capture "
    "before, capture after, verify the difference) instead of hardcoding a guessed number.\n"
    "- ARRANGE → ACT → ASSERT. Include the SETUP steps needed to reach the state\n"
    "  before the check. e.g. to 'cancel an event' you must FIRST book/create one:\n"
    "  'open app','go to Events','click Book Event',...,'click Cancel','verify ...'.\n"
    "  Never assert on a state without the steps that create it.\n"
    "- Assert the EXPECTED / FIXED behavior from the ticket, NEVER the current bug.\n"
    "  e.g. bug 'shows Points, should show XP' -> steps: 'verify XP is visible',\n"
    "  'verify Points is not visible' (assert the fix, not the defect).\n"
    "  e.g. 'says 50, should say 30' -> 'verify 30 is visible', 'verify 50 is not visible'.\n"
    "- Prefer what is VISIBLE on screen (text, buttons, navigation).\n"
    "- Always start with 'open app' and end with 'verify app did not crash'.\n"
    "- Split the ticket's test scenarios into separate scenario objects.\n"
    "Also classify the ticket `type` as one of: ui, calc, data, crash, mixed.\n"
    "  ui = wording/navigation/visibility; calc = on-screen math/totals; \n"
    "  data = needs backend/DB values not on screen; crash = app crashing.\n"
    'Return ONLY JSON: {"type":"ui","scenarios":[{"name":"...","steps":["open app","..."]}]}'
)


@router.post("/draft")
def draft_scenarios(body: DraftIn, db: Session = Depends(get_db),
                    current_user=Depends(get_current_user)):
    """AI-draft test scenarios from a pasted ticket (human reviews before saving).

    Grounds the draft in the app's real knowledge (recorded flows, cross-app
    connections, screens, endpoints) so steps reference real paths/labels/APIs."""
    import json as _json
    from automation.ai.provider import create_provider, default_config
    text = (body.title + "\n\n" + body.description).strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty ticket text.")
    know = _app_knowledge(body.project_id, db)
    user = (f"APP KNOWLEDGE (ground every step in this):\n{know}\n\n" if know else "") + \
           f"TICKET:\n{text[:6000]}"
    try:
        resp = create_provider(default_config).generate(
            _DRAFT_SYSTEM, user, json_schema={},
            max_tokens=2000, temperature=0.2)
        raw = resp.content or ""
        # Be tolerant: extract the first {...} block.
        m = raw[raw.find("{"): raw.rfind("}") + 1] if "{" in raw else raw
        data = _json.loads(m)
        scenarios = [
            {"name": (s.get("name") or f"Scenario {i+1}")[:120],
             "steps": [str(x) for x in (s.get("steps") or []) if str(x).strip()]}
            for i, s in enumerate(data.get("scenarios") or [])
            if s.get("steps")
        ]
        ttype = data.get("type") if data.get("type") in ("ui", "calc", "data", "crash", "mixed") else "mixed"
        # Self-correction: flag steps whose target isn't in the app's real vocabulary
        # so the reviewer records/confirms them instead of saving a hallucinated step.
        vocab = _app_vocabulary(body.project_id, db)
        for s in scenarios:
            s["unverified"] = _unverified_steps(s["steps"], vocab)
        return {"type": ttype, "scenarios": scenarios, "count": len(scenarios)}
    except Exception as e:
        logger.warning("draft_scenarios failed: %s", e)
        raise HTTPException(status_code=502, detail=f"AI draft failed: {e}")


@router.post("/{ticket_id}/run")
def run_ticket(ticket_id: str, db: Session = Depends(get_db),
               current_user=Depends(get_current_user)):
    """Run the ticket's linked scenarios in the background; flip status when done."""
    t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if not (t.scenario_ids or []):
        raise HTTPException(status_code=400, detail="No scenarios linked to this ticket.")
    t.status = "running"; db.commit()
    threading.Thread(target=run_ticket_scenarios, args=(ticket_id,), daemon=True).start()
    return {"started": True, "ticket_id": ticket_id, "scenarios": len(t.scenario_ids or [])}


def run_ticket_scenarios(ticket_id: str) -> None:
    """Run each linked scenario sequentially; set ticket status from the results.

    Shared by the /run endpoint and the PR webhook. Never raises."""
    from automation.api.v1.routers.scenario import run_scenario_headless, ScenarioRequest
    try:
        with SessionLocal() as db:
            t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
            if not t:
                return
            scenarios = (db.query(SavedScenario)
                         .filter(SavedScenario.id.in_(t.scenario_ids or [])).all())
            proj = db.query(TestProject).filter(TestProject.id == t.project_id).first()
            default_bundle = proj.app_bundle_id if proj else None
            # Reusable setup ("Book an event" etc.) whose steps run BEFORE each check.
            setup_steps = []
            if t.setup_scenario_id:
                setup = db.query(SavedScenario).filter(SavedScenario.id == t.setup_scenario_id).first()
                setup_steps = (setup.steps or []) if setup else []

        all_passed = True
        last_run = None
        for sc in scenarios:
            req = ScenarioRequest(
                project_id=sc.project_id or (t.project_id or ""),
                device_id=sc.device_id or _first_booted_udid(),
                bundle_id=sc.bundle_id or default_bundle,
                # ARRANGE (setup) → then the scenario's ACT/ASSERT steps.
                steps=(setup_steps + (sc.steps or [])), name=sc.name, save=False, prepare=False,
            )
            try:
                with SessionLocal() as db:
                    out = run_scenario_headless(req, db)
                if not out.get("ok"):
                    all_passed = False
                last_run = out.get("run_id") or last_run
            except Exception as e:
                logger.warning("ticket %s scenario %s failed: %s", ticket_id, sc.id, e)
                all_passed = False

        with SessionLocal() as db:
            t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
            if t:
                t.status = "passed" if all_passed and scenarios else "failed"
                t.last_run_id = last_run
                db.commit()
    except Exception as e:
        logger.warning("run_ticket_scenarios failed for %s: %s", ticket_id, e)
        with SessionLocal() as db:
            t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
            if t:
                t.status = "failed"; db.commit()


def _first_booted_udid() -> str:
    import json as _json, subprocess as _sp
    try:
        out = _sp.run(["xcrun", "simctl", "list", "devices", "booted", "-j"],
                      capture_output=True, text=True, timeout=15).stdout
        for _rt, devs in (_json.loads(out).get("devices") or {}).items():
            for d in devs:
                if d.get("state") == "Booted":
                    return d["udid"]
    except Exception:
        pass
    return ""
