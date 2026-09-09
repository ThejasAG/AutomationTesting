#!/usr/bin/env python3
"""Move the platform's OWN data — projects and saved scenarios — between machines.

The database deliberately lives outside the repo (`.db` files are in this repo's git
history, and a checkout crossing those commits overwrites a live database — that is
how a full set of projects and scenarios was lost on 2026-08-27). The cost of that
safety is that a fresh machine starts EMPTY: same repo, same apps, no scenarios.

So the data travels as a committed JSON seed instead of a binary the VCS can clobber.

    .venv/bin/python scripts/platform_seed.py export      # this machine -> seeds/
    .venv/bin/python scripts/platform_seed.py import      # seeds/ -> a new machine
    .venv/bin/python scripts/platform_seed.py diff        # what is missing here

Add --scenarios-only to import just the test scenarios and leave projects alone —
for a machine that already has its projects registered (its own clones, its own
paths) and only needs the scenarios to run against them:

    .venv/bin/python scripts/platform_seed.py import --scenarios-only

Import is ADDITIVE and idempotent: it inserts what is absent, matched on a stable
natural key, and never edits or deletes anything already present. Re-running it is a
no-op. Run histories are NOT exported — they belong to the machine that produced them.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from automation.database.config import SessionLocal, initialize_database  # noqa: E402
from automation.database.models import SavedScenario, TestProject          # noqa: E402

SEED = Path(__file__).resolve().parents[1] / "seeds" / "platform.json"

# Natural keys: ids are generated per machine, so matching on them would duplicate
# everything on import. A project is its git_url + branch; a scenario is its name.
_PROJECT_FIELDS = ("name", "git_url", "default_branch", "platform", "repo_type",
                   "project_type", "app_bundle_id", "app_path", "description", "status")
def _as_list(value) -> list:
    """A scenario's steps/covers as a real list.

    Decodes a JSON string exactly ONE level — never recursively, because a
    legitimate list may itself contain a string that looks like JSON. Anything
    that does not decode to a list becomes [], the column's own default.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return []
        return decoded if isinstance(decoded, list) else []
    return []


_SCENARIO_FIELDS = ("name", "description", "steps", "covers", "bundle_id", "device_id")


#: Credentials must never reach the seed — it is COMMITTED, and git history is
#: forever. The values live in .env on each machine (VYA_BUSINESS_*), so the seed
#: carries the placeholder and the importing machine supplies its own.
_CREDENTIAL_VARS = ("VYA_BUSINESS_WAITER_USER", "VYA_BUSINESS_WAITER_PASSWORD",
                    "VYA_BUSINESS_KITCHEN_USER", "VYA_BUSINESS_KITCHEN_PASSWORD")


def _redact(steps):
    """Replace any known credential VALUE in a step with its ${VAR} placeholder.

    Substring match, not equality: a step reads "type <password> in passwordValue",
    so the secret is embedded in a sentence. Longest first, so a username that is a
    prefix of another value cannot mask it.
    """
    pairs = sorted(((os.getenv(v) or "", v) for v in _CREDENTIAL_VARS),
                   key=lambda kv: len(kv[0]), reverse=True)
    out = []
    for st in steps or []:
        text = st if isinstance(st, str) else str(st)
        for value, var in pairs:
            if value and value in text:
                text = text.replace(value, "${" + var + "}")
        out.append(text)
    return out


def _unredact(steps):
    """Put this machine's own credentials back in at import time."""
    out = []
    for st in steps or []:
        text = st if isinstance(st, str) else str(st)
        for var in _CREDENTIAL_VARS:
            token = "${" + var + "}"
            if token in text:
                text = text.replace(token, os.getenv(var) or token)
        out.append(text)
    return out


def _project_key(p) -> str:
    return f"{(p.git_url or '').strip().lower()}@{(p.default_branch or '').strip()}|{(p.name or '').strip()}"


def export() -> None:
    with SessionLocal() as db:
        projects = db.query(TestProject).order_by(TestProject.name).all()
        scenarios = db.query(SavedScenario).order_by(SavedScenario.name).all()
        by_id = {p.id: p for p in projects}
        data = {
            "exported_at": datetime.utcnow().isoformat() + "Z",
            "projects": [{f: getattr(p, f, None) for f in _PROJECT_FIELDS} for p in projects],
            "scenarios": [
                {**{f: getattr(s, f, None) for f in _SCENARIO_FIELDS},
                 # A real list, not json.dumps(...). These land in a JSON column,
                 # so dumping first made SQLAlchemy encode an already-encoded
                 # string — which is how five rows came to hold '"[…]"'.
                 "steps": _redact(_as_list(s.steps)),
                 "covers": _as_list(s.covers),
                 # Store the project's KEY, not its per-machine id.
                 "project_key": _project_key(by_id[s.project_id]) if s.project_id in by_id else None}
                for s in scenarios
            ],
        }
    SEED.parent.mkdir(parents=True, exist_ok=True)
    SEED.write_text(json.dumps(data, indent=2, default=str) + "\n")
    print(f"exported {len(data['projects'])} projects and {len(data['scenarios'])} "
          f"scenarios -> {SEED.relative_to(Path.cwd()) if SEED.is_relative_to(Path.cwd()) else SEED}")
    print("commit seeds/platform.json so the next machine gets them.")


def _load():
    if not SEED.is_file():
        sys.exit(f"no seed file at {SEED} — run `export` on the machine that has the data")
    return json.loads(SEED.read_text())


def diff() -> None:
    data = _load()
    with SessionLocal() as db:
        have_p = {_project_key(p) for p in db.query(TestProject).all()}
        have_s = {(s.name or "").strip() for s in db.query(SavedScenario).all()}
    miss_p = [p for p in data["projects"]
              if f"{(p.get('git_url') or '').strip().lower()}@{(p.get('default_branch') or '').strip()}|{(p.get('name') or '').strip()}" not in have_p]
    miss_s = [s for s in data["scenarios"] if (s.get("name") or "").strip() not in have_s]
    print(f"missing here: {len(miss_p)} project(s), {len(miss_s)} scenario(s)")
    for p in miss_p:
        print(f"  project   {p.get('name')}")
    for s in miss_s:
        print(f"  scenario  {s.get('name')}")
    if not miss_p and not miss_s:
        print("  nothing — this machine already has everything in the seed")


def do_import(scenarios_only: bool = False) -> None:
    import uuid
    data = _load()
    initialize_database()
    added_p = added_s = 0
    with SessionLocal() as db:
        existing = {_project_key(p): p for p in db.query(TestProject).all()}
        # A scenario still needs a project to hang off. With --scenarios-only we
        # match by NAME too, so a project this machine registered itself (its own
        # id, its own clone path) is used instead of creating a duplicate.
        by_name = {(p.name or "").strip().lower(): p for p in existing.values()}
        for row in [] if scenarios_only else data["projects"]:
            key = (f"{(row.get('git_url') or '').strip().lower()}"
                   f"@{(row.get('default_branch') or '').strip()}|{(row.get('name') or '').strip()}")
            if key in existing:
                continue
            p = TestProject(id=str(uuid.uuid4()),
                            **{f: row.get(f) for f in _PROJECT_FIELDS})
            db.add(p)
            db.flush()
            existing[key] = p
            added_p += 1

        have_s = {(s.name or "").strip() for s in db.query(SavedScenario).all()}
        for row in data["scenarios"]:
            name = (row.get("name") or "").strip()
            if not name or name in have_s:
                continue
            proj = existing.get(row.get("project_key") or "")
            if proj is None:
                # Fall back to this machine's own project of the same name.
                pname = next((p.get("name") for p in data["projects"]
                              if f"{(p.get('git_url') or '').strip().lower()}"
                                 f"@{(p.get('default_branch') or '').strip()}"
                                 f"|{(p.get('name') or '').strip()}" == row.get("project_key")),
                             None)
                proj = by_name.get((pname or "").strip().lower())
            db.add(SavedScenario(
                id=str(uuid.uuid4()),
                project_id=proj.id if proj else None,
                created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
                **{**{f: row.get(f) for f in _SCENARIO_FIELDS},
                   # _as_list() decodes an older seed file's encoded string
                   # exactly once; a canonical list passes straight through.
                   "steps": _unredact(_as_list(row.get("steps"))),
                   "covers": _as_list(row.get("covers"))}))
            have_s.add(name)
            added_s += 1
        db.commit()
    print(f"imported {added_p} project(s) and {added_s} scenario(s) "
          f"(existing rows were left untouched)")
    if scenarios_only:
        orphans = [s for s in data["scenarios"] if s.get("project_key")]
        print("--scenarios-only: projects on this machine were left exactly as they are.")
    if added_p:
        print("NOTE: registered projects still need their repo cloned — open Projects "
              "in the dashboard and let each one clone.")


if __name__ == "__main__":
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if cmd == "export":
        export()
    elif cmd == "import":
        do_import(scenarios_only="--scenarios-only" in sys.argv)
    elif cmd == "diff":
        diff()
    else:
        sys.exit(__doc__)
