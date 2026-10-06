"""Phase 4F.4 — the scenario execution service and the boundary around it.

Execution moved out of automation/api/v1/routers/scenario.py into
automation/scenarios/service.py so the agent runs it without FastAPI and without
a database. The router is now an HTTP adapter; the database half lives in
automation/scenarios/run_records.py and is injected, not imported.

    agent  ──────────────┐
                         ├──► scenarios/service.py  ◄── run_records.py (backend only)
    routers/scenario.py ─┘         (no fastapi, no sqlalchemy, no database)

These tests check the real import graph and the real execution path, not
docstrings — every source assertion below parses the AST with docstrings and
comments discarded, because prose about a boundary is not the boundary.
"""
import ast
import inspect
import subprocess
import sys

import pytest

from automation.agent import main as agent_main
from automation.api.v1.routers import scenario as router
from automation.scenarios import run_records
from automation.scenarios import service


def _identifiers(mod):
    """Every name the module's CODE mentions — docstrings and comments dropped."""
    tree = ast.parse(inspect.getsource(mod))
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
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(a.name for a in node.names)
    return names


def _import_graph(module: str) -> set:
    """Modules actually loaded when `module` is imported, in a fresh interpreter."""
    out = subprocess.run(
        [sys.executable, "-c", f"import sys, {module}; print(chr(10).join(sys.modules))"],
        capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-2000:]
    return set(out.stdout.split())


# ── A/B: nobody on the execution path reaches the router ───────────────────

def test_a_the_agent_does_not_import_the_scenario_router():
    assert "automation.api.v1.routers.scenario" not in _identifiers(agent_main)
    assert "automation.api.v1.routers" not in _identifiers(agent_main)


def test_b_the_service_does_not_import_the_router():
    assert not [n for n in _identifiers(service) if n.startswith("automation.api")]


# ── C/D/E: the service's REAL import graph ─────────────────────────────────

def test_cde_the_service_import_graph_has_no_fastapi_sqlalchemy_or_database():
    g = _import_graph("automation.scenarios.service")
    offenders = sorted(m for m in g
                       if m.split(".")[0] in ("fastapi", "sqlalchemy")
                       or m.startswith("automation.database")
                       or m.startswith("automation.api"))
    assert not offenders, f"the execution service pulls in {offenders}"


def test_e_importing_the_service_does_not_initialise_a_session_factory():
    """The named hazard: SessionLocal arriving through a helper import."""
    assert "SessionLocal" not in _identifiers(service)
    assert "automation.database.config" not in _import_graph("automation.scenarios.service")


# ── F: the service names no database model ─────────────────────────────────

def _import_sources(mod):
    """(module, name) for every import in the module, including function-local ones."""
    out = []
    for node in ast.walk(ast.parse(inspect.getsource(mod))):
        if isinstance(node, ast.ImportFrom):
            out += [(node.module or "", a.name) for a in node.names]
        elif isinstance(node, ast.Import):
            out += [(a.name, a.name) for a in node.names]
    return out


def test_f_the_service_touches_no_orm_model():
    """By provenance, not by name.

    `ScenarioResult` is ambiguous: the ORM row and the scenario runner's own
    result dataclass share the name, and the service legitimately uses the
    latter. Checking the bare name would either pass vacuously or ban the wrong
    thing, so this checks where every imported name comes from.
    """
    for mod, name in _import_sources(service):
        assert not mod.startswith("automation.database"), \
            f"the execution service imports {name} from {mod}"
        assert not mod.startswith("sqlalchemy"), f"the service imports {name} from {mod}"

    names = _identifiers(service)
    for model in ("TestRun", "TestProject", "SessionLocal", "get_db", "Session"):
        assert model not in names, f"the execution service still references {model}"

    # the one it does use is the runner's dataclass, not the row
    assert ("automation.intelligence.scenario_runner", "ScenarioResult") in _import_sources(service)


# ── G/H: execution stayed with execution ───────────────────────────────────

def test_g_simulator_appium_and_wda_work_lives_in_the_service():
    src = _identifiers(service)
    for name in ("ensure_ios_booted", "ensure_metro", "ensure_appium", "Remote"):
        assert name in src, f"{name} did not move with execution"
    # ...and not left behind in the router's own run path.
    assert "scenario_events" in _identifiers(router), "the router must delegate"


def test_h_appium_url_stays_injectable():
    assert "appium_url" in service.ScenarioRequest.model_fields
    body = _identifiers(service.scenario_events)
    assert "appium_url" in body


# ── the backend half is wired, and only the backend half ───────────────────

def test_importing_the_router_registers_backend_run_bookkeeping():
    assert service._run_store is run_records


def test_the_backend_store_is_the_only_database_aware_half():
    names = _identifiers(run_records)
    assert "SessionLocal" in names and "TestProject" in names
    g = _import_graph("automation.scenarios.run_records")
    assert not [m for m in g if m.split(".")[0] == "fastapi"], \
        "the run store must not need FastAPI either"


def test_execution_is_not_duplicated_between_router_and_service():
    """The router keeps the batch endpoint's own generator and nothing else."""
    router_defs = {n.name for n in ast.parse(inspect.getsource(router)).body
                   if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    service_defs = {n.name for n in ast.parse(inspect.getsource(service)).body
                    if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert router_defs & service_defs == set(), \
        f"defined in both: {sorted(router_defs & service_defs)}"
    assert "scenario_events" not in router_defs and "run_scenario_headless" not in router_defs


# ── a real run through the extracted service ───────────────────────────────

class _Step:
    def __init__(self, step, ok=True, detail=""):
        self.step, self.ok, self.detail = step, ok, detail
        self.action, self.screenshot, self.healed, self.healed_note = "tap", None, False, ""


class _Runner:
    def __init__(self, driver, bundle_id, screenshot_dir=None):
        self.catalog = type("C", (), {"report": lambda s: {}, "render": lambda s: ""})()
        self.detail = ""

    def run_one(self, step, i):
        return _Step(step, ok=True, detail=self.detail)

    def handle_book_popup(self):
        return None

    def build_script(self, out, test_name=None):
        return "# script"


@pytest.fixture
def executable(monkeypatch, tmp_path):
    """Everything below the service stubbed — the service itself runs for real."""
    from automation.projects.builder import app_builder
    import automation.intelligence.scenario_runner as sr
    import appium

    monkeypatch.setattr(app_builder, "ensure_ios_booted", lambda udid: (True, "booted"))
    monkeypatch.setattr(app_builder, "is_installed", lambda d, b: True)
    monkeypatch.setattr(app_builder, "ensure_metro", lambda p, udid=None, bundle_id=None: (True, "metro"))
    monkeypatch.setattr(app_builder, "ensure_appium", lambda url: (True, "appium"))
    monkeypatch.setattr(service, "_appium_options", lambda d, b: {"udid": d})
    monkeypatch.setattr(sr, "ScenarioRunner", _Runner)

    seen = {}

    class _Driver:
        def update_settings(self, s): pass
        def terminate_app(self, b): pass
        def activate_app(self, b): pass
        def quit(self): pass

    monkeypatch.setattr(appium.webdriver, "Remote",
                        lambda url, options=None: seen.update(url=url) or _Driver())
    monkeypatch.setattr(service, "_run_store", None)      # agent process: nothing registered
    monkeypatch.setattr(service.time if hasattr(service, "time") else service, "__name__",
                        service.__name__, raising=False)
    return seen


def _req(**kw):
    base = dict(project_id="proj-1", steps=["tap Login"], device_id="UDID-X",
                name="Login", save=False, prepare=False, bundle_id="com.example.app")
    base.update(kw)
    return service.ScenarioRequest(**base)


def test_a_real_run_through_the_service_passes_with_no_error(executable, tmp_path):
    out = service.run_scenario_headless(_req(), run_id="job-abc")
    assert out["ok"] is True, out
    assert out["error"] is None, out["error"]
    assert out["passed"] == 1 and out["total"] == 1
    assert out["crash_detected"] is False


def test_the_appium_url_reaches_the_driver_unchanged(executable):
    service.run_scenario_headless(_req(appium_url="http://10.0.0.9:4799"), run_id="job-abc")
    assert executable["url"] == "http://10.0.0.9:4799"


def test_the_result_sink_still_receives_every_step(executable):
    got = []
    service.set_result_sink(lambda run_id, i, res, secs=None: got.append((run_id, i, res.step)))
    try:
        service.run_scenario_headless(_req(), run_id="job-abc")
    finally:
        service.set_result_sink(None)
    assert got == [("job-abc", 0, "tap Login")], got


def test_a_crash_is_detected_and_propagated(executable, monkeypatch):
    monkeypatch.setattr(_Runner, "run_one",
                        lambda self, step, i: _Step(step, ok=False, detail="APP BUG: crashed"))
    out = service.run_scenario_headless(_req(), run_id="job-abc")
    assert out["crash_detected"] is True
    assert out["ok"] is False


def test_a_missing_bundle_id_is_deterministic(executable):
    out = service.run_scenario_headless(
        service.ScenarioRequest(project_id="proj-1", steps=["tap"], device_id="UDID-X"))
    assert out["ok"] is False
    assert "bundle id" in out["error"].lower()
    assert out["total"] == 0


# ── known debt, documented and deliberately NOT fixed here ─────────────────

def test_broad_exception_handling_around_planned_scenarios_is_still_debt():
    """Broad `except Exception` around planned scenario execution remains a
    reliability debt because programming/automation failures can be converted
    into scenario FAIL — 4F.3 and 4F.5 each shipped one that way (an undefined
    name, then a stale stub signature), and both were invisible for the same
    reason. Phase 4F.4 is a packaging refactor and does not change it.

    Pinned so the debt is visible; delete this test when the policy changes.
    """
    fn = ast.parse(inspect.getsource(agent_main._run_planned_scenarios)).body[0]
    broad = [h for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers
             if isinstance(h.type, ast.Name) and h.type.id == "Exception"]
    assert broad, "the broad handler is gone — good; remove this note"
