"""The cross-app spine as Archify JSON IR.

ONE converter, used by both the live Workflow diagram and the PR before/after delta,
so the two can never disagree about what the spine is.

Why `architecture` and not the `workflow` diagram type, despite this being a workflow:
Archify's workflow grid caps `col` at 5 (six columns). The business lane is nine steps
deep (login -> bookings -> assign -> add -> send -> ready -> serve -> notify -> close),
so it does not fit. `architecture` has no column cap and takes a 12-wide grid.

Archify enforces CLEAN LAYOUT — it rejects edges that cut through unrelated components
and labels that overlap them. Two things here exist only to satisfy that, and both were
arrived at by running the validator, not by guessing:

  * cross-app handoffs declare fromSide/toSide, and the sides must be TRUTHFUL about
    direction: the consumer row sits above the business row, so consumer->business
    leaves 'bottom' and enters 'top', and business->consumer is the reverse. Declaring
    bottom/top for an upward edge is rejected as dishonest routing.
  * edge labels are dropped. They are the main source of overlap failures, and the
    relationship is already legible from the shape.
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, List

from automation.workflow import catalog as C

#: Long node labels overflow the default 120px box and fail the layout check.
_SHORTEN = {
    "Pay: Cash (B-App)": "Pay: Cash",
    "Pay: Voucher (B-App)": "Pay: Voucher",
    "Pay: E-pay (B-App)": "Pay: E-pay",
}


def _nodes(cat=C) -> Dict[str, Dict[str, str]]:
    return {n["id"]: n for n in cat.all_nodes()}


def _columns(nodes: Dict[str, Dict[str, str]], cat=C) -> Dict[str, int]:
    """Longest-path depth, so branches that rejoin land in the same column.

    Plain insertion order would put 'order later' and 'pre-order' in different columns
    even though they are alternatives that both feed the wallet.
    """
    succ: Dict[str, List[str]] = {}
    indeg = {k: 0 for k in nodes}
    for e in cat.WF_EDGES:
        succ.setdefault(e["source"], []).append(e["target"])
        indeg[e["target"]] = indeg.get(e["target"], 0) + 1
    col = {k: 0 for k in nodes}
    queue = [k for k, d in indeg.items() if d == 0]
    while queue:
        cur = queue.pop(0)
        for nxt in succ.get(cur, []):
            col[nxt] = max(col[nxt], col[cur] + 1)
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    return col


def build_ir(built_ids: FrozenSet[str] = frozenset(), cat=C) -> Dict[str, Any]:
    """The spine as Archify `architecture` IR. *built_ids* marks covered spine steps.

    *cat* is the catalog module to read. It is injectable so the PR delta can build the
    spine as it was at a git ref through THIS converter — diffing two snapshots is only
    meaningful when both were produced the same way.
    """
    nodes = _nodes(cat)
    col = _columns(nodes, cat)
    app = {nid: n["app"] for nid, n in nodes.items()}

    components, used = [], {}
    for nid, n in nodes.items():
        base = 0 if n["app"] == "consumer" else 2
        key = (base, col[nid])
        row = base + used.get(key, 0)          # stack same-column siblings, don't overlap
        used[key] = used.get(key, 0) + 1
        label = _SHORTEN.get(n["label"], n["label"])
        comp = {"id": nid, "col": col[nid], "row": row,
                "type": "frontend" if n["app"] == "consumer" else "backend",
                "label": label,
                "sublabel": "built" if nid in built_ids else "not built"}
        if nid in built_ids:
            comp["tag"] = "covered"
        components.append(comp)

    connections = []
    for e in cat.WF_EDGES:
        src, dst = e["source"], e["target"]
        # STABLE id — `compare` refuses to diff connections without authored ids, and a
        # positional id would report every insertion as "everything changed".
        conn: Dict[str, Any] = {"id": f"{src}__{dst}", "from": src, "to": dst}
        if e.get("kind") == "handoff":
            conn["variant"] = "dashed"
            if app[src] == "consumer":
                conn["fromSide"], conn["toSide"] = "bottom", "top"
            else:
                conn["fromSide"], conn["toSide"] = "top", "bottom"
        connections.append(conn)

    return {
        "schema_version": 1,
        "diagram_type": "architecture",
        "meta": {
            "title": "Vyapy cross-app spine",
            "subtitle": "Consumer (iPhone) + Business (iPad), with cross-app handoffs",
            "visual_preset": "signal-flow",
        },
        "layout": {"mode": "grid", "cols": max(col.values()) + 1},
        "components": components,
        "boundaries": [
            {"kind": "region", "label": "Consumer App",
             "wraps": [n for n in nodes if app[n] == "consumer"]},
            {"kind": "region", "label": "Business App",
             "wraps": [n for n in nodes if app[n] == "business"]},
        ],
        "connections": connections,
    }
