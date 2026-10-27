"""Which code a flow run executed — so a later edit makes only the passes it can
affect stale.

MEASURED 2026-10-27 (session cf6bd414): two new drafts passed at 16:18 and 16:21;
the agent then fixed, and finally removed, a third draft in the same file. The gate
compared every run with the LAST modification of any changed file, so neither pass
counted and the two good scenarios were refused. A batch of ten drafts would have
had to re-run all ten after each fix.

A run's fingerprint is taken as it starts: a hash of each segment's steps, and one
per engine unit of cross_app_flows.py (each function, each `_handle_special` branch,
each FlowRunner attribute, each module constant) plus one for the rest of the device
code. Its pass still counts for its flow when the flow's steps are the same now and
no unit that changed since reaches the flow (impact.py's call graph). Fingerprints
live outside the session folder, where a session cannot write.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Set

from automation.claude_code import impact

DIR = os.path.expanduser("~/.vya-platform/agent-run-code")
DEVICE_DIRS = ("automation/scenarios", "automation/intelligence", "automation/projects")
KEEP_DAYS = 14

# Data, not engine: the flows themselves (compared by their steps) and the editor's menu.
_DATA = {"FLOWS", "STEP_CATALOG"}
_STEP_BLOCK = re.compile(r"^_[CWK]_[A-Z0-9_]+$")      # _C_BOOK_PREFIX, _W_ASSIGN_SEND, …
_REST = "<rest>"


def _sha(x: Any) -> str:
    raw = x if isinstance(x, str) else json.dumps(x, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _names(node: ast.AST) -> List[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return [t.id for t in targets if isinstance(t, ast.Name)]


def _branch_tokens(st: ast.stmt) -> Set[str]:
    if not isinstance(st, ast.If):
        return set()
    return {c.value for c in ast.walk(st.test)
            if isinstance(c, ast.Constant) and isinstance(c.value, str) and c.value.startswith("@")}


def units(src: str) -> Dict[str, str]:
    """{unit: hash} for cross_app_flows.py."""
    tree = ast.parse(src)
    out: Dict[str, str] = {}
    rest: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = _sha(ast.dump(node))
        elif isinstance(node, ast.ClassDef) and node.name == "FlowRunner":
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == "_handle_special":
                    branches: Dict[str, List[str]] = {}
                    for st in m.body:
                        key = "|".join(sorted(_branch_tokens(st))) or _REST
                        branches.setdefault(key, []).append(ast.dump(st))
                    for key, dumps in branches.items():
                        out[f"branch:{key}"] = _sha("".join(dumps))
                elif isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[m.name] = _sha(ast.dump(m))
                elif isinstance(m, (ast.Assign, ast.AnnAssign)):
                    for n in _names(m):
                        out[f"attr:{n}"] = _sha(ast.dump(m))
                elif not isinstance(m, ast.Expr):
                    rest.append(ast.dump(m))
        elif isinstance(node, ast.ClassDef):
            out[f"class:{node.name}"] = _sha(ast.dump(node))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for n in _names(node):
                if n not in _DATA and not _STEP_BLOCK.match(n):
                    out[f"const:{n}"] = _sha(ast.dump(node))
        elif not isinstance(node, ast.Expr):
            rest.append(ast.dump(node))
    out[_REST] = _sha("".join(rest))
    return out


def other_code(root: str) -> str:
    """One hash for the rest of the device code (idb driver, scenario runner, …)."""
    h = hashlib.sha1()
    for d in DEVICE_DIRS:
        for base, dirs, files in sorted(os.walk(os.path.join(root, d))):
            dirs[:] = sorted(x for x in dirs if x != "__pycache__")
            for n in sorted(files):
                p = os.path.join(base, n)
                rel = os.path.relpath(p, root)
                if not n.endswith(".py") or rel == impact.FLOWS_FILE:
                    continue
                try:
                    with open(p, "rb") as f:
                        h.update(rel.encode() + b"\0" + f.read())
                except OSError:
                    continue
    return h.hexdigest()[:12]


def segment_hashes(segments: Iterable[Any]) -> List[str]:
    """One hash per segment, from its steps ([steps] or {'steps': [...]})."""
    return [_sha(list((s.get("steps") if isinstance(s, dict) else s) or [])) for s in segments]


def take(root: str, segments: Iterable[Any], start: int = 0,
         resumed_from: Optional[str] = None) -> Dict[str, Any]:
    with open(os.path.join(root, impact.FLOWS_FILE), encoding="utf-8") as f:
        src = f.read()
    return {"segments": segment_hashes(segments), "units": units(src), "other": other_code(root),
            "start": int(start or 0), "resumed_from": resumed_from, "at": time.time()}


def record(run_id: str, fp: Dict[str, Any]) -> None:
    os.makedirs(DIR, exist_ok=True)
    with open(os.path.join(DIR, f"{run_id}.json"), "w") as f:
        json.dump(fp, f)
    cutoff = time.time() - KEEP_DAYS * 86400
    for n in os.listdir(DIR):
        p = os.path.join(DIR, n)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


def load(run_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not run_id or not re.fullmatch(r"[\w-]{1,64}", run_id):
        return None
    try:
        with open(os.path.join(DIR, f"{run_id}.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ── the gate's question ──────────────────────────────────────────────────────

def _referencing(src: str, name: str) -> Set[str]:
    """Functions (and `_handle_special`) whose code names `name`."""
    out: Set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any((isinstance(n, ast.Name) and n.id == name)
                   or (isinstance(n, ast.Attribute) and n.attr == name) for n in ast.walk(node)):
                out.add(node.name)
    return out


def reaches(changed: Set[str], steps: List[str], src: str, aliases: Dict[str, str]) -> bool:
    """Does a change to these units reach a flow with these steps?"""
    if not changed:
        return False
    toks: Set[str] = set()
    funcs: Set[str] = set()
    for k in changed:
        if k == _REST or k == f"branch:{_REST}" or k.startswith("class:"):
            return True
        if k.startswith("branch:"):
            toks |= set(k[len("branch:"):].split("|"))
        elif k.startswith(("attr:", "const:")):
            refs = _referencing(src, k.split(":", 1)[1])
            if "_handle_special" in refs:
                return True
            funcs |= refs
        else:
            funcs.add(k)
    run = [aliases.get(s.strip(), s.strip()) for s in steps]
    if any(s.startswith(t) for s in run for t in toks):
        return True
    return bool(funcs) and bool(impact.flows_for_functions({"f": steps}, src, funcs, aliases))


def is_current(fp: Dict[str, Any], segments_now: List[str], units_now: Dict[str, str],
               other_now: str, steps: List[str], src: str, aliases: Dict[str, str],
               upto: Optional[int] = None) -> bool:
    """Did this run execute the code the flow has now, for segments [start:upto]?"""
    start = int(fp.get("start") or 0)
    end = len(segments_now) if upto is None else upto
    if fp.get("other") != other_now or (fp.get("segments") or [])[start:end] != segments_now[start:end]:
        return False
    then = fp.get("units") or {}
    changed = {k for k in set(then) | set(units_now) if then.get(k) != units_now.get(k)}
    return not reaches(changed, steps, src, aliases)


def run_is_current(run_id: str, segments_now: List[str], units_now: Dict[str, str],
                   other_now: str, steps: List[str], src: str, aliases: Dict[str, str]) -> Optional[bool]:
    """True/False for a run with a fingerprint (a resumed run: the runs it continued
    must be current for the segments they ran); None for one without."""
    fp = load(run_id)
    if fp is None:
        return None
    upto: Optional[int] = None
    for _ in range(5):
        if not is_current(fp, segments_now, units_now, other_now, steps, src, aliases, upto):
            return False
        if not fp.get("resumed_from") or not int(fp.get("start") or 0):
            return True
        upto = int(fp["start"])
        fp = load(fp["resumed_from"])
        if fp is None:
            return False
    return False
