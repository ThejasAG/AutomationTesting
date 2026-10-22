"""The AI Agent's plain parts (automation/agentic/): the rule sorter, the coverage
inventory, @token validation and the run/flow readers its Claude Code sessions use.
The sessions themselves are tested in tests/test_claude_agent.py.
"""
import uuid
from datetime import datetime

import pytest

from automation.database.config import Base, SessionLocal, engine
from automation.database import models  # noqa: F401
from automation.database.models import ScenarioResult, TestRun


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.create_all(bind=engine)
    yield
    with SessionLocal() as db:
        for m in (ScenarioResult, TestRun, models.AgentSetting):
            db.query(m).delete()
        db.commit()


def _failed_run(error: str, flow_name: str = "Preorder → C-App card → pay in B-App") -> str:
    rid = str(uuid.uuid4())
    with SessionLocal() as db:
        db.add(TestRun(id=rid, test_name=f"[Staging] {flow_name}", status="failed",
                       bot_type="ios-crossapp-flow", started_at=datetime.utcnow()))
        db.add(ScenarioResult(run_id=rid, scenario_num="1", scenario_name="C-App", status="FAIL",
                              error=error, reasons=[f"[where] failed at step: 'click x (step 2/5)'"]))
        db.commit()
    return rid


# ── rule sorter ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("error,cat", [
    ("[FAIL] @to_checkout: Could not proxy command to the remote server. socket hang up", "INFRA"),
    ("[FAIL] open app — APP BUG (not automation): No bundle URL present.", "INFRA"),
    ("[FAIL] open app — APP BUG (not automation): RCTFatal", "APP_BUG"),
    ("[FAIL] select Any — No element matches 'Any' — it may need a testID", "LOCATOR"),
    ("[FAIL] @open_order — timed out after 240s (step hung; aborting segment)", "TIMING"),
    ("[FAIL] something nobody has seen before", "UNKNOWN"),
])
def test_sorter_uses_the_runners_real_messages(error, cat):
    from automation.agentic import classifier
    rid = _failed_run(error)
    with SessionLocal() as db:
        v = classifier.classify(db.get(TestRun, rid),
                                db.query(ScenarioResult).filter_by(run_id=rid).all())
    assert v["category"] == cat
    assert v["failed_step"] == "click x (step 2/5)"


def test_only_infra_and_timing_get_a_free_retry():
    from automation.agentic.classifier import first_policy
    assert first_policy("INFRA") == "retry" and first_policy("TIMING") == "resume"
    assert first_policy("APP_BUG") is None and first_policy("LOCATOR") is None


# ── @tokens and value checks ─────────────────────────────────────────────────

def test_only_tokens_the_dispatcher_names_are_valid():
    from automation.agentic.runinfo import token_ok
    assert token_ok("@assign_table") and token_ok("@pay:cash") and token_ok("click addNewEvent")
    assert not token_ok("@make_it_pass")


def test_a_draft_flow_cannot_vouch_for_its_own_invented_token(monkeypatch):
    """Tokens used to be trusted when any flow used them, so a new flow with an
    invented @token passed check-flows by its own say-so."""
    from automation.agentic import runinfo
    from automation.scenarios import cross_app_flows as caf
    runinfo.known_tokens.cache_clear()
    monkeypatch.setitem(caf.FLOWS, "cc_fake", {"id": "cc_fake", "name": "[Draft] fake", "segments": [
        {"num": "1", "name": "x", "role": "consumer", "steps": ["@invented_step"]}]})
    try:
        assert not runinfo.token_ok("@invented_step")
    finally:
        runinfo.known_tokens.cache_clear()


def test_value_check_steps_are_recognised():
    from automation.agentic.runinfo import CHECK_STEP
    for st in ("verify the total is €12.00", "check VAT", "assert amount 5.00"):
        assert CHECK_STEP.search(st)
    assert not CHECK_STEP.search("click addNewEvent")


# ── run / flow readers ───────────────────────────────────────────────────────

def test_a_run_is_mapped_to_its_flow_by_name():
    from automation.agentic import runinfo
    from automation.scenarios.cross_app_flows import list_flows
    f = list_flows()[0]
    rid = _failed_run("[FAIL] x", flow_name=f["name"])
    run, rows = runinfo.load(rid)
    assert runinfo.flow_id_for_run(run) == f["id"]
    assert "Segment 1" in runinfo.run_text(rid)


# ── application inventory ────────────────────────────────────────────────────

def test_barrel_exports_resolve_to_each_screens_own_file(tmp_path, monkeypatch):
    from automation.agentic import inventory, sources
    app = tmp_path / "App"
    (app / "Navigation").mkdir(parents=True)
    (app / "Screens" / "Refund").mkdir(parents=True)
    (app / "Screens" / "index.js").write_text(
        "export {default as Refund} from './Refund';\nexport {default as Home} from './Home';\n")
    (app / "Screens" / "Refund" / "index.js").write_text(
        "<Button testID=\"refundSubmitBtn\"/>\n<View accessibilityLabel={`${id}RefundRow`}/>\n")
    (app / "Navigation" / "Stack.js").write_text(
        "import {Refund} from '../Screens';\n<Stack.Screen name=\"Refund\" component={Refund} />\n")
    monkeypatch.setattr(sources, "app_repo", lambda a: str(tmp_path) if a == "consumer" else None)
    [r] = inventory.routes("consumer")
    assert r["route"] == "Refund" and r["file"] == "App/Screens/Refund/index.js"
    assert r["ids"] == ["*RefundRow", "refundSubmitBtn"]


def test_coverage_counts_only_ids_the_automation_drives(monkeypatch):
    from automation.agentic import inventory
    monkeypatch.setattr(inventory, "_automation_tokens", lambda: {"bookAppoitment", "click"})
    monkeypatch.setattr(inventory, "routes", lambda app: [
        {"route": "Reservation", "file": "a.js", "ids": ["bookAppoitment", "counterPlus"]},
        {"route": "Refund", "file": "b.js", "ids": ["refundSubmitBtn"]},
    ] if app == "consumer" else [])
    a = inventory.build()["apps"]["consumer"]
    assert [r["covered"] for r in a["routes"]] == [True, False]
    assert (a["screens_covered"], a["ids_covered"], a["ids"]) == (1, 1, 3)
