"""Phase 4G.3 — engine settings for PostgreSQL, and no session across an Appium run.

Two independent problems the 4G.3 audit measured:

1. The engine took SQLAlchemy's defaults on every dialect — pool_recycle=-1 and
   pool_pre_ping=False. On a networked database that hands the application a
   connection that died in the pool.

2. Three callers held an open session for the whole of a scenario execution.
   Production runs reach 28.3 minutes (102 measured runs, 16 over five minutes),
   so on PostgreSQL each was one idle-in-transaction connection for that long —
   blocking VACUUM, holding locks, and liable to be killed by
   idle_in_transaction_session_timeout. SQLite hid all of it.
"""
import ast
import inspect

import pytest
from sqlalchemy import create_engine

import automation.database.config as cfg
from automation.intelligence import pr_autotest
from automation.api.v1.routers import tickets, workflow

PG_KWARGS = ("pool_pre_ping", "pool_recycle", "pool_size", "max_overflow",
             "pool_timeout", "connect_timeout")


def _engine_branches():
    """The two create_engine() calls, split by which branch they sit in."""
    src = inspect.getsource(cfg)
    tree = ast.parse(src)
    branch = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = ast.unparse(node.test)
        if "sqlite" not in test:
            continue
        for label, body in (("sqlite", node.body), ("other", node.orelse)):
            for n in ast.walk(ast.Module(body=body, type_ignores=[])):
                if (isinstance(n, ast.Call) and getattr(n.func, "id", "") == "create_engine"):
                    branch[label] = ast.unparse(n)
    return branch


# ── the settings land only on the non-SQLite branch ─────────────────────────

def test_01_postgres_branch_carries_every_audited_setting():
    other = _engine_branches().get("other", "")
    assert other, "no non-SQLite create_engine() found"
    for kw in PG_KWARGS:
        assert kw in other, f"{kw} missing from the non-SQLite engine"
    assert "pool_pre_ping=True" in other.replace(" ", "")
    assert "pool_recycle=1800" in other.replace(" ", "")
    assert "pool_size=5" in other.replace(" ", "")
    assert "max_overflow=10" in other.replace(" ", "")
    assert "pool_timeout=30" in other.replace(" ", "")


def test_02_sqlite_branch_receives_none_of_them():
    sqlite = _engine_branches().get("sqlite", "")
    assert sqlite, "no SQLite create_engine() found"
    for kw in PG_KWARGS:
        assert kw not in sqlite, f"SQLite engine was given {kw}"
    assert "check_same_thread" in sqlite, "SQLite lost check_same_thread"


def test_03_in_memory_sqlite_would_reject_the_pool_kwargs():
    """Why the split matters: SingletonThreadPool refuses pool_size/max_overflow."""
    create_engine("sqlite://", connect_args={"check_same_thread": False})  # fine
    with pytest.raises(TypeError):
        create_engine("sqlite://", pool_size=5, max_overflow=10)


def test_04_the_live_engine_is_sqlite_and_unpooled_for_postgres(monkeypatch):
    """The suite runs on SQLite; the engine must look exactly as it always did."""
    assert str(cfg.engine.url).startswith("sqlite")
    assert cfg.engine.pool.__class__.__name__ in ("QueuePool", "SingletonThreadPool")
    # no PostgreSQL-only connect arg leaked into the SQLite engine
    assert "connect_timeout" not in str(cfg.engine.dialect.create_connect_args(cfg.engine.url))


def test_05_alembic_still_uses_nullpool():
    src = open("alembic/env.py").read()
    assert "poolclass=pool.NullPool" in src, "the migration engine must stay NullPool"


# ── no session across the execution boundary ────────────────────────────────

def _calls_with_session(fn):
    """run_scenario_headless(...) calls that pass a second positional/db argument."""
    tree = ast.parse(inspect.getsource(fn))
    out = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call)
                and getattr(n.func, "id", "") == "run_scenario_headless"):
            passes_db = len(n.args) > 1 or any(k.arg == "db" for k in n.keywords)
            out.append(passes_db)
    return out


@pytest.mark.parametrize("fn,label", [
    (pr_autotest.run_pr_autotest, "pr_autotest"),
    (tickets.run_ticket_scenarios, "tickets"),
    (workflow.recreate, "workflow"),
])
def test_06_no_caller_passes_a_session_into_scenario_execution(fn, label):
    calls = _calls_with_session(fn)
    assert calls, f"{label}: no run_scenario_headless call found — test is stale"
    assert not any(calls), f"{label} still hands a session to the scenario runner"


@pytest.mark.parametrize("fn,label", [
    (pr_autotest.run_pr_autotest, "pr_autotest"),
    (tickets.run_ticket_scenarios, "tickets"),
    (workflow.recreate, "workflow"),
])
def test_07_no_session_context_wraps_the_execution_call(fn, label):
    """A `with SessionLocal()` block must not contain the scenario run."""
    tree = ast.parse(inspect.getsource(fn))
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        opens = any(
            isinstance(n, ast.Call) and getattr(n.func, "id", "") == "SessionLocal"
            for item in node.items for n in ast.walk(item.context_expr))
        if not opens:
            continue
        runs = [n for n in ast.walk(node)
                if isinstance(n, ast.Call)
                and getattr(n.func, "id", "") == "run_scenario_headless"]
        assert not runs, (
            f"{label}: a session is still open around scenario execution "
            f"(line {node.lineno})")


def test_08_the_bundle_id_is_resolved_before_execution():
    """Behaviour preservation: dropping the session must not drop bundle resolution."""
    assert "project_bundle_id" in inspect.getsource(pr_autotest.run_pr_autotest)
    assert "default_bundle" in inspect.getsource(tickets.run_ticket_scenarios)
    assert "fallback_bundles" in inspect.getsource(workflow.recreate)


def test_09_resolve_run_still_accepts_no_session():
    """The mechanism the fix relies on (Phase 4F.5)."""
    from automation.scenarios.service import resolve_run, ScenarioError
    req_ok = _req(bundle_id="com.example.app", steps=["tap"])
    bundle, steps, _repo = resolve_run(req_ok)          # no db at all
    assert bundle == "com.example.app" and steps == ["tap"]
    with pytest.raises(ScenarioError):
        resolve_run(_req(bundle_id=None, steps=["tap"]))


def _req(**over):
    from automation.scenarios.service import ScenarioRequest
    base = dict(project_id="proj-1", device_id="UDID-X", name="n",
                save=False, prepare=False, steps=["tap"])
    base.update(over)
    return ScenarioRequest(**base)


# ── sessions still close, on success and on failure ─────────────────────────

def test_10_get_db_closes_on_success_and_on_exception(monkeypatch):
    closed = []

    class _S:
        def close(self): closed.append(True)

    monkeypatch.setattr(cfg, "SessionLocal", lambda: _S())

    gen = cfg.get_db(); next(gen)
    with pytest.raises(StopIteration):
        next(gen)
    assert closed == [True], "get_db did not close on the happy path"

    closed.clear()
    gen = cfg.get_db(); next(gen)
    with pytest.raises(ValueError):
        gen.throw(ValueError("boom"))
    assert closed == [True], "get_db did not close when the caller raised"


def test_11_every_bare_session_in_the_touched_files_is_closed():
    """No `db = SessionLocal()` without a finally in the three edited modules."""
    import automation.api.v1.routers.tickets as t
    import automation.api.v1.routers.workflow as w
    import automation.intelligence.pr_autotest as p
    for mod in (t, w, p):
        src = inspect.getsource(mod)
        bare = src.count("= SessionLocal()")
        managed = src.count("with SessionLocal()")
        assert bare == 0, (
            f"{mod.__name__} has {bare} unmanaged SessionLocal() — "
            f"{managed} are context-managed")
