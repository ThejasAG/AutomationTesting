"""The autonomous test agent (AI Agent page): sorter, guards, Claude loop, batch.

Asked 2026-10-06: "create an agent … analyze the application, create scenarios,
test them, and if any fail, solve it end to end". These pin the parts that make
that safe to leave running overnight: failures are sorted without AI, a test is
never fixed by weakening it, a fix is kept only when a re-run passes, the Claude
budget is a hard stop, and the batch survives a backend restart.
"""
import types
import uuid
from datetime import datetime

import pytest

from automation.database.config import Base, SessionLocal, engine
from automation.database import models  # noqa: F401
from automation.database.models import (AgentAction, AgentBatch, AgentItem, CrossAppFlowEdit,
                                        ScenarioResult, TestRun)


@pytest.fixture(autouse=True)
def _db():
    Base.metadata.create_all(bind=engine)
    yield
    with SessionLocal() as db:
        for m in (AgentAction, AgentItem, AgentBatch, ScenarioResult, TestRun, CrossAppFlowEdit,
                  models.AgentSetting):
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


# ── test-fix guards ──────────────────────────────────────────────────────────

def _flow1_step():
    from automation.scenarios.cross_app_flows import list_flows
    f = next(x for x in list_flows() if x["id"] == "flow1")
    return f["segments"][0]["num"], f["segments"][0]["steps"][1]


def test_a_step_that_checks_a_value_is_never_auto_edited():
    from automation.agentic import fixer
    with pytest.raises(fixer.FixRefused, match="checks a value"):
        fixer.check_test_fix("flow1", {"segment_num": "1", "old_step": "verify the total is €12.00",
                                       "new_steps": ["@got_it"]})


def test_an_invented_handler_is_refused():
    from automation.agentic import fixer
    num, step = _flow1_step()
    with pytest.raises(fixer.FixRefused, match="unknown step handler"):
        fixer.check_test_fix("flow1", {"segment_num": num, "old_step": step,
                                       "new_steps": ["@make_it_pass"]})


def test_apply_then_rollback_restores_the_builtin_flow():
    from automation.agentic import fixer
    from automation.scenarios.cross_app_flows import list_flows
    num, step = _flow1_step()
    snap = fixer.apply_test_fix("flow1", {"segment_num": num, "old_step": step,
                                          "new_steps": ["@hide_keyboard", step]})
    edited = next(x for x in list_flows() if x["id"] == "flow1")
    assert edited["edited"] and edited["segments"][0]["steps"][1] == "@hide_keyboard"
    fixer.rollback("flow1", snap)
    with SessionLocal() as db:
        assert db.get(CrossAppFlowEdit, "flow1") is None     # back to the built-in


# ── Claude loop (fake client) ────────────────────────────────────────────────

class _Usage:
    input_tokens, output_tokens = 1_000_000, 100_000
    cache_creation_input_tokens = cache_read_input_tokens = 0


def _msg(stop, blocks):
    return types.SimpleNamespace(stop_reason=stop, content=blocks, usage=_Usage())


def _tool(name, inp, id_="t1"):
    return types.SimpleNamespace(type="tool_use", name=name, input=inp, id=id_)


def _tool_results(messages):
    """The tool_result blocks the loop sent (the list is shared and keeps growing,
    so look them up rather than assume they are last)."""
    return [b for m in messages if m["role"] == "user" and isinstance(m["content"], list)
            for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]


class _FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return self.replies.pop(0)


def test_loop_runs_tools_prices_turns_and_returns_the_submission(monkeypatch):
    from automation.agentic import claude
    fake = _FakeClient([_msg("tool_use", [_tool("lookup", {"q": "x"})]),
                        _msg("tool_use", [_tool("submit", {"answer": 42}, "t2")])])
    monkeypatch.setattr(claude, "_client", lambda: fake)
    res = claude.run_loop(system="s", user_content="u", tools=[], submit_tool="submit",
                          handlers={"lookup": lambda i: f"found {i['q']}"},
                          model="claude-opus-5-5", budget_left_usd=100)
    assert res.stop == "submitted" and res.submitted == {"answer": 42}
    assert res.cost_usd == pytest.approx(2 * (4.0 + 2.0))      # 1M in @ $4 + 0.1M out @ $20
    sent = _tool_results(fake.calls[1]["messages"])[0]
    assert sent["type"] == "tool_result" and sent["content"] == "found x"
    assert fake.calls[0]["model"] == "claude-opus-5-5"
    assert fake.calls[0]["fallbacks"] == "default"


def test_loop_stops_at_the_budget(monkeypatch):
    from automation.agentic import claude
    fake = _FakeClient([_msg("tool_use", [_tool("lookup", {"q": "x"})])] * 5)
    monkeypatch.setattr(claude, "_client", lambda: fake)
    res = claude.run_loop(system="s", user_content="u", tools=[], submit_tool="submit",
                          handlers={"lookup": lambda i: "ok"}, model="claude-opus-5-5",
                          budget_left_usd=5.0)
    assert res.stop == "budget" and len(fake.calls) == 1


def test_a_failing_tool_is_reported_to_the_model_not_raised(monkeypatch):
    from automation.agentic import claude
    fake = _FakeClient([_msg("tool_use", [_tool("boom", {})]),
                        _msg("tool_use", [_tool("submit", {}, "t2")])])
    monkeypatch.setattr(claude, "_client", lambda: fake)

    def boom(_):
        raise RuntimeError("disk on fire")
    res = claude.run_loop(system="s", user_content="u", tools=[], submit_tool="submit",
                          handlers={"boom": boom}, model="claude-opus-5-5", budget_left_usd=100)
    err = _tool_results(fake.calls[1]["messages"])[0]
    assert res.stop == "submitted" and err["is_error"] and "disk on fire" in err["content"]


# ── the batch, with the simulators and Claude faked ─────────────────────────

@pytest.fixture
def rig(monkeypatch):
    """run_flow results are scripted: each call pops the next status."""
    from automation.agentic import orchestrator, preflight, settings
    monkeypatch.setattr(preflight, "run", lambda env="staging": [{"name": "x", "ok": True, "detail": ""}])
    monkeypatch.setattr(orchestrator, "_finish", lambda bid: None)
    script = {"results": [], "errors": []}

    def run_flow(flow_id, env, resume_from=None):
        st = script["results"].pop(0)
        err = script["errors"].pop(0) if script["errors"] else "[FAIL] @x — timed out after 240s"
        rid = _failed_run(err) if st == "failed" else str(uuid.uuid4())
        if st != "failed":
            with SessionLocal() as db:
                db.add(TestRun(id=rid, test_name="t", status=st, bot_type="ios-crossapp-flow"))
                db.commit()
        script.setdefault("calls", []).append(resume_from)
        return rid, st
    monkeypatch.setattr(orchestrator, "run_flow", run_flow)
    settings.update({"triage": True, "autofix_tests": True, "budget_usd": 10.0})
    return script


def _one_item_batch():
    with SessionLocal() as db:
        b = AgentBatch(env="staging", status="running", spend_usd=0.0)
        db.add(b)
        db.flush()
        it = AgentItem(batch_id=b.id, position=0, flow_id="flow1", flow_name="flow1", status="pending")
        db.add(it)
        db.commit()
        db.refresh(b)
        db.refresh(it)
        db.expunge(b)
        db.expunge(it)
    return b, it


def _status(iid):
    with SessionLocal() as db:
        return db.get(AgentItem, iid).status


def test_a_timeout_is_resumed_once_and_counts_as_passed_on_retry(rig):
    from automation.agentic import orchestrator, settings
    rig["results"] = ["failed", "passed"]
    b, it = _one_item_batch()
    orchestrator._process(it, b, settings.get())
    assert _status(it.id) == "passed_on_retry"
    assert rig["calls"][1] is not None            # resumed from the failed run, not restarted


def test_a_verified_test_fix_is_kept(rig, monkeypatch):
    from automation.agentic import claude, orchestrator, settings, triage
    num, step = _flow1_step()
    rig["results"] = ["failed", "passed"]
    rig["errors"] = ["[FAIL] select Any — No element matches 'Any'"]
    monkeypatch.setattr(claude, "configured", lambda: True)
    monkeypatch.setattr(triage, "diagnose", lambda *a, **k: claude.LoopResult(
        stop="submitted", cost_usd=0.5, submitted={
            "category": "LOCATOR", "root_cause": "id renamed", "confidence": "high",
            "recommended_action": "fix_test", "evidence": [],
            "test_fix": {"segment_num": num, "old_step": step,
                         "new_steps": ["@hide_keyboard", step], "reason": "keyboard"},
            "app_fix": {"app": "none", "patch": "", "explanation": ""}}))
    b, it = _one_item_batch()
    orchestrator._process(it, b, settings.get())
    assert _status(it.id) == "fixed"
    with SessionLocal() as db:
        fix = db.query(AgentAction).filter_by(kind="test_fix").one()
        assert fix.status == "verified"
        assert db.get(CrossAppFlowEdit, "flow1") is not None
        assert db.get(AgentBatch, b.id).spend_usd == pytest.approx(0.5)


def test_a_fix_that_does_not_make_the_flow_pass_is_rolled_back(rig, monkeypatch):
    from automation.agentic import claude, orchestrator, settings, triage
    num, step = _flow1_step()
    settings.update({"max_fix_attempts": 1})
    rig["results"] = ["failed", "failed"]
    rig["errors"] = ["[FAIL] select Any — No element matches 'Any'"] * 2
    monkeypatch.setattr(claude, "configured", lambda: True)
    monkeypatch.setattr(triage, "diagnose", lambda *a, **k: claude.LoopResult(
        stop="submitted", submitted={
            "category": "LOCATOR", "root_cause": "?", "confidence": "medium",
            "recommended_action": "fix_test", "evidence": [],
            "test_fix": {"segment_num": num, "old_step": step, "new_steps": ["@hide_keyboard", step],
                         "reason": ""},
            "app_fix": {"app": "none", "patch": "", "explanation": ""}}))
    b, it = _one_item_batch()
    orchestrator._process(it, b, settings.get())
    assert _status(it.id) == "failed"
    with SessionLocal() as db:
        assert db.query(AgentAction).filter_by(kind="test_fix").one().status == "rolled_back"
        assert db.get(CrossAppFlowEdit, "flow1") is None


def test_no_claude_spend_once_the_budget_is_gone(rig, monkeypatch):
    from automation.agentic import claude, orchestrator, settings, triage
    rig["results"] = ["failed"]
    rig["errors"] = ["[FAIL] select Any — No element matches 'Any'"]
    monkeypatch.setattr(claude, "configured", lambda: True)
    called = []
    monkeypatch.setattr(triage, "diagnose", lambda *a, **k: called.append(1))
    b, it = _one_item_batch()
    with SessionLocal() as db:
        db.get(AgentBatch, b.id).spend_usd = 10.0
        db.commit()
    orchestrator._process(it, b, settings.get())
    assert not called and _status(it.id) == "failed"


def test_an_interrupted_batch_continues_after_a_restart(monkeypatch):
    from automation.agentic import orchestrator
    started = []
    monkeypatch.setattr(orchestrator.threading, "Thread",
                        lambda target, args, **k: types.SimpleNamespace(start=lambda: started.append(args)))
    b, it = _one_item_batch()
    with SessionLocal() as db:
        db.get(AgentItem, it.id).status = "running"
        db.get(AgentBatch, b.id).notes = "continues=0"
        db.commit()
    try:
        orchestrator.continue_after_restart()
        assert started == [(b.id,)] and _status(it.id) == "pending"
    finally:
        orchestrator._state["batch_id"] = None


def test_the_report_summarises_every_flow():
    from automation.agentic import report
    b, it = _one_item_batch()
    with SessionLocal() as db:
        db.get(AgentItem, it.id).status = "app_bug"
        db.get(AgentItem, it.id).root_cause = "Pay button does nothing"
        db.get(AgentBatch, b.id).totals = {"app_bug": 1}
        db.commit()
    rep = report.build(b.id)
    assert "0/1 flows green" in rep["subject"] and "Pay button does nothing" in rep["html"]


# ── application analysis ─────────────────────────────────────────────────────

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


def test_analysis_stores_features_and_proposes_only_new_valid_flows(monkeypatch):
    from automation.agentic import claude, generator, inventory, orchestrator
    monkeypatch.setattr(inventory, "build", lambda: {"apps": {"consumer": {
        "screens": 1, "screens_covered": 0, "ids": 1, "ids_covered": 0,
        "routes": [{"route": "Refund", "file": "b.js", "ids": ["x"], "covered_ids": [], "covered": False}]}}})
    ok = {"name": "Refund a paid order", "description": "", "rationale": "money path", "feature": "Refunds",
          "segments": [{"name": "C-App refund", "role": "consumer", "steps": ["@consumer_home", "click refundBtn"]}]}
    bad = {**ok, "name": "Uses a made-up handler",
           "segments": [{"name": "x", "role": "consumer", "steps": ["@do_magic"]}]}
    monkeypatch.setattr(generator, "analyze", lambda **k: claude.LoopResult(
        stop="submitted", cost_usd=1.25, submitted={
            "app_summary": "map", "scenarios": [ok, bad, ok],
            "features": [{"name": "Refunds", "app": "consumer", "screens": ["Refund"], "coverage": "missing",
                          "covered_by": [], "risk": "high", "notes": ""}]}))
    ran = []
    monkeypatch.setattr(orchestrator, "_background", lambda label, fn: (ran.append(label), fn()))
    orchestrator.generate_scenarios("refunds")
    with SessionLocal() as db:
        an = db.query(AgentAction).filter_by(kind="app_analysis").one()
        props = {a.title: a for a in db.query(AgentAction).filter_by(kind="scenario_proposal")}
        assert an.detail["features"][0]["coverage"] == "missing" and an.cost_usd == 1.25
        assert an.detail["coverage"]["consumer"]["screens"] == 1
        statuses = sorted(a.status for a in db.query(AgentAction).filter_by(kind="scenario_proposal"))
    assert statuses == ["proposed", "rejected", "rejected"]          # valid, unknown handler, duplicate
    assert props["Refund a paid order"].detail["flow_id"] == "agent_refund_a_paid_order"


def test_approve_and_test_adds_the_flow_and_runs_it_once(monkeypatch):
    from automation.agentic import orchestrator
    from automation.scenarios.cross_app_flows import list_flows
    with SessionLocal() as db:
        a = AgentAction(kind="scenario_proposal", status="proposed", title="Refund",
                        detail={"flow_id": "agent_refund", "flow": {
                            "name": "Refund", "description": "", "segments": [
                                {"name": "C", "role": "consumer", "steps": ["@consumer_home"]}]}})
        db.add(a)
        db.commit()
        aid = a.id
    started = []
    monkeypatch.setattr(orchestrator, "start_batch", lambda trig, ids: started.append((trig, ids)) or "b1")
    out = orchestrator.approve_and_test(aid)
    assert out == {"flow_id": "agent_refund", "batch_id": "b1"}
    assert started == [("trial", ["agent_refund"])]
    assert any(f["id"] == "agent_refund" for f in list_flows())
