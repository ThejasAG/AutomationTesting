"""Cross-app dependency analysis.

Apps in a suite (consumer / business / superadmin / backend) are coupled through
the HTTP endpoints they share. A change in one app can therefore break another
with no code-level link between them. This module discovers that coupling by
scanning each app's source for the endpoints it calls, building a bipartite
app -> endpoint graph, and using it to answer: "given these changed files, which
OTHER apps must be re-tested?"
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# Directories that never contain first-party source.
_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "Pods", "build",
    "DerivedData", "dist", ".next", "vendor", ".pytest_cache", "coverage",
}

# Source extensions per app type.
_EXTENSIONS: Dict[str, Tuple[str, ...]] = {
    "ios_swift": (".swift", ".m", ".mm"),
    "react_native": (".js", ".jsx", ".ts", ".tsx"),
    "web_react": (".js", ".jsx", ".ts", ".tsx"),
    "python": (".py",),
}

# HTTP verbs we recognise, longest first so PATCH/DELETE win over any prefix.
_VERBS = ("DELETE", "PATCH", "POST", "PUT", "GET", "HEAD", "OPTIONS")

# Import lines name the HTTP library but never call it.
_IMPORT_RE = re.compile(r"^\s*(?:import|#import|from|@import|require)\b|^\s*use\s")

# A commented-out request is not a call site. Covers // and /* (JS, Swift, Java)
# and # (Python). Deliberately only matches a comment that STARTS the line — a
# trailing comment on a real call, `Axios.get('/x')  // TODO`, is still a call.
_COMMENT_RE = re.compile(r"^\s*(?://|/\*|\*|#)")

# A path that looks like an API route: starts with / and runs to the closing
# quote. The body must accept ANY non-quote, non-space character — Swift
# interpolation `/orders/\(id)/status` and JS templates `/orders/${id}/status`
# contain backslashes, parens and braces, and an over-narrow character class
# silently misses every interpolated route (i.e. most of the interesting ones).
_PATH_RE = re.compile(r"""['"`](/[^'"`\s]*)['"`]""")

# A JS template literal whose base URL is interpolated:
#   `${API_URL}/appointments/api/makeReservation?query=x`
# _PATH_RE cannot see these — it requires the quote to be IMMEDIATELY followed by
# '/', but here the backtick is followed by '${'. Apps that keep their host in a
# constant (rather than passing a bare '/path' to a preconfigured client) write
# EVERY call this way, so missing this pattern hides an entire app's endpoints and
# it lands in the graph with no edges at all.
_TEMPLATE_PATH_RE = re.compile(r"`(?:\$\{[^}]*\})+(/[^`\s]*)`")

# Full URLs — we keep only the path portion so the same endpoint matches across
# apps regardless of which base host each one is configured with.
_URL_RE = re.compile(r"""['"`]https?://[^/'"`\s]+(/[^'"`\s]*)['"`]""")


# ── Per-language call-site patterns ───────────────────────────────────────────
# Each entry maps a regex to the HTTP method it implies (None = infer from the
# line, e.g. from an explicit `method = "POST"` or an .httpMethod assignment).

_PATTERNS: Dict[str, List[Tuple[re.Pattern, Optional[str]]]] = {
    "ios_swift": [
        (re.compile(r"\bAF\.request\b"), None),
        # NOT a bare `\bAlamofire\b` — that matches `import Alamofire` and every
        # type annotation, turning imports into phantom call sites.
        (re.compile(r"\bAlamofire\.(?:request|upload|download)\b"), None),
        (re.compile(r"\bURLSession\b"), None),
        (re.compile(r"\bURLRequest\s*\("), None),
        (re.compile(r"\bURL\s*\(\s*string\s*:"), None),
        # .method = .post  /  httpMethod = "POST"
        (re.compile(r"\.method\s*[:=]\s*\.(get|post|put|patch|delete)\b", re.I), None),
        (re.compile(r"httpMethod\s*=\s*['\"](GET|POST|PUT|PATCH|DELETE)['\"]", re.I), None),
    ],
    "react_native": [
        (re.compile(r"\bfetch\s*\("), None),
        (re.compile(r"\baxios\.(get|post|put|patch|delete)\b", re.I), None),
        (re.compile(r"\baxios\s*\("), None),
        (re.compile(r"\bapi\.(get|post|put|patch|delete)\b", re.I), None),
        (re.compile(r"\bapiClient\.(get|post|put|patch|delete)\b", re.I), None),
        (re.compile(r"\bapiClient\s*\("), None),
        (re.compile(r"\bapisauce\b", re.I), None),
    ],
    "python": [
        (re.compile(r"\brequests\.(get|post|put|patch|delete)\b", re.I), None),
        (re.compile(r"\bhttpx\.(get|post|put|patch|delete)\b", re.I), None),
        (re.compile(r"\b(?:client|session)\.(get|post|put|patch|delete)\s*\(", re.I), None),
    ],
}
_PATTERNS["web_react"] = _PATTERNS["react_native"]

# Method captured directly from the call itself, e.g. axios.post(...) / api.get(...)
_INLINE_METHOD_RE = re.compile(
    r"\b(?:axios|api|apiClient|client|session|requests|httpx)\.(get|post|put|patch|delete)\b",
    re.I,
)
# Swift/Alamofire: method: .post
_SWIFT_METHOD_RE = re.compile(r"\.method\s*[:=]\s*\.(get|post|put|patch|delete)\b", re.I)
_SWIFT_HTTPMETHOD_RE = re.compile(
    r"httpMethod\s*=\s*['\"](GET|POST|PUT|PATCH|DELETE)['\"]", re.I
)
# JS fetch: { method: 'POST' }
_JS_METHOD_RE = re.compile(r"method\s*:\s*['\"](GET|POST|PUT|PATCH|DELETE)['\"]", re.I)


def _normalize_path(path: str) -> Optional[str]:
    """Canonicalise a route so the same endpoint matches across apps.

    Interpolated segments become `{id}`, so `/api/v1/orders/\\(orderId)` (Swift),
    `/api/v1/orders/${id}` (JS) and `/api/v1/orders/{id}` (Python) all collapse to
    one node. Without this the graph would never link two apps together.
    """
    if not path or not path.startswith("/"):
        return None

    # Collapse interpolation BEFORE stripping the query string. Order matters:
    # JS optional chaining inside a template — ${this.props?.auth?.user?.id} —
    # contains '?', so splitting on '?' first truncates the path mid-expression
    # ("/restaurants/${this.props"). The same route written two different ways in
    # two apps then yields two different endpoint nodes, and the graph reports no
    # cross-app link at all. This is not hypothetical: it produced 0 shared
    # endpoints between Consumer and Business.
    #
    # Swift string interpolation \(expr) and JS template ${expr}.
    path = re.sub(r"\\\([^)]*\)", "{id}", path)
    path = re.sub(r"\$\{[^}]*\}", "{id}", path)

    # Drop query strings and trailing slashes.
    path = path.split("?", 1)[0].rstrip("/") or "/"
    # Python f-string / format placeholders that are not already {id}.
    path = re.sub(r"\{[A-Za-z_][A-Za-z0-9_]*\}", "{id}", path)
    # Bare numeric segments are almost certainly ids.
    path = re.sub(r"/\d+(?=/|$)", "/{id}", path)

    # Ignore obvious non-API paths (asset/file references).
    if re.search(r"\.(png|jpg|jpeg|gif|svg|css|scss|html|json|plist|xml)$", path, re.I):
        return None
    if not re.search(r"[A-Za-z]", path):
        return None

    return path


def _method_for_line(line: str, app_type: str, window: str) -> str:
    """Best-effort HTTP verb for a call site. Defaults to GET."""
    for regex in (_INLINE_METHOD_RE, _SWIFT_METHOD_RE, _SWIFT_HTTPMETHOD_RE, _JS_METHOD_RE):
        m = regex.search(line) or regex.search(window)
        if m:
            return m.group(1).upper()

    upper = line.upper()
    for verb in _VERBS:
        if re.search(rf"\b{verb}\b", upper):
            return verb
    return "GET"


class CrossAppDependencyAnalyzer:
    """Discovers which HTTP endpoints each app calls, and what that couples."""

    # How many lines after a call site to look at when hunting for the path/method.
    LOOKAHEAD = 4

    @staticmethod
    def _paths_in(text: str) -> List[str]:
        """Candidate route paths in *text*. Full URLs win over bare paths.

        A bare '/path' and an interpolated `${BASE}/path` are the same tier: both
        are a route relative to a host configured elsewhere. They cannot both
        match the same literal, so collecting both never double-counts.
        """
        paths = [m.group(1) for m in _URL_RE.finditer(text)]
        if not paths:
            paths = [m.group(1) for m in _PATH_RE.finditer(text)]
            paths += [m.group(1) for m in _TEMPLATE_PATH_RE.finditer(text)]
        return paths

    # ── 1. Scan one app ──────────────────────────────────────────────────────

    def scan_app_for_api_calls(
        self, app_path: str, app_type: str
    ) -> Dict[str, List[str]]:
        """Scan an app's source for API endpoint usage.

        Returns ``{"GET /api/v1/orders": ["OrderService.swift:45"], ...}`` —
        endpoint -> the call sites that reference it.
        """
        results: Dict[str, List[str]] = {}

        if not app_path or not os.path.isdir(app_path):
            logger.warning(f"Cannot scan '{app_path}': not a directory")
            return results

        extensions = _EXTENSIONS.get(app_type)
        patterns = _PATTERNS.get(app_type)
        if not extensions or not patterns:
            logger.warning(f"Unknown app_type '{app_type}' — nothing scanned")
            return results

        app_root = os.path.abspath(app_path)

        for root, dirs, files in os.walk(app_root):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]

            for filename in files:
                if not filename.endswith(extensions):
                    continue

                full = os.path.join(root, filename)
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                except OSError as e:
                    logger.debug(f"Skipping {full}: {e}")
                    continue

                rel = os.path.relpath(full, app_root)

                for idx, line in enumerate(lines):
                    stripped = line.strip()

                    # Imports mention the HTTP library but call nothing.
                    if _IMPORT_RE.match(stripped):
                        continue

                    # Commented-out code is not a call site. Dead code is *littered*
                    # with old requests — the Business app's only "/restaurants/{id}"
                    # references are all `// Axios.get(...)` — and counting them
                    # invents endpoints the app never actually calls, which then show
                    # up as false cross-app dependencies and drag in test suites that
                    # did not need to run.
                    if _COMMENT_RE.match(stripped):
                        continue

                    if not any(rx.search(line) for rx, _ in patterns):
                        continue

                    # PATHS: prefer the call line itself. Only widen to a
                    # lookahead window when the call actually CONTINUES onto the
                    # following lines (unbalanced parens) — a multi-line request
                    # builder. Looking ahead unconditionally attributes the *next*
                    # call's URL to this line, inventing endpoints that do not
                    # exist; that is especially wrong for calls that pass a URL
                    # variable rather than a literal, e.g.
                    # `URLSession.shared.dataTask(with: url!)`.
                    paths = self._paths_in(line)
                    if not paths and line.count("(") > line.count(")"):
                        paths = self._paths_in(
                            "".join(lines[idx: idx + self.LOOKAHEAD])
                        )

                    # METHOD: must always look ahead — Swift sets it on a later
                    # line (`req.httpMethod = "PATCH"`), so a same-line-only
                    # search would silently label every URLRequest as GET.
                    method_window = "".join(lines[idx: idx + self.LOOKAHEAD])

                    for raw in paths:
                        endpoint_path = _normalize_path(raw)
                        if not endpoint_path:
                            continue
                        method = _method_for_line(line, app_type, method_window)
                        key = f"{method} {endpoint_path}"
                        site = f"{rel}:{idx + 1}"
                        results.setdefault(key, [])
                        if site not in results[key]:
                            results[key].append(site)

        logger.info(
            f"Scanned {app_path} ({app_type}): {len(results)} endpoint(s) discovered"
        )
        return results

    # ── 2. Build the graph ───────────────────────────────────────────────────

    def build_dependency_graph(self, apps: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Build the bipartite app -> endpoint graph for a set of apps.

        *apps*: ``[{"name", "path", "type", "test_suite"}]``
        """
        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, Any]] = []
        seen_nodes: Set[str] = set()
        endpoint_index: Dict[str, Dict[str, Any]] = {}

        def add_node(node_id: str, **attrs) -> None:
            if node_id in seen_nodes:
                return
            seen_nodes.add(node_id)
            nodes.append({"id": node_id, **attrs})

        for app in apps:
            name = app.get("name")
            if not name:
                continue

            add_node(
                name,
                type="app",
                app_type=app.get("type"),
                test_suite=app.get("test_suite"),
                app_role=app.get("app_role"),
                path=app.get("path"),
                project_id=app.get("project_id"),
            )

            calls = self.scan_app_for_api_calls(app.get("path", ""), app.get("type", ""))

            for endpoint, sites in calls.items():
                add_node(endpoint, type="endpoint")
                entry = endpoint_index.setdefault(
                    endpoint, {"endpoint": endpoint, "apps": [], "call_sites": {}}
                )
                if name not in entry["apps"]:
                    entry["apps"].append(name)
                entry["call_sites"][name] = sites

                for site in sites:
                    file_part, _, line_part = site.rpartition(":")
                    edges.append({
                        "from": name,
                        "to": endpoint,
                        "file": file_part or site,
                        "line": int(line_part) if line_part.isdigit() else 0,
                    })

        # An endpoint used by more than one app is where cross-app risk lives.
        shared = [e for e, v in endpoint_index.items() if len(v["apps"]) > 1]

        graph = {
            "nodes": nodes,
            "edges": edges,
            "endpoints": endpoint_index,
            "shared_endpoints": sorted(shared),
            "stats": {
                "apps": sum(1 for n in nodes if n["type"] == "app"),
                "endpoints": sum(1 for n in nodes if n["type"] == "endpoint"),
                "shared_endpoints": len(shared),
                "edges": len(edges),
            },
        }
        logger.info(
            f"Dependency graph: {graph['stats']['apps']} apps, "
            f"{graph['stats']['endpoints']} endpoints, "
            f"{graph['stats']['shared_endpoints']} shared"
        )
        return graph

    # ── 3. Cross-app impact ──────────────────────────────────────────────────

    def find_cross_app_impact(
        self, changed_files: List[str], graph: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Given changed files, find every app that must be re-tested.

        The chain is: changed file -> the app that owns it -> the endpoints that
        file touches -> every OTHER app calling those endpoints.
        """
        edges = graph.get("edges", [])
        nodes = graph.get("nodes", [])
        endpoints_index = graph.get("endpoints", {})

        app_nodes = {n["id"]: n for n in nodes if n.get("type") == "app"}

        # Match a changed file to edges by basename, so a repo-relative path from
        # a webhook ("src/services/OrderService.swift") still matches a call site
        # recorded relative to the app root.
        changed_basenames = {os.path.basename(f) for f in changed_files if f}

        directly_affected_apps: Set[str] = set()
        affected_endpoints: Set[str] = set()

        for edge in edges:
            edge_file = edge.get("file", "")
            if not edge_file:
                continue
            if os.path.basename(edge_file) in changed_basenames or any(
                edge_file in cf or cf in edge_file for cf in changed_files if cf
            ):
                directly_affected_apps.add(edge["from"])
                affected_endpoints.add(edge["to"])

        # Every other app calling an affected endpoint is transitively impacted.
        cross_app_impact: List[Dict[str, Any]] = []
        seen_apps: Set[str] = set()

        for endpoint in sorted(affected_endpoints):
            entry = endpoints_index.get(endpoint, {})
            for app_name in entry.get("apps", []):
                if app_name in directly_affected_apps or app_name in seen_apps:
                    continue
                seen_apps.add(app_name)

                node = app_nodes.get(app_name, {})
                suite = node.get("test_suite")
                cross_app_impact.append({
                    "app": app_name,
                    "reason": f"uses {endpoint}",
                    "endpoint": endpoint,
                    "tests": [suite] if suite else [],
                    "test_suite": suite,
                    "call_sites": entry.get("call_sites", {}).get(app_name, []),
                })

        # Suites to run: the directly-changed apps plus every impacted one.
        run_suites: List[str] = []
        for app_name in list(directly_affected_apps) + [i["app"] for i in cross_app_impact]:
            suite = app_nodes.get(app_name, {}).get("test_suite")
            if suite and suite not in run_suites:
                run_suites.append(suite)

        return {
            "changed_files": list(changed_files),
            "directly_affected_app": (
                sorted(directly_affected_apps)[0] if directly_affected_apps else None
            ),
            "directly_affected_apps": sorted(directly_affected_apps),
            "affected_endpoints": sorted(affected_endpoints),
            "cross_app_impact": cross_app_impact,
            "total_apps_affected": len(cross_app_impact),
            "run_all_test_suites": sorted(run_suites),
        }


cross_app_analyzer = CrossAppDependencyAnalyzer()
