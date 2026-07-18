"""Hybrid cross-app impact analysis.

Two tools, each covering the other's blind spot:

  Graphify (tree-sitter AST)   -> INTRA-app blast radius.
      Given a changed file, which other files in the SAME app depend on it?
      Exact, deterministic, multi-language (Swift/TS/JS/Python/Kotlin/ObjC).
      It cannot see across apps: separate repos never import each other, so
      Graphify correctly reports zero edges between them.

  CrossAppDependencyAnalyzer   -> INTER-app coupling.
      Apps are joined only by calling the same HTTP endpoint. Graphify has no
      concept of an endpoint node, so this supplies that layer.

The chain:

    changed file
      -> Graphify affected_nodes()      -> every file in the app that depends on it
      -> scan THOSE files for endpoints -> the endpoints the change can touch
      -> find other apps calling them   -> the suites that must also run

Using Graphify for step 2 is what makes this better than the endpoint scan alone:
a change to a file that makes no HTTP call at all (a model, a constant) still
reaches the services that DO call one, because the AST walk gets there.
"""

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from automation.intelligence.cross_app_analyzer import (
    CrossAppDependencyAnalyzer,
    cross_app_analyzer,
)

logger = logging.getLogger(__name__)

# Graphify is an optional dependency: the platform must still start (and the
# endpoint-only path must still work) if it is not installed.
try:
    from graphify.affected import affected_nodes, resolve_seed
    from graphify.build import build_from_json
    from graphify.extract import collect_files
    from graphify.extract import extract as graphify_extract

    GRAPHIFY_AVAILABLE = True
except Exception as _e:  # pragma: no cover - depends on the environment
    GRAPHIFY_AVAILABLE = False
    logger.warning(
        "graphify not importable (%s) — falling back to endpoint-only impact analysis.", _e
    )

# Relations worth walking backwards for "who breaks if this changes".
_AFFECTED_RELATIONS = (
    "calls", "indirect_call", "references", "imports", "imports_from",
    "re_exports", "inherits", "extends", "implements", "uses",
)

# Extensions we will re-scan for endpoints inside the blast radius.
_SOURCE_EXT = (".swift", ".m", ".mm", ".js", ".jsx", ".ts", ".tsx", ".py", ".kt", ".java")


class HybridImpactAnalyzer:
    """Graphify (intra-app) + endpoint graph (inter-app)."""

    def __init__(self, endpoint_analyzer: Optional[CrossAppDependencyAnalyzer] = None):
        self._endpoints = endpoint_analyzer or cross_app_analyzer
        # Graphify graphs are expensive to build — cache per repo path.
        self._graph_cache: Dict[str, Any] = {}
        # endpoint -> call sites, cached per (repo_path, app_type).
        self._scan_cache: Dict[Tuple[str, str], Dict[str, List[str]]] = {}

    # ── App resolution ───────────────────────────────────────────────────────

    def _owning_app(
        self, changed_file: str, app_configs: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Which app does this changed file belong to?

        Prefers a repo_path prefix match. Falls back to locating the file by
        basename inside each repo, because a webhook delivers repo-relative
        paths ("App/Screens/Order.js") that carry no repo prefix at all.
        """
        norm = changed_file.replace("\\", "/")

        # 1. Absolute / prefixed path.
        best: Optional[Dict[str, Any]] = None
        best_len = -1
        for app in app_configs:
            repo = os.path.abspath(app.get("repo_path", ""))
            if not repo:
                continue
            if os.path.abspath(norm).startswith(repo + os.sep) or repo in norm:
                if len(repo) > best_len:
                    best, best_len = app, len(repo)
        if best:
            return best

        # 2. Repo-relative path or bare basename — find the file on disk.
        for app in app_configs:
            repo = app.get("repo_path", "")
            if not repo or not os.path.isdir(repo):
                continue
            if os.path.exists(os.path.join(repo, norm)):
                return app
            if self._find_by_basename(repo, os.path.basename(norm)):
                return app
        return None

    def _find_by_basename(self, repo: str, basename: str) -> Optional[str]:
        for root, dirs, files in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in _SKIP or not d.startswith(".")]
            if basename in files:
                return os.path.join(root, basename)
        return None

    # ── Graphify: intra-app blast radius ─────────────────────────────────────

    def _graph_for(self, repo_path: str):
        if not GRAPHIFY_AVAILABLE:
            return None
        key = os.path.abspath(repo_path)
        if key in self._graph_cache:
            return self._graph_cache[key]
        try:
            files = collect_files(Path(key))
            if not files:
                self._graph_cache[key] = None
                return None
            ex = graphify_extract(files, parallel=True)
            g = build_from_json(ex, directed=True, root=key)
            logger.info(
                "Graphify: %s -> %d nodes, %d edges",
                key, g.number_of_nodes(), g.number_of_edges(),
            )
            self._graph_cache[key] = g
            return g
        except Exception as e:
            logger.error("Graphify failed on %s: %s", key, e)
            self._graph_cache[key] = None
            return None

    def _node_file(self, graph, node_id: str) -> Optional[str]:
        data = graph.nodes.get(node_id, {})
        for key in ("path", "source_file", "file"):
            val = data.get(key)
            if val:
                return str(val)
        return None

    def intra_app_blast_radius(
        self, repo_path: str, changed_files: List[str], depth: int = 2
    ) -> Tuple[List[str], int]:
        """Files in *repo_path* that depend on *changed_files*.

        Returns ``(files, graphify_nodes_affected)``. The changed files
        themselves are always included, so the caller still gets a usable result
        when Graphify is unavailable or cannot resolve a seed.
        """
        radius: Set[str] = set()
        node_count = 0

        for cf in changed_files:
            resolved = self._resolve_changed_file(repo_path, cf)
            if resolved:
                radius.add(resolved)

        graph = self._graph_for(repo_path)
        if graph is None:
            return sorted(radius), 0

        for cf in changed_files:
            seed = None
            # Graphify resolves a seed from a filename, stem, or symbol name.
            for query in (cf, os.path.basename(cf), Path(cf).stem):
                if not query:
                    continue
                try:
                    seed = resolve_seed(graph, query)
                except Exception:
                    seed = None
                if seed:
                    break

            if not seed:
                logger.info("Graphify could not resolve a seed for '%s'", cf)
                continue

            try:
                hits = affected_nodes(
                    graph, seed, relations=_AFFECTED_RELATIONS, depth=depth
                )
            except Exception as e:
                logger.warning("affected_nodes failed for seed %s: %s", seed, e)
                continue

            node_count += len(hits)
            for hit in hits:
                src = self._node_file(graph, hit.node_id)
                if src:
                    radius.add(self._abs(repo_path, src))

        return sorted(radius), node_count

    def _resolve_changed_file(self, repo_path: str, changed: str) -> Optional[str]:
        norm = changed.replace("\\", "/")
        if os.path.isabs(norm) and os.path.exists(norm):
            return norm
        candidate = os.path.join(repo_path, norm)
        if os.path.exists(candidate):
            return candidate
        return self._find_by_basename(repo_path, os.path.basename(norm))

    def _abs(self, repo_path: str, src: str) -> str:
        if os.path.isabs(src):
            return src
        return os.path.join(os.path.abspath(repo_path), src)

    # ── Endpoints touched by the blast radius ────────────────────────────────

    def _scan(self, app: Dict[str, Any]) -> Dict[str, List[str]]:
        """Endpoint -> call sites for an app (cached)."""
        repo = os.path.abspath(app.get("repo_path", ""))
        app_type = app.get("app_type") or app.get("type") or "react_native"
        key = (repo, app_type)
        if key not in self._scan_cache:
            self._scan_cache[key] = self._endpoints.scan_app_for_api_calls(repo, app_type)
        return self._scan_cache[key]

    def endpoints_in_files(
        self, app: Dict[str, Any], files: Set[str]
    ) -> Set[str]:
        """Endpoints whose call sites fall inside *files*.

        Reuses CrossAppDependencyAnalyzer's per-language scanner rather than a
        naive `GET /api/...` regex — real source never contains the verb and the
        path adjacent as literal text; the verb comes from `axios.patch(...)`,
        `AF.request(..., method: .patch)` or `req.httpMethod = "PATCH"`.
        """
        repo = os.path.abspath(app.get("repo_path", ""))
        calls = self._scan(app)

        wanted = {os.path.abspath(f) for f in files}
        found: Set[str] = set()

        for endpoint, sites in calls.items():
            for site in sites:
                rel_file = site.rsplit(":", 1)[0]
                if os.path.join(repo, rel_file) in wanted:
                    found.add(endpoint)
                    break
        return found

    # ── Full analysis ────────────────────────────────────────────────────────

    def analyze(
        self,
        changed_files: List[str],
        app_configs: List[Dict[str, Any]],
        depth: int = 2,
    ) -> Dict[str, Any]:
        """Cross-app impact of *changed_files*."""
        if not changed_files:
            return self._empty(changed_files)

        # 1. Which app owns the change?
        owners: Dict[str, List[str]] = {}
        for cf in changed_files:
            app = self._owning_app(cf, app_configs)
            if app:
                owners.setdefault(app["name"], []).append(cf)

        if not owners:
            result = self._empty(changed_files)
            result["error"] = (
                "None of the changed files could be located in any app in this group. "
                "Check that the projects are cloned and the paths are correct."
            )
            return result

        by_name = {a["name"]: a for a in app_configs}
        primary_name = max(owners, key=lambda n: len(owners[n]))
        primary = by_name[primary_name]

        # 2-3. Graphify blast radius within the owning app(s), then the endpoints
        #      those files touch.
        blast: Set[str] = set()
        affected_endpoints: Set[str] = set()
        graphify_nodes = 0

        for app_name, files in owners.items():
            app = by_name[app_name]
            radius, n_nodes = self.intra_app_blast_radius(
                app["repo_path"], files, depth=depth
            )
            graphify_nodes += n_nodes
            blast.update(radius)
            affected_endpoints.update(self.endpoints_in_files(app, set(radius)))

        # 4. Which OTHER apps call those endpoints?
        cross_app: List[Dict[str, Any]] = []
        for app in app_configs:
            if app["name"] in owners:
                continue
            calls = self._scan(app)

            hit_endpoints = [e for e in affected_endpoints if e in calls]
            if not hit_endpoints:
                continue

            files_ref: List[str] = []
            for e in hit_endpoints:
                for site in calls.get(e, []):
                    fname = site.rsplit(":", 1)[0]
                    if fname not in files_ref:
                        files_ref.append(fname)

            cross_app.append({
                "app": app["name"],
                "reason": "calls " + ", ".join(sorted(hit_endpoints)),
                "endpoints": sorted(hit_endpoints),
                "affected_files": files_ref,
                "test_suite": app.get("test_suite"),
                "project_id": app.get("project_id"),
                "app_role": app.get("app_role"),
            })

        # 5. Suites: the changed app(s) plus every impacted app.
        suites: List[str] = []
        for name in list(owners) + [c["app"] for c in cross_app]:
            suite = by_name.get(name, {}).get("test_suite")
            if suite and suite not in suites:
                suites.append(suite)

        repo_root = os.path.abspath(primary["repo_path"])
        blast_rel = sorted(
            os.path.relpath(f, repo_root) if f.startswith(repo_root) else f
            for f in blast
        )

        return {
            "changed_files": list(changed_files),
            "primary_app": primary_name,
            "primary_app_project_id": primary.get("project_id"),
            "intra_app_blast_radius": blast_rel,
            "affected_endpoints": sorted(affected_endpoints),
            "cross_app_impact": cross_app,
            "all_test_suites_to_run": suites,
            "total_apps_affected": len(cross_app),
            "graphify_nodes_affected": graphify_nodes,
            "graphify_available": GRAPHIFY_AVAILABLE,
        }

    def _empty(self, changed_files: List[str]) -> Dict[str, Any]:
        return {
            "changed_files": list(changed_files),
            "primary_app": None,
            "primary_app_project_id": None,
            "intra_app_blast_radius": [],
            "affected_endpoints": [],
            "cross_app_impact": [],
            "all_test_suites_to_run": [],
            "total_apps_affected": 0,
            "graphify_nodes_affected": 0,
            "graphify_available": GRAPHIFY_AVAILABLE,
        }

    # ── Module graph (intra-app file dependencies) for D3 ────────────────────

    def build_module_graph(self, repo_path: str, max_nodes: int = 140) -> Dict[str, Any]:
        """File-level dependency graph for ONE app, from Graphify's AST.

        Collapses Graphify's symbol-level graph to files: an edge A->B means some
        symbol in file A calls/imports a symbol in file B. Same {nodes, links}
        shape the endpoint graph uses, so the dashboard renders it with the same
        force layout. Capped to the most-connected files so a big repo stays legible.
        """
        empty = {"nodes": [], "links": [], "stats": {"apps": 0, "endpoints": 0,
                 "shared_endpoints": 0, "edges": 0}, "available": GRAPHIFY_AVAILABLE}
        graph = self._graph_for(repo_path)
        if graph is None:
            return empty

        root = os.path.abspath(repo_path)

        def rel(path: str) -> str:
            ap = self._abs(repo_path, path)
            return os.path.relpath(ap, root) if ap.startswith(root) else path

        # file -> file, weighted by number of symbol edges between them.
        weights: Dict[Tuple[str, str], int] = {}
        for u, v in graph.edges():
            fu, fv = self._node_file(graph, u), self._node_file(graph, v)
            if not fu or not fv:
                continue
            ru, rv = rel(fu), rel(fv)
            if ru == rv:
                continue
            weights[(ru, rv)] = weights.get((ru, rv), 0) + 1

        if not weights:
            return empty

        # Keep the most-connected files (degree), so a large repo stays readable.
        degree: Dict[str, int] = {}
        for (a, b), w in weights.items():
            degree[a] = degree.get(a, 0) + w
            degree[b] = degree.get(b, 0) + w
        keep = {f for f, _ in sorted(degree.items(), key=lambda kv: kv[1], reverse=True)[:max_nodes]}

        links = [
            {"source": a, "target": b, "weight": w}
            for (a, b), w in weights.items() if a in keep and b in keep
        ]
        used = {e["source"] for e in links} | {e["target"] for e in links}
        nodes = [
            {
                "id": f,
                "label": os.path.basename(f),
                "type": "app",                 # renders as a circle in the same layout
                "kind": "module",
                "color": "#6366f1",
                "connections": degree.get(f, 1),
            }
            for f in sorted(used)
        ]
        return {
            "nodes": nodes,
            "links": links,
            "stats": {
                "apps": len(nodes),
                "endpoints": 0,
                "shared_endpoints": 0,
                "edges": len(links),
                "truncated": len(degree) > len(keep),
                "total_files": len(degree),
            },
            "available": GRAPHIFY_AVAILABLE,
        }

    # ── Combined graph for D3 ────────────────────────────────────────────────

    def build_graph(self, app_configs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """App -> endpoint graph shaped for the dashboard's force layout."""
        role_colors = {
            "consumer": "#3B82F6",
            "business": "#F97316",
            "superadmin": "#A855F7",
            "api": "#22C55E",
        }
        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        endpoint_users: Dict[str, List[str]] = {}

        for app in app_configs:
            name = app["name"]
            if name not in seen:
                seen.add(name)
                nodes.append({
                    "id": name,
                    "type": "app",
                    "color": role_colors.get(app.get("app_role", ""), "#64748B"),
                    "app_role": app.get("app_role"),
                    "test_suite": app.get("test_suite"),
                    "project_id": app.get("project_id"),
                })

            for endpoint, sites in self._scan(app).items():
                if endpoint not in seen:
                    seen.add(endpoint)
                    nodes.append({
                        "id": endpoint,
                        "type": "endpoint",
                        "color": "#22C55E",
                    })
                endpoint_users.setdefault(endpoint, []).append(name)
                for site in sites:
                    edges.append({
                        "source": name,
                        "target": endpoint,
                        "label": site,
                    })

        for node in nodes:
            if node["type"] == "endpoint":
                users = endpoint_users.get(node["id"], [])
                node["used_by"] = users
                node["shared"] = len(users) > 1
            node["connections"] = sum(
                1 for e in edges if node["id"] in (e["source"], e["target"])
            )

        return {
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "apps": sum(1 for n in nodes if n["type"] == "app"),
                "endpoints": sum(1 for n in nodes if n["type"] == "endpoint"),
                "shared_endpoints": sum(
                    1 for n in nodes if n["type"] == "endpoint" and n.get("shared")
                ),
                "edges": len(edges),
            },
        }


# Directories never worth walking when locating a changed file.
_SKIP = {
    ".git", "node_modules", ".venv", "__pycache__", "Pods", "build",
    "DerivedData", "dist", "vendor",
}

hybrid_impact_analyzer = HybridImpactAnalyzer()
