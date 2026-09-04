"""Phase 4F.6 — performance data crosses the agent/backend boundary over HTTP.

The collector used to open a session on the agent and write performance_metrics
and performance_summaries itself. It now produces a bounded payload and the
backend owns the write, behind the same per-agent credential and ownership check
Phase 4F.3 established for scenario results.

A caveat these tests cannot remove: the OLD path produced zero rows in production
across 158 runs, so there is no observed before/after behaviour to compare. These
tests are the parity guarantee, not a replay of a working system.
"""
import ast
import inspect
from datetime import datetime
import subprocess
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from automation.agent import main as agent_main
from automation.api.v1.routers import agents as ar
from automation.api.v1.routers import jobs as jr
from automation.auth import security
from automation.database.models import (Base, PerformanceMetric, PerformanceSummary,
                                        TestProject, TestRun)
from automation.performance import collector as pc


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'perf.db'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    import automation.database.config as cfg
    monkeypatch.setattr(cfg, "SessionLocal", Session)
    import automation.device_manager.service as dm
    monkeypatch.setattr(dm, "_backend_machine_id", None)
    db = Session()
    db.add(TestProject(id="proj-1", name="Demo", git_url="https://example.com/x.git"))
    db.commit()
    yield db, Session
    db.close()


def _agent(db, hostname):
    out = ar.register_agent(ar.RegisterAgentRequest(hostname=hostname, os="Darwin",
                                                    capabilities={}, connected_devices=[]), db=db)
    return out["id"], out["agent_credential"]


def _auth(db, cred):
    return security.authenticated_agent(request=None, x_agent_credential=cred, db=db)


def _run(db, run_id, agent_id=None):
    db.add(TestRun(id=run_id, project_id="proj-1", test_suite="s", test_name=run_id,
                   status="running", job_state="running", device_name="UDID-X",
                   agent_id=agent_id))
    db.commit()
    return run_id


def _sample(i, **over):
    d = dict(timestamp=f"2026-09-04T06:{i // 60:02d}:{i % 60:02d}.000000",
             cpu_percent=10.0 + i, memory_mb=200.0 + i, fps=None,
             network_requests=i, avg_response_ms=100.0 + i)
    d.update(over)
    return d


def _collector(n_samples=3, **kw):
    """A real collector with a fabricated sample series — no device, no thread."""
    c = pc.PerformanceCollector(device_id="UDID-X", run_id=kw.pop("run_id", "run-1"),
                                bundle_id="com.example.app")
    c.metrics = [_sample(i) for i in range(n_samples)]
    c.app_launch_time_s = 1.4
    return c


def _post(db, run_id, payload, cred):
    return jr.post_run_performance(
        run_id, jr.PerformanceReportIn(**payload), db=db, agent=_auth(db, cred))


def _rows(db, run_id):
    db.expire_all()
    return (db.query(PerformanceMetric).filter(PerformanceMetric.run_id == run_id)
            .order_by(PerformanceMetric.timestamp.asc()).all())


def _summary_row(db, run_id):
    db.expire_all()
    return db.query(PerformanceSummary).filter(PerformanceSummary.run_id == run_id).first()


def _identifiers(obj):
    """Names the CODE mentions, with docstrings dropped — prose is not a boundary."""
    tree = ast.parse(inspect.getsource(obj))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body = node.body[1:] or [ast.Pass()]
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    return names


# ── 1. no database dependency ───────────────────────────────────────────────

def test_01_collector_import_graph_has_no_database_or_web_framework():
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys, automation.performance.collector; print(chr(10).join(sys.modules))"],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-2000:]
    g = set(out.stdout.split())
    offenders = sorted(m for m in g
                       if m.split(".")[0] in ("fastapi", "sqlalchemy")
                       or m.startswith("automation.database")
                       or m.startswith("automation.api"))
    assert not offenders, f"the collector pulls in {offenders}"


def test_01b_collector_names_no_session_or_model():
    names = _identifiers(pc)
    for banned in ("SessionLocal", "PerformanceMetric", "PerformanceSummary", "get_db"):
        assert banned not in names, f"the collector still references {banned}"
    for mod, _n in [(n.module or "", None) for n in ast.walk(ast.parse(inspect.getsource(pc)))
                    if isinstance(n, ast.ImportFrom)]:
        assert not mod.startswith("automation.database"), f"collector imports from {mod}"


# ── 2. payload round trip ───────────────────────────────────────────────────

def test_02_every_persisted_column_has_a_source_in_the_payload():
    payload = _collector(3).payload()
    assert set(payload) == {"summary", "samples", "sample_interval_s", "downsampled_from"}

    summary_cols = {c.name for c in PerformanceSummary.__table__.columns} - {
        "id", "run_id", "created_at"}
    missing = summary_cols - set(payload["summary"])
    assert not missing, f"summary cannot populate {missing}"

    metric_cols = {c.name for c in PerformanceMetric.__table__.columns} - {"id", "run_id"}
    missing = metric_cols - set(payload["samples"][0])
    assert not missing, f"samples cannot populate {missing}"

    assert payload["sample_interval_s"] == pc.SAMPLE_INTERVAL_S
    assert payload["downsampled_from"] is None


def test_02b_the_request_model_accepts_exactly_what_the_collector_sends():
    payload = _collector(3).payload()
    parsed = jr.PerformanceReportIn(**payload)
    assert len(parsed.samples) == 3
    assert parsed.summary.performance_score == payload["summary"]["performance_score"]


# ── 3. backend persistence parity ───────────────────────────────────────────

def test_03_the_backend_writes_exactly_what_the_collector_measured(env):
    db, _ = env
    a, cred = _agent(db, "mac-a"); _run(db, "run-1", a)
    c = _collector(3)
    payload = c.payload()

    ack = _post(db, "run-1", payload, cred)
    assert ack == {"run_id": "run-1", "samples_written": 3, "summary_written": True}

    row = _summary_row(db, "run-1")
    for col in {c_.name for c_ in PerformanceSummary.__table__.columns} - {
            "id", "run_id", "created_at"}:
        assert getattr(row, col) == payload["summary"][col], f"summary.{col} differs"

    metrics = _rows(db, "run-1")
    assert len(metrics) == 3
    for got, sent in zip(metrics, payload["samples"]):
        assert got.cpu_percent == sent["cpu_percent"]
        assert got.memory_mb == sent["memory_mb"]
        assert got.fps == sent["fps"]
        assert got.network_requests == sent["network_requests"]
        assert got.avg_response_ms == sent["avg_response_ms"]
        assert got.timestamp == datetime.fromisoformat(sent["timestamp"])


# ── 4. downsampling ─────────────────────────────────────────────────────────

def test_04_three_thousand_samples_are_thinned_not_truncated():
    c = _collector(3000)
    first, last = c.metrics[0]["timestamp"], c.metrics[-1]["timestamp"]
    payload = c.payload()

    assert len(payload["samples"]) <= pc.MAX_SAMPLES
    assert payload["downsampled_from"] == 3000
    assert payload["samples"][0]["timestamp"] == first
    assert payload["samples"][-1]["timestamp"] == last


def test_04b_downsampling_is_deterministic_and_unbiased():
    a = _collector(3000).payload()["samples"]
    b = _collector(3000).payload()["samples"]
    assert [s["timestamp"] for s in a] == [s["timestamp"] for s in b], "not deterministic"

    # An evenly-spaced thin puts about half the picks in each half of the run;
    # truncation or head-bias would put nearly all of them in the first half.
    cpus = [s["cpu_percent"] for s in a]           # cpu_percent == 10 + index
    second_half = sum(1 for v in cpus if v - 10 >= 1500)
    assert 0.45 <= second_half / len(cpus) <= 0.55, \
        f"{second_half}/{len(cpus)} samples from the run's second half — biased"


@pytest.mark.parametrize("n,expect_sent,expect_from", [
    (0, 0, None), (1, 1, None), (1999, 1999, None),
    (2000, 2000, None), (2001, 2000, 2001),
])
def test_04c_the_cap_boundary(n, expect_sent, expect_from):
    payload = _collector(n).payload()
    assert len(payload["samples"]) == expect_sent
    assert payload["downsampled_from"] == expect_from


def test_04d_zero_samples_still_produces_a_summary(env):
    db, _ = env
    a, cred = _agent(db, "mac-a"); _run(db, "run-1", a)
    payload = _collector(0).payload()
    ack = _post(db, "run-1", payload, cred)
    assert ack["samples_written"] == 0 and ack["summary_written"] is True
    assert _summary_row(db, "run-1") is not None


# ── issue cap + endpoint truncation ─────────────────────────────────────────

def test_04e_the_issue_list_is_capped_and_says_how_many_were_dropped():
    c = _collector(1)
    c._issues = [f"issue {i}" for i in range(200)]
    issues = c.get_summary()["issues"]
    assert len(issues) <= pc.MAX_ISSUES
    assert issues[-1].startswith("+") and "more issues" in issues[-1]
    omitted = int(issues[-1].split()[0].lstrip("+"))
    assert len(issues) - 1 + omitted == 200, "the dropped count must be exact"


def test_04f_a_short_issue_list_is_untouched():
    c = _collector(1)
    c._issues = ["only one"]
    assert c.get_summary()["issues"] == ["only one"]


def test_04g_the_slowest_endpoint_is_bounded_to_its_column(env):
    db, _ = env
    a, cred = _agent(db, "mac-a"); _run(db, "run-1", a)
    long_url = "https://example.com/" + "x" * 900

    class _Api:
        def get_api_summary(self):
            return {"total_calls": 1, "avg_ms": 12.0,
                    "slowest": {"url": long_url, "ms": 12.0}, "failed": [], "by_endpoint": {}}

    c = _collector(1)
    c._api = _Api()
    payload = c.payload()
    assert len(payload["summary"]["slowest_api_endpoint"]) == pc.MAX_ENDPOINT_CHARS

    _post(db, "run-1", payload, cred)      # must not raise a column-width error
    assert len(_summary_row(db, "run-1").slowest_api_endpoint) == pc.MAX_ENDPOINT_CHARS


# ── 5. reporting failure is not a test failure ──────────────────────────────

def test_05_a_failing_post_never_raises_and_never_reports_success(monkeypatch):
    def _boom(*a, **k):
        raise ConnectionError("backend unreachable")
    monkeypatch.setattr(agent_main.HTTP, "post", _boom)
    monkeypatch.setattr("time.sleep", lambda s: None)

    assert agent_main.report_performance("run-1", _collector(3).payload()) is False


def test_05b_both_agent_call_sites_sit_inside_a_handler(monkeypatch):
    """A raising report must not escape into the job's verdict."""
    fn = ast.parse(inspect.getsource(agent_main.run_job)).body[0]
    parent = {}
    for node in ast.walk(fn):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "report_performance"]
    assert len(calls) == 2, f"expected both call sites, found {len(calls)}"

    for call in calls:
        node, handled = call, False
        while node in parent:
            node = parent[node]
            if isinstance(node, ast.Try) and node.handlers:
                handled = True
                break
        assert handled, "a report_performance call can escape into the job's verdict"


def test_05c_a_garbage_response_body_is_not_treated_as_success(monkeypatch):
    class _Res:
        status_code = 200
        text = "not json"
        def json(self): raise ValueError("no json")
    monkeypatch.setattr(agent_main.HTTP, "post", lambda *a, **k: _Res())
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert agent_main.report_performance("run-1", _collector(1).payload()) is False


# ── 6. partial metrics ──────────────────────────────────────────────────────

def test_06_all_none_probes_still_build_a_scored_summary(env):
    db, _ = env
    a, cred = _agent(db, "mac-a"); _run(db, "run-1", a)
    c = pc.PerformanceCollector(device_id="UDID-X", run_id="run-1")
    c.metrics = [_sample(i, cpu_percent=None, memory_mb=None, fps=None,
                         avg_response_ms=None) for i in range(3)]
    payload = c.payload()
    s = payload["summary"]
    assert s["avg_cpu_percent"] == 0.0 and s["avg_fps"] is None
    assert s["app_launch_time_s"] is None
    assert isinstance(s["performance_score"], int) and s["grade"] in "ABCDF"

    ack = _post(db, "run-1", payload, cred)
    assert ack["samples_written"] == 3
    assert _summary_row(db, "run-1").avg_fps is None


# ── 7. the existing GET is unchanged ────────────────────────────────────────

def test_07_the_read_endpoint_keeps_its_shape(env):
    db, _ = env
    a, cred = _agent(db, "mac-a"); _run(db, "run-1", a)
    _post(db, "run-1", _collector(3).payload(), cred)

    out = jr.get_run_performance("run-1", db=db, current_user=None)
    assert set(out) >= {"summary", "metrics_over_time", "api_calls", "comparison"}
    assert len(out["metrics_over_time"]) == 3
    assert set(out["metrics_over_time"][0]) == {"timestamp", "cpu", "memory", "fps"}
    assert out["summary"]["performance_score"] is not None
    assert set(out["api_calls"]) == {"total_calls", "avg_ms", "slowest"}


def test_07b_a_run_with_no_performance_data_still_404s(env):
    from fastapi import HTTPException
    db, _ = env
    _agent(db, "mac-a"); _run(db, "run-empty")
    with pytest.raises(HTTPException) as e:
        jr.get_run_performance("run-empty", db=db, current_user=None)
    assert e.value.status_code == 404


# ── 8. the agent opens no session ───────────────────────────────────────────

def test_08_the_agent_performance_path_opens_no_database_session(env, monkeypatch):
    """Real path, SessionLocal booby-trapped — the Phase 4F.4a technique."""
    import automation.database.config as cfg
    opened = []

    def _trap():
        opened.append(True)
        raise AssertionError("the agent opened a session for performance")
    monkeypatch.setattr(cfg, "SessionLocal", _trap)

    posted = {}

    class _Res:
        status_code = 200
        text = ""
        def json(self): return {"run_id": "run-1", "samples_written": len(posted["json"]["samples"]),
                                "summary_written": True}
    monkeypatch.setattr(agent_main.HTTP, "post",
                        lambda url, json=None, timeout=None: posted.update(url=url, json=json) or _Res())

    c = _collector(5)
    assert agent_main.report_performance("run-1", c.payload()) is True
    assert not opened
    assert posted["url"].endswith("/runs/run-1/performance")


# ── 9. resubmission replaces, never duplicates ──────────────────────────────

def test_09_posting_the_same_payload_twice_does_not_duplicate_the_series(env):
    db, _ = env
    a, cred = _agent(db, "mac-a"); _run(db, "run-1", a)
    payload = _collector(4).payload()

    _post(db, "run-1", payload, cred)
    _post(db, "run-1", payload, cred)

    assert len(_rows(db, "run-1")) == 4, "the time series was duplicated by a retry"
    assert db.query(PerformanceSummary).filter(
        PerformanceSummary.run_id == "run-1").count() == 1


def test_09b_a_second_report_replaces_the_first(env):
    db, _ = env
    a, cred = _agent(db, "mac-a"); _run(db, "run-1", a)
    _post(db, "run-1", _collector(4).payload(), cred)

    later = _collector(2).payload()
    later["summary"]["performance_score"] = 42
    later["summary"]["grade"] = "D"
    _post(db, "run-1", later, cred)

    assert len(_rows(db, "run-1")) == 2
    row = _summary_row(db, "run-1")
    assert row.performance_score == 42 and row.grade == "D"


def test_09c_one_run_s_report_does_not_disturb_another(env):
    db, _ = env
    a, cred = _agent(db, "mac-a"); _run(db, "run-1", a); _run(db, "run-2", a)
    _post(db, "run-1", _collector(3).payload(), cred)
    _post(db, "run-2", _collector(5).payload(), cred)
    _post(db, "run-2", _collector(5).payload(), cred)

    assert len(_rows(db, "run-1")) == 3
    assert len(_rows(db, "run-2")) == 5


# ── 10. ownership ───────────────────────────────────────────────────────────

def test_10_another_agents_run_is_refused_and_writes_nothing(env):
    from fastapi import HTTPException
    db, _ = env
    a, _ca = _agent(db, "mac-a")
    _b, cb = _agent(db, "mac-b")
    _run(db, "run-1", a)

    with pytest.raises(HTTPException) as e:
        _post(db, "run-1", _collector(3).payload(), cb)
    assert e.value.status_code == 403
    assert _rows(db, "run-1") == []
    assert _summary_row(db, "run-1") is None


def test_10b_an_unknown_run_is_a_404(env):
    from fastapi import HTTPException
    db, _ = env
    _a, cred = _agent(db, "mac-a")
    with pytest.raises(HTTPException) as e:
        _post(db, "nope", _collector(1).payload(), cred)
    assert e.value.status_code == 404


def test_10c_an_unclaimed_run_is_writable(env):
    """A run with no agent_id is not owned by anyone — preserve that."""
    db, _ = env
    _a, cred = _agent(db, "mac-a"); _run(db, "run-free", None)
    assert _post(db, "run-free", _collector(2).payload(), cred)["samples_written"] == 2


def test_10d_the_endpoint_requires_a_proven_agent():
    sig = inspect.signature(jr.post_run_performance)
    dep = sig.parameters["agent"].default
    assert dep.dependency is security.require_authenticated_agent, \
        "the write endpoint must not fall back to the weaker require_agent"


# ── acknowledgement + retry discipline ──────────────────────────────────────

class _Response:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body or {}, text
    def json(self):
        return self._body


def _client(monkeypatch, responses):
    """Drive report_performance against a scripted sequence of responses."""
    calls = []
    seq = list(responses)

    def _post_(url, json=None, timeout=None):
        calls.append(url)
        r = seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(agent_main.HTTP, "post", _post_)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return calls


def test_11_a_short_count_is_reported_as_failure(monkeypatch):
    calls = _client(monkeypatch, [_Response(200, {"samples_written": 2, "summary_written": True})])
    assert agent_main.report_performance("run-1", _collector(3).payload()) is False
    assert len(calls) == 1, "a wrong count is a server problem, not a transient one"


def test_11b_summary_written_false_is_reported_as_failure(monkeypatch):
    _client(monkeypatch, [_Response(200, {"samples_written": 3, "summary_written": False})])
    assert agent_main.report_performance("run-1", _collector(3).payload()) is False


def test_11c_a_matching_count_is_success(monkeypatch):
    _client(monkeypatch, [_Response(200, {"samples_written": 3, "summary_written": True})])
    assert agent_main.report_performance("run-1", _collector(3).payload()) is True


def test_12_a_413_is_not_retried(monkeypatch):
    calls = _client(monkeypatch, [_Response(413, text="too large")])
    assert agent_main.report_performance("run-1", _collector(3).payload()) is False
    assert len(calls) == 1


def test_12b_a_403_is_not_retried(monkeypatch):
    calls = _client(monkeypatch, [_Response(403, text="claimed by another agent")])
    assert agent_main.report_performance("run-1", _collector(3).payload()) is False
    assert len(calls) == 1


def test_12c_a_transient_5xx_is_retried_then_succeeds(monkeypatch):
    calls = _client(monkeypatch, [
        _Response(503, text="unavailable"),
        _Response(200, {"samples_written": 3, "summary_written": True}),
    ])
    assert agent_main.report_performance("run-1", _collector(3).payload()) is True
    assert len(calls) == 2


def test_12d_a_network_error_is_retried_up_to_the_bound(monkeypatch):
    calls = _client(monkeypatch, [ConnectionError("x"), ConnectionError("y"), ConnectionError("z")])
    assert agent_main.report_performance("run-1", _collector(3).payload()) is False
    assert len(calls) == 3, "bounded retry must stop at 3 attempts"


# ── malformed input ─────────────────────────────────────────────────────────

def test_13_a_malformed_sample_is_rejected_by_the_schema():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        jr.PerformanceReportIn(summary={}, samples=[{"cpu_percent": "not-a-number"}])


def test_13b_an_unparseable_timestamp_does_not_break_the_write(env):
    db, _ = env
    a, cred = _agent(db, "mac-a"); _run(db, "run-1", a)
    payload = _collector(2).payload()
    payload["samples"][0]["timestamp"] = "definitely not a date"
    assert _post(db, "run-1", payload, cred)["samples_written"] == 2
    assert len(_rows(db, "run-1")) == 2


def test_13c_the_endpoint_takes_named_fields_not_an_open_dict():
    fields = set(jr.PerformanceReportIn.model_fields)
    assert fields == {"summary", "samples", "sample_interval_s", "downsampled_from"}
    assert not any(f.annotation is dict for f in jr.PerformanceReportIn.model_fields.values())


# ── no double reporting from the two agent paths ────────────────────────────

def test_14_the_error_path_only_reports_for_a_collector_still_running():
    """stop() clears is_collecting, so the finally block cannot report a second time."""
    src = inspect.getsource(agent_main.run_job)
    fn = ast.parse(src).body[0]
    finals = [h for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.finalbody]
    guards = [n for n in ast.walk(ast.Module(body=finals, type_ignores=[]))
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "getattr"
              and any(isinstance(a, ast.Constant) and a.value == "is_collecting" for a in n.args)]
    assert guards, "the finally block no longer checks is_collecting — it can double-report"


def test_14b_stop_clears_the_flag_the_guard_reads():
    c = _collector(1)
    c.is_collecting = True
    c.stop()
    assert c.is_collecting is False


# ── the obsolete writer is gone ─────────────────────────────────────────────

def test_15_save_to_db_is_gone_and_nothing_calls_it():
    assert not hasattr(pc.PerformanceCollector, "save_to_db")
    assert "save_to_db" not in inspect.getsource(agent_main)
