"""Waiter demo failed with 'could not log in as waiter … (check creds …)' when the
real cause was an empty waiter password, and its AI summary said the run
'finished with status running' because it was written mid-run and cached.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from automation.ai.services import summary as summ
from automation.scenarios import cross_app_config as cfgmod
from automation.scenarios.cross_app_flows import FlowRunner


def test_missing_password_is_named_with_where_to_set_it():
    runner = FlowRunner.__new__(FlowRunner)
    runner.credentials = {"waiter": {"email": "w@example.com", "password": ""}}
    notes = []
    assert runner._login_business(None, "waiter", notes) is False
    msg = " ".join(notes)
    assert "no waiter password" in msg
    assert "VYA_BUSINESS_WAITER_PASSWORD" in msg and "Run Cross-App Suite" in msg


def test_env_login_fills_blanks_of_a_saved_config(tmp_path, monkeypatch):
    path = tmp_path / "cfg.json"
    path.write_text('{"credentials": {"waiter": {"email": "saved@example.com", "password": ""}}}')
    monkeypatch.setattr(cfgmod, "_CONFIG_PATH", path)
    monkeypatch.setattr(cfgmod, "localize_devices", lambda d: d)
    monkeypatch.setenv("VYA_BUSINESS_WAITER_USER", "env@example.com")
    monkeypatch.setenv("VYA_BUSINESS_WAITER_PASSWORD", "from-env")
    c = cfgmod.load_config()["credentials"]["waiter"]
    assert c == {"email": "saved@example.com", "password": "from-env"}, \
        "saved values win; only blanks come from .env"


class _Q:
    def __init__(self, obj):
        self.obj = obj

    def filter(self, *a):
        return self

    def first(self):
        return self.obj


class _DB:
    def __init__(self, run):
        self.run = run

    def query(self, model):
        return _Q(self.run if model.__name__ == "TestRun" else None)

    def commit(self):
        pass

    def rollback(self):
        pass


def _run(**kw):
    base = dict(id="r", status="running", test_name="Waiter demo", test_suite=None,
                device_name="iPad", os_version="26.5", duration_ms=0, error_message=None,
                report_summary=None, report_generated_at=None, completed_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_running_run_summary_is_not_stored(monkeypatch):
    monkeypatch.setattr(summ, "_ollama", lambda *a: None)
    run = _run()
    out = summ.TestSummaryGenerator().generate_run_summary("r", _DB(run))
    assert "still running" in out["summary"]
    assert run.report_summary is None


def test_summary_written_mid_run_is_regenerated_after_it_ends(monkeypatch):
    monkeypatch.setattr(summ, "_ollama", lambda *a: None)
    t = datetime.utcnow()
    run = _run(status="failed", error_message="no waiter password",
               report_summary="Waiter demo finished with status 'running'.",
               report_generated_at=t - timedelta(minutes=1), completed_at=t)
    out = summ.TestSummaryGenerator().generate_run_summary("r", _DB(run))
    assert "failed" in out["summary"] and "running" not in out["summary"]
