"""The spine as it was at a git ref — the 'before' half of the PR delta.

The spine lives in automation/workflow/catalog.py, so "what did this PR change about
the application's flow?" is answerable by building the diagram from that file at the
base ref and comparing it to the working tree. Both sides go through the SAME converter
(archify_ir.build_ir) — diffing snapshots produced two different ways would report
differences that are artefacts of the builder, not of the change.
"""
from __future__ import annotations

import logging
import os
import subprocess
import types
from typing import Any, Dict, FrozenSet, Optional, Tuple

logger = logging.getLogger("archify")

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CATALOG_PATH = "automation/workflow/catalog.py"


def catalog_at_ref(ref: str) -> Tuple[Optional[types.ModuleType], str]:
    """Load catalog.py as it was at *ref*, as a throwaway module.

    This execs the repo's OWN file from its OWN history — the same trust level as
    importing it normally. It is not a sandbox for third-party code, and must never be
    pointed at a ref from an untrusted fork.
    """
    try:
        p = subprocess.run(["git", "show", f"{ref}:{CATALOG_PATH}"],
                           cwd=_ROOT, capture_output=True, text=True, timeout=30)
    except Exception as e:
        return None, f"could not read {CATALOG_PATH} at {ref}: {type(e).__name__}: {e}"
    if p.returncode != 0:
        return None, (p.stderr or "").strip()[:300] or f"unknown git ref {ref!r}"
    mod = types.ModuleType(f"catalog_at_{ref.replace('/', '_')}")
    try:
        exec(compile(p.stdout, f"<catalog@{ref}>", "exec"), mod.__dict__)
    except Exception as e:
        return None, f"{CATALOG_PATH} at {ref} did not import: {type(e).__name__}: {e}"
    for attr in ("all_nodes", "WF_EDGES"):
        if not hasattr(mod, attr):
            return None, f"{CATALOG_PATH} at {ref} has no {attr} — too old to compare"
    return mod, ""


def spine_ir_at_ref(ref: str, built_ids: FrozenSet[str] = frozenset()
                    ) -> Tuple[Optional[Dict[str, Any]], str]:
    """The spine IR at *ref*, built through the same converter as the current one."""
    from automation.workflow.archify_ir import build_ir
    mod, why = catalog_at_ref(ref)
    if mod is None:
        return None, why
    try:
        return build_ir(built_ids, cat=mod), ""
    except Exception as e:
        return None, f"could not build the spine at {ref}: {type(e).__name__}: {e}"


def comparable_refs() -> list:
    """Refs whose tree actually contains catalog.py, newest-committed first.

    Offering a free-text box invited two failures that look identical to a user
    but are not: a ref from the wrong repository (the apps under test have their
    own branches, and this reads the PLATFORM repo), and a real platform ref that
    predates catalog.py. Listing only refs that can be compared removes both.
    """
    try:
        p = subprocess.run(
            ["git", "for-each-ref", "--sort=-committerdate",
             "--format=%(refname:short)", "refs/heads", "refs/remotes"],
            cwd=_ROOT, capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        logger.warning("could not list refs: %s", e)
        return []
    if p.returncode != 0:
        return []

    refs, seen = [], set()
    for name in p.stdout.split():
        short = name[len("origin/"):] if name.startswith("origin/") else name
        if short in seen or short == "HEAD":
            continue
        ok = subprocess.run(["git", "cat-file", "-e", f"{name}:{CATALOG_PATH}"],
                            cwd=_ROOT, capture_output=True, timeout=10)
        if ok.returncode == 0:
            seen.add(short)
            refs.append(name)
    return refs
