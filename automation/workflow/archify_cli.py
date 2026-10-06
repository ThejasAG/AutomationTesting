"""Thin wrapper around the vendored Archify CLI.

Archify is a Node tool (node >= 18) vendored at vendor/archify. It has no npm
dependencies — the bin uses node builtins only — so there is nothing to install.

Every entry point here is NON-FATAL: the diagram is a nice-to-have on top of the
existing Workflow page, and a missing node binary must never take an API route down.
Failures come back as (None, reason) for the caller to surface as a note.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("archify")

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BIN = os.path.join(_ROOT, "vendor", "archify", "bin", "archify.mjs")
#: Rendering the spine takes ~2s; a slow CI box gets headroom without hanging a request.
TIMEOUT = 120


def available() -> Tuple[bool, str]:
    """(usable, why-not). Checked before every call so the reason is specific."""
    if not os.path.exists(BIN):
        return False, f"archify is not vendored at {BIN}"
    if not shutil.which("node"):
        return False, "node is not on PATH (archify needs node >= 18)"
    return True, ""


def _run(args: list) -> Tuple[bool, str]:
    ok, why = available()
    if not ok:
        return False, why
    try:
        p = subprocess.run(["node", BIN, *args], capture_output=True, text=True,
                           timeout=TIMEOUT)
        return p.returncode == 0, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return False, f"archify timed out after {TIMEOUT}s"
    except Exception as e:                      # never propagate into a request
        logger.warning("archify failed: %s", e)
        return False, f"{type(e).__name__}: {e}"


def render(ir: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """IR -> self-contained HTML string. (None, reason) when it could not be produced."""
    tmp = tempfile.mkdtemp(prefix="archify-")
    src, out = os.path.join(tmp, "ir.json"), os.path.join(tmp, "out.html")
    try:
        with open(src, "w") as f:
            json.dump(ir, f)
        ok, log = _run(["render", ir.get("diagram_type", "architecture"), src, out])
        if not ok or not os.path.exists(out):
            return None, log.strip()[:600] or "archify produced no output"
        with open(out) as f:
            return f.read(), ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def compare(base_ir: Dict[str, Any], head_ir: Dict[str, Any]
            ) -> Tuple[Optional[Dict[str, Any]], Optional[str], str]:
    """Before/Delta/After for two spine snapshots.

    Returns (summary, html, reason). The summary carries exact added/removed/changed
    counts — that is what a PR comment needs; the HTML is the interactive artifact.
    """
    tmp = tempfile.mkdtemp(prefix="archify-cmp-")
    b, h = os.path.join(tmp, "base.json"), os.path.join(tmp, "head.json")
    out = os.path.join(tmp, "delta.html")
    try:
        with open(b, "w") as f:
            json.dump(base_ir, f)
        with open(h, "w") as f:
            json.dump(head_ir, f)
        ok, log = _run(["compare", "architecture", b, h, out, "--json"])
        summary = None
        for line in (log or "").splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    summary = json.loads(line)
                    break
                except Exception:
                    pass
        if summary is None:                      # --json prints one object; may be multiline
            try:
                summary = json.loads(log[log.index("{"):log.rindex("}") + 1])
            except Exception:
                summary = None
        if not ok:
            reason = (summary or {}).get("error") or log.strip()[:600]
            return summary, None, reason
        html = None
        if os.path.exists(out):
            with open(out) as f:
                html = f.read()
        return summary, html, ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
