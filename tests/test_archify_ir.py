"""The spine -> Archify IR converter, and the vendored CLI.

The IR tests are pure and fast. The two CLI tests actually shell out to the vendored
renderer and are skipped when node is absent — they are the ones that would catch an
upstream schema change after a version bump, so they are worth the couple of seconds.
"""
import json

import pytest

from automation.workflow import catalog as C
from automation.workflow import archify_cli
from automation.workflow.archify_ir import build_ir

IR = build_ir()
IDS = {c["id"] for c in IR["components"]}


# ── shape ────────────────────────────────────────────────────────────────────
def test_every_spine_and_branch_node_is_a_component():
    assert IDS == {n["id"] for n in C.all_nodes()}


def test_every_catalog_edge_becomes_a_connection():
    got = {(c["from"], c["to"]) for c in IR["connections"]}
    assert got == {(e["source"], e["target"]) for e in C.WF_EDGES}


def test_the_two_apps_become_the_two_regions():
    labels = {b["label"] for b in IR["boundaries"]}
    assert labels == {"Consumer App", "Business App"}
    assert all(b["kind"] == "region" for b in IR["boundaries"])   # schema enum


# ── the constraints that made me choose `architecture` ───────────────────────
def test_the_spine_is_deeper_than_the_workflow_grid_allows():
    """Archify's `workflow` type caps col at 5. If the spine ever fits in six columns
    this converter could switch to it — until then, this is why it does not."""
    assert max(c["col"] for c in IR["components"]) > 5


def test_branches_that_rejoin_share_a_column():
    col = {c["id"]: c["col"] for c in IR["components"]}
    assert col["c_preorder"] == col["c_order_later"]        # alternatives, same depth
    assert col["b_pay_cash"] == col["b_pay_voucher"] == col["b_pay_epay"]


def test_same_column_siblings_do_not_share_a_cell():
    seen = {(c["col"], c["row"]) for c in IR["components"]}
    assert len(seen) == len(IR["components"])              # no two components overlap


# ── clean-layout requirements, learned from the validator ────────────────────
def test_handoff_sides_are_truthful_about_direction():
    """Consumer sits above Business. Declaring bottom->top for an upward edge is
    rejected by the layout checker as dishonest routing, so the side depends on which
    way the handoff actually goes."""
    app = {n["id"]: n["app"] for n in C.all_nodes()}
    handoffs = [c for c in IR["connections"] if c.get("variant") == "dashed"]
    assert handoffs
    for c in handoffs:
        if app[c["from"]] == "consumer":
            assert (c["fromSide"], c["toSide"]) == ("bottom", "top")
        else:
            assert (c["fromSide"], c["toSide"]) == ("top", "bottom")


def test_no_edge_labels():
    # They are the main source of label/component overlap failures.
    assert not any("label" in c for c in IR["connections"])


def test_long_labels_are_shortened_to_fit_the_box():
    labels = {c["label"] for c in IR["components"]}
    assert "Pay: Voucher" in labels                        # was "Pay: Voucher (B-App)"
    assert all(len(l) <= 22 for l in labels)


# ── diffing ──────────────────────────────────────────────────────────────────
def test_connection_ids_are_stable_and_positional_free():
    """`compare` refuses to diff connections without authored ids, and a positional id
    would report every insertion as 'everything changed'."""
    assert all(c["id"] == f"{c['from']}__{c['to']}" for c in IR["connections"])
    assert build_ir()["connections"] == IR["connections"]   # deterministic across calls


def test_built_status_marks_only_what_was_passed():
    ir = build_ir(frozenset({"c_login"}))
    by = {c["id"]: c for c in ir["components"]}
    assert by["c_login"]["sublabel"] == "built" and by["c_login"]["tag"] == "covered"
    assert by["c_book"]["sublabel"] == "not built" and "tag" not in by["c_book"]


# ── against the real renderer ────────────────────────────────────────────────
_node_missing = not archify_cli.available()[0]


@pytest.mark.skipif(_node_missing, reason=archify_cli.available()[1])
def test_the_generated_ir_passes_archifys_own_validator(tmp_path):
    """Guards the vendored version: an upstream schema change breaks here, loudly,
    rather than silently producing a blank diagram in the dashboard."""
    html, why = archify_cli.render(IR)
    assert html, why
    assert "<html" in html.lower()


@pytest.mark.skipif(_node_missing, reason=archify_cli.available()[1])
def test_compare_reports_an_added_step_exactly():
    base = build_ir()
    head = build_ir()
    head["components"].append({"id": "b_pay_split", "col": 8, "row": 6,
                               "type": "backend", "label": "Pay: Split",
                               "sublabel": "not built"})
    head["connections"].append({"id": "b_notify__b_pay_split",
                                "from": "b_notify", "to": "b_pay_split"})
    head["boundaries"][1]["wraps"].append("b_pay_split")
    summary, html, why = archify_cli.compare(base, head)
    assert summary and summary.get("ok"), why
    s = summary["summary"]
    assert s["components"]["added"] == 1 and s["components"]["removed"] == 0
    assert s["connections"]["added"] == 1


def test_a_missing_node_binary_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(archify_cli.shutil, "which", lambda _n: None)
    ok, why = archify_cli.available()
    assert ok is False and "node" in why
    html, reason = archify_cli.render(IR)
    assert html is None and "node" in reason        # degrades, never raises


# ── base/head selection ──────────────────────────────────────────────────────
def test_a_ref_resolves_through_the_same_converter_as_the_live_spine():
    """Both sides of a diff must be built the same way, or the comparison reports
    differences that are artefacts of the builder rather than of the change."""
    from automation.workflow.catalog_at_ref import spine_ir_at_ref
    at_head, why = spine_ir_at_ref("HEAD")
    assert at_head is not None, why
    live = build_ir()
    assert {c["id"] for c in at_head["components"]} == {c["id"] for c in live["components"]}


def test_an_unknown_ref_is_reported_not_raised():
    from automation.workflow.catalog_at_ref import spine_ir_at_ref
    ir, why = spine_ir_at_ref("no/such/ref")
    assert ir is None and why


def test_the_endpoint_defaults_head_to_the_working_tree():
    """head_ref empty = working tree (what you want while editing locally); a PR passes
    its head sha, because the reviewer's working tree is not the PR."""
    import inspect
    from automation.api.v1.routers import workflow as W
    src = inspect.getsource(W.get_diagram_delta)
    assert 'head_ref: str = ""' in src
    assert "if head_ref.strip():" in src
    assert "build_ir(built)" in src            # the working-tree branch


def test_the_delta_is_only_meaningful_between_platform_refs():
    """A PR on the apps under test cannot change the platform's spine.

    catalog.py lives in the PLATFORM repo; PR base refs (pre-prod-one, staging,
    preprod-2-May18) are branches of vya-consumer / vya-business. Comparing across the
    two repositories fails with "fatal: invalid object name" — which is what the Pull
    Requests page showed on every row before the panel was removed from it.
    """
    from automation.workflow.catalog_at_ref import catalog_at_ref
    for app_ref in ("pre-prod-one", "staging", "preprod-2-May18"):
        mod, why = catalog_at_ref(app_ref)
        assert mod is None
        assert "invalid object name" in why or "unknown git ref" in why


def test_the_pull_requests_page_does_not_render_the_flow_delta():
    from pathlib import Path
    src = Path("automation/dashboard/src/pages/PullRequestsPage.tsx").read_text()
    assert "FlowDeltaPanel" not in src
