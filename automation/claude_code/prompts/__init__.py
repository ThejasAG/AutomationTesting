"""The prompt a session starts with: common.md (where, tools, rules, report) with
the kind's task (<kind>.md) filled in from the session's options.

Templates use `$name` placeholders (string.Template) so the JSON examples in them
need no escaping.
"""
from __future__ import annotations

import os
from datetime import datetime
from string import Template
from typing import Any, Dict, List

_DIR = os.path.dirname(os.path.abspath(__file__))

TITLES = {"heal": "run and heal", "fix_run": "fix one failed run", "analyze": "analyze the apps",
          "generate": "new scenarios", "nightly": "nightly pass"}

MODE_TEXT = {
    "delta": ("Quick pass: run `app-changes`. Analyze the screens and features that new app "
              "commits added or changed (look at the changed files under App/), plus any screen "
              "`coverage` shows no flow drives. If nothing new landed, say so and keep it short."),
    "full": ("Weekly full analysis: every screen of both apps — the consumer app and the "
             "business app's waiter and kitchen roles."),
}
MODE_SCOPE = {"delta": "only the screens new commits changed and the uncovered ones",
              "full": "every screen of its app"}


def _read(name: str) -> str:
    with open(os.path.join(_DIR, name), encoding="utf-8") as f:
        return f.read()


def resolve_mode(mode: str) -> str:
    if mode in ("delta", "full"):
        return mode
    return "full" if datetime.now().weekday() == 6 else "delta"


def describe_runs(run_ids: List[str]) -> List[str]:
    from automation.agentic.runinfo import flow_id_for_run, load
    out = []
    for rid in run_ids:
        run, rows = load(rid)
        if run is None:
            out.append(f"- run {rid} (not found)")
            continue
        err = (run.error_message or next((r.error for r in rows if r.error), "") or "").strip()
        out.append(f"- run `{rid}` — flow `{flow_id_for_run(run) or '?'}` ({run.test_name}) — "
                   f"{run.status}: {err.splitlines()[0][:240] if err else 'no error text'}")
    return out


def _run_scope(options: Dict[str, Any], s: Dict[str, Any]) -> str:
    ids = options.get("run_ids") or []
    if ids:
        return ("A dashboard **Run all** just finished. These runs failed — investigate exactly "
                "these; do not re-run the flows that passed:\n" + "\n".join(describe_runs(ids)))
    demos = ("include the quick demos too" if s.get("include_demos") else
             "skip the quick demos flow_book_demo, flow_waiter_demo and flow_kitchen_demo")
    only = options.get("flow_ids") or []
    if only:
        return "Run these flows, one by one, in this order: " + ", ".join(f"`{f}`" for f in only) + "."
    return f"Run every flow from `flows`, one by one, in that order ({demos})."


def _focus(options: Dict[str, Any]) -> str:
    if options.get("features"):
        return "\n".join(f"- {f}" for f in options["features"])
    if (options.get("focus") or "").strip():
        return options["focus"].strip()
    try:
        from automation.claude_code import session
        fmap = session.latest_feature_map()
    except Exception:
        fmap = None
    gaps = []
    for app, feats in ((fmap or {}).get("apps") or {}).items():
        for f in feats or []:
            if f.get("status") in ("missing", "partial") and f.get("risk") == "high":
                gaps.append(f"- [{app}] {f.get('feature')}: {f.get('gap') or f.get('scenario') or ''}")
    if gaps:
        return "The high-risk gaps from the latest analysis:\n" + "\n".join(gaps[:15])
    return ("No analysis yet: run `coverage`, and pick the highest-risk screens no flow drives "
            "(payments, bookings, orders first).")


def previous_features() -> Dict[str, Any]:
    """The latest feature map's feature names per app — the baseline the next analysis
    keeps (MEASURED 2026-10-26: two analyses grouped the same screens into 44 and 24
    features, so the map could not be compared from one run to the next)."""
    try:
        from automation.claude_code import session
        fmap = session.latest_feature_map()
    except Exception:
        fmap = None
    lines, n = [], 0
    for app, feats in ((fmap or {}).get("apps") or {}).items():
        for f in feats or []:
            n += 1
            lines.append(f"   - [{app}/{f.get('role') or app}] {f.get('feature')}")
    return {"count": str(n), "text": "\n".join(lines) or "   (none yet — this is the first analysis)"}


def build(kind: str, options: Dict[str, Any], ctx: Dict[str, str], s: Dict[str, Any]) -> str:
    mode = resolve_mode(options.get("mode") or s.get("nightly_mode") or "auto")
    max_new = int(options.get("max_new") or s.get("max_new_scenarios") or 3)
    if kind == "nightly" and mode == "full":
        max_new *= 2
    prev = previous_features() if kind in ("analyze", "nightly") else {"count": "0", "text": ""}
    values = {
        **ctx,
        "previous_count": prev["count"],
        "previous_features": prev["text"],
        "title": TITLES.get(kind, kind),
        "max_fix_attempts": str(s.get("max_fix_attempts") or 2),
        "max_new": str(max_new),
        "mode": mode,
        "mode_text": MODE_TEXT[mode],
        "mode_scope": MODE_SCOPE[mode],
        "generate_hint": ("New scenarios are written in a separate 'Create scenarios' session, "
                          "after a person picks the gaps."),
        "focus": _focus(options) if kind == "generate" else "",
        "run_scope": _run_scope(options, s) if kind in ("heal", "nightly") else "",
        "run_list": "\n".join(describe_runs(options.get("run_ids") or [])) if kind == "fix_run" else "",
    }
    task = Template(_read(f"{kind}.md")).safe_substitute(values)
    return Template(_read("common.md")).safe_substitute({**values, "task": task})
