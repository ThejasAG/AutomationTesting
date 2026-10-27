"""Creating scenarios faster.

Asked 2026-10-27: "creating one scenario is taking around one hour … 100+ scenarios
yet to create … make the creating scenarios more faster". MEASURED on that day's
session (cf6bd414, 57 min for 3 drafts): 23 min in helper agents rediscovering the
step forms, the idb API and the unit-test pattern; 7 min running the full unit suite
the checks run again afterwards; then ~6 min more in the checks measuring the base
commit's whole suite because 2 stale tests fail on every commit. These pin the fixes.
"""
import json
import types

import pytest

from automation.scenarios import cross_app_flows as caf
from automation.scenarios.cross_app_flows import FlowRunner


# ── @see:<text> — a fast outcome check ───────────────────────────────────────

def _el(label, ident=""):
    return {"AXLabel": label, "AXIdentifier": ident, "type": "StaticText",
            "frame": {"x": 0, "y": 0, "width": 100, "height": 20}}


@pytest.fixture
def screen(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(caf, "time", types.SimpleNamespace(
        time=lambda: clock["t"], sleep=lambda s: clock.__setitem__("t", clock["t"] + s)))
    from automation.scenarios import screen_text
    state = {"els": [], "ocr": [], "reads": 0}

    def describe_all(udid):
        state["reads"] += 1
        return state["els"]

    monkeypatch.setattr(caf._idbd, "describe_all", describe_all)
    monkeypatch.setattr(screen_text, "read_text",
                        lambda udid, region=None, els=None, accurate=False: state["ocr"])
    runner = FlowRunner.__new__(FlowRunner)
    runner._cur_udid = "IPAD"
    runner.devices = {}
    return runner, state, clock


def test_see_is_dispatched_with_its_text(screen):
    runner, state, _ = screen
    state["els"] = [_el("Invoice")]
    notes = []
    assert FlowRunner._handle_special(runner, None, "@see:Invoice", notes)
    assert "[ok] @see:Invoice" in notes[-1]
    from automation.agentic.runinfo import token_ok
    assert token_ok("@see:Sorry, only 4 seats")


def test_see_matches_part_of_a_label_ignoring_case_and_punctuation(screen):
    runner, state, _ = screen
    state["els"] = [_el("My Orders"), _el("2 result(s) shown for « Reserved »", "resultLine")]
    notes = []
    assert runner._see_text("RESULTS shown", notes)
    assert "2 result(s) shown" in notes[-1]


def test_see_reads_the_pixels_for_overlay_text(screen):
    """A magnus Overlay hides its text from idb (locator_map.md)."""
    runner, state, _ = screen
    state["ocr"] = [("Sorry, Indoor only has 12 seats", 10, 10, 200, 20)]
    notes = []
    assert runner._see_text("only has 12 seats", notes)
    assert "read by OCR" in notes[-1]


def test_see_fails_fast_and_says_so(screen):
    runner, state, clock = screen
    state["els"] = [_el("My Orders")]
    notes = []
    assert not runner._see_text("Invoice", notes)
    assert "[FAIL] @see:Invoice — not on screen after 12s" in notes[-1]
    assert clock["t"] <= FlowRunner.SEE_SECS + 1 < 49        # verify took 49s (cbc65fce)


def test_a_see_step_is_a_value_check_the_gates_protect():
    from automation.agentic.runinfo import CHECK_STEP
    assert CHECK_STEP.search("@see:Payment Completed")
    assert not CHECK_STEP.search("click seeMoreBtn")


# ── `steps`: the step reference, read from the engine ────────────────────────

def test_steps_lists_forms_tokens_with_handlers_and_shared_blocks(capsys):
    import importlib.util, os
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "agent_tools.py")
    spec = importlib.util.spec_from_file_location("agent_tools_under_test", path)
    tools = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tools)
    assert tools.main(["steps"]) == 0
    out = capsys.readouterr().out
    assert "click <id>" in out and "?<step>" in out
    assert "@see:Invoice" in out and "_see_text" in out
    assert "@open_order" in out and "_open_reservation" in out    # the branch, not the next one
    assert "_C_BOOK_PREFIX  (consumer; used by" in out and "flow1" in out
    assert "click NylaiKitchen2" in out and "-> @open_restaurant" in out


# ── the prompt carries what the analysis already found ───────────────────────

FMAP = {"apps": {
    "business": [{"feature": "History: look up a past order's invoice", "role": "waiter",
                  "status": "missing", "risk": "medium",
                  "screens": ["App/Screens/History/index.js"], "ids": ["historyBtn", "historyCard"],
                  "covered_by": [], "scenario": "Open History, pick an order, see its invoice"}],
    "consumer": [{"feature": "Book a table: party size, dining area", "role": "consumer",
                  "status": "partial", "risk": "high",
                  "screens": ["App/Screens/StoreView/Reservation.js"], "ids": ["counterPlus", "Indoor"],
                  "covered_by": ["flow1"], "gap": "Indoor never picked"}]}}


def _ctx():
    return {"session_dir": "/s", "worktree": "/wt", "branch": "agent/x", "date": "2026-10-27",
            "stop_by": "03:40", "deadline": "04:00", "known_failing_tests": "none recorded"}


def test_picked_features_come_with_their_screens_and_ids(monkeypatch):
    from automation.claude_code import prompts, session
    monkeypatch.setattr(session, "latest_feature_map", lambda: FMAP)
    picked = ["[business] History: look up a past order's invoice: no flow opens History",
              "[consumer] Book a table: party size, dining area: Indoor never picked"]
    text = prompts.build("generate", {"features": picked}, _ctx(), {"max_new_scenarios": 3})
    assert "screens: App/Screens/History/index.js" in text and "ids: historyBtn, historyCard" in text
    assert "ids: counterPlus, Indoor" in text and "closest flows: flow1" in text
    assert "scenario idea: Open History" in text


def test_a_feature_the_map_no_longer_has_is_still_passed_on(monkeypatch):
    from automation.claude_code import prompts, session
    monkeypatch.setattr(session, "latest_feature_map", lambda: FMAP)
    text = prompts.build("generate", {"features": ["[consumer] Coupons: never applied"]},
                         _ctx(), {"max_new_scenarios": 3})
    assert "- [consumer] Coupons: never applied" in text


def test_ticking_ten_gaps_asks_for_ten_scenarios_in_time_for_ten(monkeypatch):
    from automation.claude_code import prompts, session
    monkeypatch.setattr(session, "latest_feature_map", lambda: FMAP)
    picked = [f"[consumer] Feature {i}: gap" for i in range(10)]
    s = {"max_new_scenarios": 3, "hours": {"generate": 3.0}}
    assert "At most **10** new scenarios" in prompts.build("generate", {"features": picked}, _ctx(), s)
    assert "At most **3** new scenarios" in prompts.build("generate", {}, _ctx(), s)
    assert "At most **2** new" in prompts.build("generate", {"features": picked, "max_new": 2}, _ctx(), s)
    from automation.claude_code import runner
    assert runner.session_hours("generate", {"features": picked}, s) == 3.0
    assert runner.session_hours("generate", {"features": picked * 2}, s) == pytest.approx(4.5)
    assert runner.session_hours("generate", {"max_new": 100}, s) == 8.0
    assert runner.session_hours("heal", {"features": picked * 5}, {"hours": {"heal": 4.0}}) == 4.0


def test_the_prompt_points_at_the_authoring_guide_not_at_the_whole_suite():
    from automation.claude_code import prompts
    text = prompts.build("generate", {"focus": "History"}, _ctx(), {"max_new_scenarios": 3})
    assert "scenario_authoring.md" in text and "`steps`" in text
    assert "never the whole suite" in text


# ── the base commit's failures: only the tests that failed ───────────────────

@pytest.fixture
def base(tmp_path, monkeypatch):
    from automation.claude_code import baselines, paths
    monkeypatch.setattr(paths, "BASELINES_DIR", str(tmp_path / "baselines"))
    calls = []
    on_base = {"failed": set(), "absent": set()}

    def measure(commit, on_phase, tests=None):
        calls.append(None if tests is None else list(tests))
        if tests is None:
            return {"failed": sorted(on_base["failed"]), "counts": {"failed": 1}, "returncode": 1}
        there = [t for t in tests if t not in on_base["absent"]]
        return {"failed": [t for t in there if t in on_base["failed"]], "returncode": 1,
                "absent": [t for t in tests if t in on_base["absent"]]}

    monkeypatch.setattr(baselines, "_measure", measure)
    return baselines, calls, on_base


STALE = ["tests/test_consumer_checkout.py::test_a", "tests/test_consumer_checkout.py::test_b"]


def test_known_failures_need_only_those_tests_on_the_base(base):
    baselines, calls, on_base = base
    on_base["failed"] = set(STALE)
    got = baselines.get_or_measure("c1", tests=STALE)
    assert calls == [STALE]                                   # never the full suite
    assert set(got["failed"]) == set(STALE)
    again = baselines.get_or_measure("c1", tests=STALE)       # cached for the commit
    assert calls == [STALE] and set(again["failed"]) == set(STALE)


def test_a_test_the_session_added_is_new_without_measuring_the_whole_base(base):
    from automation.claude_code import gates
    baselines, calls, on_base = base
    on_base["failed"], on_base["absent"] = set(STALE), {"tests/test_new.py::test_x"}
    failing = STALE + ["tests/test_new.py::test_x"]
    got = baselines.get_or_measure("c2", tests=failing)
    assert calls == [failing]
    g = gates.unit_gate({"failed": failing, "returncode": 1, "counts": {}}, got)
    assert not g["ok"] and "tests/test_new.py::test_x" in g["detail"] and "test_a" not in g["detail"]


def test_a_failure_that_looks_new_is_confirmed_on_the_whole_base_suite(base):
    baselines, calls, on_base = base
    on_base["failed"] = set(STALE)
    failing = STALE + ["tests/test_pay.py::test_y"]           # passes on the base alone
    got = baselines.get_or_measure("c3", tests=failing)
    assert calls == [failing, None]
    assert "partial" not in got and set(got["failed"]) == set(STALE)


def test_a_full_baseline_is_used_as_it_is(base, tmp_path):
    baselines, calls, _ = base
    baselines.save("c4", {"failed": STALE, "counts": {"failed": 2}})
    assert set(baselines.get_or_measure("c4", tests=STALE)["failed"]) == set(STALE)
    assert calls == []


def test_in_tree_knows_a_test_the_base_does_not_have(tmp_path):
    from automation.claude_code import baselines
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_old():\n    pass\n")
    assert baselines._in_tree(str(tmp_path), "tests/test_a.py::test_old")
    assert baselines._in_tree(str(tmp_path), "tests/test_a.py::test_old[2]")
    assert not baselines._in_tree(str(tmp_path), "tests/test_a.py::test_new")
    assert not baselines._in_tree(str(tmp_path), "tests/test_b.py::test_old")


# ── a pass stays valid until something it ran changes ────────────────────────

ENGINE = '''
FLOWS = {"cc_a": {"id": "cc_a", "segments": []}}
STEP_CATALOG = {"Handlers (@)": []}
_C_BLOCK = ["open app"]
SEE_SECS = 12


class FlowRunner:
    _PLAIN_STEP_TOKENS = {"click Checkout": "@checkout"}

    def _run_segment(self, seg):
        return self._handle_special(None, "x", [])

    def _see_text(self, text, notes):
        return SEE_SECS > 0

    def _checkout(self, notes):
        return self._helper()

    def _helper(self):
        return 1

    def _handle_special(self, r, step, notes):
        if step.startswith("@see:"):
            return self._see_text(step[5:], notes)
        if step == "@checkout":
            return self._checkout(notes)
        notes.append("unknown")
        return False
'''

SEGS_A = [["open app", "@see:Invoice"], ["click Checkout"]]       # uses @see: and @checkout (alias)
SEGS_B = [["open app", "@see:Total"]]                             # uses only @see:


@pytest.fixture
def code(tmp_path, monkeypatch):
    from automation.claude_code import fingerprint
    monkeypatch.setattr(fingerprint, "DIR", str(tmp_path / "fp"))
    root = tmp_path / "wt"
    (root / "automation" / "scenarios").mkdir(parents=True)
    (root / "automation" / "scenarios" / "idb_driver.py").write_text("def tap():\n    pass\n")
    src_file = root / "automation" / "scenarios" / "cross_app_flows.py"
    src_file.write_text(ENGINE)

    def run(run_id, segs, start=0, resumed_from=None):
        fingerprint.record(run_id, fingerprint.take(str(root), segs, start, resumed_from))

    def current(run_id, segs):
        src = src_file.read_text()
        return fingerprint.run_is_current(
            run_id, fingerprint.segment_hashes(segs), fingerprint.units(src),
            fingerprint.other_code(str(root)), [s for seg in segs for s in seg], src,
            {"click Checkout": "@checkout"})
    return src_file, run, current, root


def test_editing_another_draft_does_not_undo_a_pass(code):
    """cf6bd414: two passes refused because a third draft was edited and removed later."""
    src_file, run, current, _ = code
    run("r_a", SEGS_A)
    src_file.write_text(ENGINE.replace('"segments": []}}', '"segments": []}, "cc_c": {}}'))
    assert current("r_a", SEGS_A) is True
    assert current("r_a", [["open app", "@see:Invoice"], ["click Checkout", "@see:Paid"]]) is False


def test_a_handler_change_undoes_only_the_passes_of_flows_using_it(code):
    src_file, run, current, _ = code
    run("r_a", SEGS_A)
    run("r_b", SEGS_B)
    src_file.write_text(ENGINE.replace("return 1", "return 2"))     # _helper, under @checkout
    assert current("r_a", SEGS_A) is False
    assert current("r_b", SEGS_B) is True


def test_a_new_token_branch_does_not_undo_flows_that_do_not_use_it(code):
    src_file, run, current, _ = code
    run("r_a", SEGS_A)
    src_file.write_text(ENGINE.replace(
        '        notes.append("unknown")',
        '        if step == "@filter_table":\n            return self._helper()\n'
        '        notes.append("unknown")'))
    assert current("r_a", SEGS_A) is True


def test_a_constant_undoes_the_flows_whose_handlers_read_it(code):
    src_file, run, current, _ = code
    run("r_a", SEGS_A)
    src_file.write_text(ENGINE.replace("SEE_SECS = 12", "SEE_SECS = 20"))
    assert current("r_a", SEGS_A) is False
    run("r_a2", SEGS_A)
    src_file.write_text(ENGINE.replace("SEE_SECS = 12", "SEE_SECS = 20")
                        .replace('"Handlers (@)": []', '"Handlers (@)": [{"step": "@x"}]'))
    assert current("r_a2", SEGS_A) is True                      # the editor's menu is data


def test_engine_core_and_other_device_code_undo_every_pass(code):
    src_file, run, current, root = code
    run("r_b", SEGS_B)
    src_file.write_text(ENGINE.replace('"x", []', '"y", []'))           # _run_segment
    assert current("r_b", SEGS_B) is False
    src_file.write_text(ENGINE)
    assert current("r_b", SEGS_B) is True
    (root / "automation" / "scenarios" / "idb_driver.py").write_text("def tap():\n    return 1\n")
    assert current("r_b", SEGS_B) is False


def test_a_resumed_pass_counts_when_the_runs_it_continued_ran_the_same_code(code):
    src_file, run, current, _ = code
    run("r1", [["open app", "@see:Invoice"], ["click Checkout", "@see:Wrong"]])   # failed in seg 2
    fixed = [["open app", "@see:Invoice"], ["click Checkout", "@see:Paid"]]
    run("r2", fixed, start=1, resumed_from="r1")
    assert current("r2", fixed) is True
    changed_first = [["open app", "@see:Total"], ["click Checkout", "@see:Paid"]]
    run("r3", changed_first, start=1, resumed_from="r1")
    assert current("r3", changed_first) is False                # segment 1 never ran like this


def test_a_run_without_a_fingerprint_falls_back_to_the_last_edit(code):
    from automation.claude_code import gates
    _, _, current, _ = code
    assert current("never-recorded", SEGS_A) is None
    runs = [{"flow_id": "cc_a", "run_id": "x", "status": "passed", "t0": 100.0}]
    assert not gates.device_gate(runs, 200.0, ["cc_a"], None, current=lambda r: None)["ok"]
    assert gates.device_gate(runs, 200.0, ["cc_a"], None, current=lambda r: True)["ok"]
    assert not gates.device_gate(runs, 50.0, ["cc_a"], None, current=lambda r: False)["ok"]


def test_the_session_dump_carries_each_flows_segments():
    from automation.claude_code import gates
    import os
    root = os.path.dirname(os.path.dirname(__file__))
    d = gates.dump_flows(root)
    assert d["segments"]["flow1"][0][:2] == ["open app", "@consumer_home"]
    assert [s for seg in d["segments"]["flow1"] for s in seg] == d["flows"]["flow1"]
