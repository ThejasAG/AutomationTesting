"""What the apps contain, and how much of it the tests touch — computed, not guessed.

Rule-based (no AI, no cost), from the app checkouts:
  routes   every navigator entry: `name="Menu"  component={Menu}` in App/Navigation
           (and MobileNavigation), resolved through its import — and through barrel
           files (`export {default as Menu} from './Menu'`) — to the screen's file
  ids      every literal testID / accessibilityLabel in the screen file and the
           local files it imports (template ids like `${title}Card` become "*Card")

Coverage: a screen counts as exercised when one of its ids appears in what the
automation actually drives: the flows' steps, the flow runner's own source
(handlers tap ids like AssignTableBtn), saved scenarios and learned locators.
That is a floor, not a proof: it shows where tests certainly go, and so where
they certainly don't.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Set

from automation.agentic import sources

_SRC_EXT = (".js", ".jsx", ".ts", ".tsx")
_ROUTE = re.compile(r"""name\s*=\s*\{?\s*["']([^"']+)["']\s*\}?[\s\S]{0,200}?component\s*=\s*\{\s*([A-Za-z_][\w.]*)\s*\}""")
_ID = re.compile(r"""(?:testID|accessibilityLabel)\s*=\s*\{?\s*["']([A-Za-z0-9_\-]+)["']""")
_TPL = re.compile(r"""(?:testID|accessibilityLabel)\s*=\s*\{\s*`([^`]+)`""")
_REL_IMPORT = re.compile(r"""(?:import|export)\s[^;]*?from\s+["'](\.[^"']+)["']""")
_TOKEN = re.compile(r"[A-Za-z0-9_\-]+")


def _import_re(name: str) -> re.Pattern:
    n = re.escape(name)
    return re.compile(r"import\s+(?:\{[^}]*\b" + n + r"\b[^}]*\}|" + n + r")\s+from\s+[\"']([^\"']+)[\"']")


def _reexport_re(name: str) -> re.Pattern:
    n = re.escape(name)
    return re.compile(r"export\s*\{[^}]*\bdefault\s+as\s+" + n + r"\b[^}]*\}\s*from\s*[\"']([^\"']+)[\"']")


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _resolve(base_dir: str, spec: str) -> Optional[str]:
    if not spec.startswith("."):
        return None
    p = os.path.normpath(os.path.join(base_dir, spec))
    for cand in [p] + [p + e for e in _SRC_EXT] + [os.path.join(p, "index" + e) for e in _SRC_EXT]:
        if os.path.isfile(cand):
            return cand
    return None


def _follow(path: Optional[str], name: str, depth: int = 0) -> Optional[str]:
    """Walk through barrel files (index.js re-exports) to the real module."""
    if not path or depth > 4:
        return path
    m = _reexport_re(name).search(_read(path))
    if not m:
        return path
    return _follow(_resolve(os.path.dirname(path), m.group(1)), name, depth + 1)


def _screen_files(entry: str, repo: str) -> List[str]:
    """The screen file plus the local files it imports (its own parts), not the
    app-wide barrels — one level deep."""
    files = [entry]
    for spec in _REL_IMPORT.findall(_read(entry)):
        dep = _resolve(os.path.dirname(entry), spec)
        if not dep or dep in files:
            continue
        rel = os.path.relpath(dep, repo)
        if os.path.basename(dep).startswith("index.") and rel.count(os.sep) <= 2:
            continue          # App/Components/index.js and the like: shared, not this screen's
        files.append(dep)
    return files


def _ids_in(paths: List[str]) -> Set[str]:
    out: Set[str] = set()
    for p in paths:
        text = _read(p)
        out.update(_ID.findall(text))
        for t in _TPL.findall(text):
            pat = re.sub(r"\$\{[^}]*\}", "*", t)
            if pat.strip("*"):
                out.add(pat)
    return out


def routes(app: str) -> List[Dict[str, Any]]:
    repo = sources.app_repo(app)
    if not repo:
        return []
    out, seen = [], set()
    for nav in ("Navigation", "MobileNavigation"):
        nav_dir = os.path.join(repo, "App", nav)
        if not os.path.isdir(nav_dir):
            continue
        for fn in sorted(os.listdir(nav_dir)):
            if not fn.endswith(_SRC_EXT):
                continue
            path = os.path.join(nav_dir, fn)
            text = _read(path)
            for route, comp in _ROUTE.findall(text):
                name = comp.split(".")[0]
                m = _import_re(name).search(text)
                entry = _follow(_resolve(nav_dir, m.group(1)) if m else None, name)
                key = (route, entry or name)
                if key in seen:
                    continue
                seen.add(key)
                files = _screen_files(entry, repo) if entry else []
                out.append({
                    "route": route, "component": comp, "navigator": f"App/{nav}/{fn}",
                    "file": os.path.relpath(entry, repo) if entry else None,
                    "ids": sorted(_ids_in(files)),
                })
    return out


def _automation_tokens() -> Set[str]:
    """Every identifier-like token in what the automation drives."""
    from automation.database.config import SessionLocal
    from automation.database.models import SavedScenario
    from automation.scenarios.cross_app_flows import list_flows
    parts = [_read(os.path.join(sources.ROOT, "automation", "scenarios", "cross_app_flows.py")),
             _read(os.path.join(sources.KNOWLEDGE, "learned_locators.json"))]
    for f in list_flows():
        parts += [st for s in f["segments"] for st in s["steps"]]
    with SessionLocal() as db:
        for s in db.query(SavedScenario).all():
            parts += list(s.steps or [])
    return set(_TOKEN.findall("\n".join(parts)))


def _covered(identifier: str, tokens: Set[str]) -> bool:
    if "*" not in identifier:
        return identifier in tokens
    rx = re.compile("^" + re.escape(identifier).replace(r"\*", r"[A-Za-z0-9_\-]*") + "$")
    return any(rx.match(t) for t in tokens)


SHARED_AT = 5          # an id on this many screens belongs to a shared component


def _distinctive(identifier: str) -> bool:
    """`*Btn` matches every button; a pattern only identifies a screen when its
    fixed text does."""
    return len(identifier.replace("*", "")) >= 6


def build() -> Dict[str, Any]:
    tokens = _automation_tokens()
    apps: Dict[str, Any] = {}
    shared_out: Dict[str, List[str]] = {}
    for app in ("consumer", "business"):
        rs = routes(app)
        freq: Dict[str, int] = {}
        for r in rs:
            for i in r["ids"]:
                freq[i] = freq.get(i, 0) + 1
        shared = {i for i, n in freq.items() if n >= SHARED_AT}
        for r in rs:
            own = [i for i in r["ids"] if i not in shared and _distinctive(i)]
            r["shared_ids"] = len(r["ids"]) - len(own)
            r["ids"] = own
            r["covered_ids"] = [i for i in own if _covered(i, tokens)]
            r["covered"] = bool(r["covered_ids"])
        shared_out[app] = sorted(shared)
        all_ids = {i for r in rs for i in r["ids"]}
        cov_ids = {i for r in rs for i in r["covered_ids"]}
        apps[app] = {"routes": rs, "screens": len(rs),
                     "screens_covered": sum(1 for r in rs if r["covered"]),
                     "ids": len(all_ids), "ids_covered": len(cov_ids)}
    return {"apps": apps, "shared_ids": shared_out}


def compact(inv: Dict[str, Any], max_ids: int = 25) -> str:
    """The inventory as a short text block for Claude."""
    lines = []
    for app, a in inv["apps"].items():
        lines.append(f"\n## {app} app — {a['screens_covered']}/{a['screens']} screens touched by tests, "
                     f"{a['ids_covered']}/{a['ids']} element ids used")
        for r in a["routes"]:
            mark = "COVERED" if r["covered"] else "NOT COVERED"
            ids = r["ids"][:max_ids]
            more = f" …(+{len(r['ids']) - max_ids})" if len(r["ids"]) > max_ids else ""
            used = f"; tests use: {', '.join(r['covered_ids'][:8])}" if r["covered_ids"] else ""
            lines.append(f"- [{mark}] route '{r['route']}' → {r['file'] or r['component']}"
                         f"  ids: {', '.join(ids) or '(none found)'}{more}{used}")
    return "\n".join(lines)
