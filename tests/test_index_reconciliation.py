"""Phase 4G.5A — the six declared-but-missing indexes, and target hardening.

Production's tables came from create_all() + _add_missing_columns(), which never
creates an index. Any index declared on a model after its table already existed
was therefore never built. This revision adds exactly those six.

It also covers the hazard that nearly aimed a production migration at the wrong
file: with DATABASE_URL unset, alembic.ini named a stale repo-root database as a
fallback, so migrations silently targeted it. Alembic now refuses to guess.
"""
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect, text

from automation.database.config import Base
from automation.database import models  # noqa: F401

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEW_HEAD = "b3e21c07f4d9"
PRIOR_HEAD = "9c1f4b2ad70e"

EXPECTED = {
    "ix_devices_reserved_by": ("devices", ["reserved_by"]),
    "ix_execution_agents_agent_credential_hash": ("execution_agents", ["agent_credential_hash"]),
    "ix_test_runs_id": ("test_runs", ["id"]),
    "ix_test_runs_machine_id": ("test_runs", ["machine_id"]),
    "ix_test_runs_test_name": ("test_runs", ["test_name"]),
    "ix_test_runs_test_suite": ("test_runs", ["test_suite"]),
}


def _alembic(args, db_path, env_extra=None, expect_ok=True):
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}", "PYTHONPATH": REPO}
    if env_extra is not None:
        env.update(env_extra)
    res = subprocess.run([sys.executable, "-m", "alembic", *args],
                         cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
    if expect_ok:
        assert res.returncode == 0, f"{args} failed:\n{res.stderr[-2500:]}"
    return res


def _indexes(db_path, table):
    return {i["name"] for i in inspect(create_engine(f"sqlite:///{db_path}")).get_indexes(table)}


def _all_indexes(db_path):
    eng = create_engine(f"sqlite:///{db_path}")
    insp = inspect(eng)
    return {(t, i["name"]) for t in insp.get_table_names() for i in insp.get_indexes(t)}


# ── fresh database ──────────────────────────────────────────────────────────

def test_01_a_fresh_upgrade_creates_all_six(tmp_path):
    db = tmp_path / "fresh.db"
    _alembic(["upgrade", "head"], db)
    for name, (table, cols) in EXPECTED.items():
        got = {i["name"]: i["column_names"]
               for i in inspect(create_engine(f"sqlite:///{db}")).get_indexes(table)}
        assert name in got, f"{name} missing after upgrade head"
        assert list(got[name]) == cols, f"{name} on {got[name]}, expected {cols}"


def test_02_upgrading_to_head_passes_through_this_revision(tmp_path):
    """This revision need not stay the tip — later phases add more — but a
    database at head must have applied it."""
    db = tmp_path / "head.db"
    _alembic(["upgrade", "head"], db)
    history = _alembic(["history"], db).stdout
    assert NEW_HEAD in history, history
    # and the indexes it creates are present at head
    for name, (table, _cols) in EXPECTED.items():
        assert name in _indexes(db, table), f"{name} absent at head"


def test_03_the_fresh_schema_still_matches_the_models(tmp_path):
    """Indexes were the only gap; nothing else may drift."""
    db = tmp_path / "parity.db"
    _alembic(["upgrade", "head"], db)
    insp = inspect(create_engine(f"sqlite:///{db}"))
    missing = []
    for t in Base.metadata.sorted_tables:
        got = {i["name"] for i in insp.get_indexes(t.name)}
        missing += [f"{t.name}.{ix.name}" for ix in t.indexes if ix.name not in got]
    assert not missing, missing
    res = _alembic(["check"], db)
    assert "No new upgrade operations detected" in (res.stdout + res.stderr)


# ── an existing database that lacks them ────────────────────────────────────

@pytest.fixture
def legacy(tmp_path):
    """A database built the way production was: create_all(), no indexes, stamped."""
    db = tmp_path / "legacy.db"
    eng = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(eng)
    with eng.begin() as c:
        for name, (table, _cols) in EXPECTED.items():
            c.execute(text(f"DROP INDEX IF EXISTS {name}"))
        c.execute(text("INSERT INTO test_projects (id, name, git_url) "
                       "VALUES ('p1', 'D', 'https://e/x.git')"))
        for i in range(3):
            c.execute(text("INSERT INTO test_runs (id, project_id, test_suite, test_name, "
                           "status, job_state, device_name) VALUES "
                           f"('r{i}', 'p1', 's', 't{i}', 'passed', 'passed', 'U')"))
    _alembic(["stamp", PRIOR_HEAD], db)
    assert not (_indexes(db, "test_runs") & set(EXPECTED)), "fixture still has the indexes"
    return db


def test_04_an_existing_database_gains_all_six(legacy):
    before = _all_indexes(legacy)
    _alembic(["upgrade", "head"], legacy)
    after = _all_indexes(legacy)
    added = {n for _t, n in after - before}
    assert added == set(EXPECTED), f"added {added}"


def test_05_no_unrelated_index_changes(legacy):
    before = _all_indexes(legacy)
    _alembic(["upgrade", "head"], legacy)
    after = _all_indexes(legacy)
    assert not (before - after), f"indexes disappeared: {before - after}"
    assert {n for _t, n in after - before} == set(EXPECTED)


def test_06_no_rows_change(legacy):
    eng = create_engine(f"sqlite:///{legacy}")
    def snapshot():
        insp = inspect(eng)
        with eng.connect() as c:
            return {t: c.execute(text(f'select count(*) from "{t}"')).scalar()
                    for t in insp.get_table_names()}
    before = snapshot()
    with eng.connect() as c:
        runs_before = c.execute(text("select id, test_name, status from test_runs order by id")).fetchall()
    _alembic(["upgrade", "head"], legacy)
    after = snapshot()
    changed = {t for t in before if t != "alembic_version" and before[t] != after.get(t)}
    assert not changed, f"row counts changed: {changed}"
    with eng.connect() as c:
        assert c.execute(text("select id, test_name, status from test_runs order by id")).fetchall() == runs_before


def test_07_the_migration_is_idempotent(legacy):
    _alembic(["upgrade", "head"], legacy)
    first = _all_indexes(legacy)
    _alembic(["upgrade", "head"], legacy)          # no-op: already at head
    _alembic(["downgrade", PRIOR_HEAD], legacy)
    _alembic(["upgrade", "head"], legacy)          # round trip
    assert _all_indexes(legacy) == first, "index set differs after a down/up cycle"


def test_08_running_upgrade_twice_on_a_prepared_db_is_safe(legacy):
    """The guard inside upgrade(): an index that already exists is skipped."""
    eng = create_engine(f"sqlite:///{legacy}")
    with eng.begin() as c:
        c.execute(text("CREATE INDEX ix_test_runs_machine_id ON test_runs (machine_id)"))
    _alembic(["upgrade", "head"], legacy)          # must not raise "already exists"
    assert set(EXPECTED) <= {n for _t, n in _all_indexes(legacy)}


# ── downgrade ───────────────────────────────────────────────────────────────

def test_09_downgrade_removes_exactly_these_six(legacy):
    _alembic(["upgrade", "head"], legacy)
    at_head = _all_indexes(legacy)
    _alembic(["downgrade", PRIOR_HEAD], legacy)
    after = _all_indexes(legacy)
    removed = {n for _t, n in at_head - after}
    assert removed == set(EXPECTED), f"removed {removed}"
    assert not (after - at_head), "downgrade added something"


def test_10_downgrade_changes_no_rows(legacy):
    eng = create_engine(f"sqlite:///{legacy}")
    _alembic(["upgrade", "head"], legacy)
    with eng.connect() as c:
        before = c.execute(text("select id, test_name from test_runs order by id")).fetchall()
    _alembic(["downgrade", PRIOR_HEAD], legacy)
    with eng.connect() as c:
        assert c.execute(text("select id, test_name from test_runs order by id")).fetchall() == before


# ── engine portability ──────────────────────────────────────────────────────

def test_11_the_revision_uses_no_engine_specific_sql():
    src = open(f"{REPO}/alembic/versions/b3e21c07f4d9_add_missing_indexes.py").read()
    for bad in ("PRAGMA", "sqlite_master", "op.execute(", "AUTOINCREMENT"):
        assert bad not in src, f"revision contains engine-specific {bad}"
    assert "op.create_index" in src and "op.drop_index" in src


def test_12_postgres_integration_coverage_is_still_declared():
    """4G.4's PostgreSQL suite must still assert index parity."""
    src = open(f"{REPO}/tests/test_postgres_integration.py").read()
    assert "get_indexes" in src, "PostgreSQL parity no longer checks indexes"


# ── target hardening ────────────────────────────────────────────────────────

def test_13_alembic_refuses_to_run_without_database_url(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env["PYTHONPATH"] = REPO
    res = subprocess.run([sys.executable, "-m", "alembic", "current"],
                         cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert res.returncode != 0, "alembic ran without DATABASE_URL"
    assert "DATABASE_URL is not set" in res.stderr, res.stderr[-1500:]


def test_14_alembic_ini_offers_no_fallback_database():
    for line in open(f"{REPO}/alembic.ini"):
        if line.strip().startswith("sqlalchemy.url"):
            value = line.split("=", 1)[1].strip()
            assert value == "", f"alembic.ini still names a fallback: {value!r}"
            break
    else:
        pytest.fail("sqlalchemy.url not found in alembic.ini")


def test_15_the_stale_repo_database_is_not_reachable_by_default():
    """The exact hazard: an unset DATABASE_URL must never reach this file."""
    src = open(f"{REPO}/alembic.ini").read()
    assert "sqlalchemy.url = sqlite:///test_automation_new.db" not in src
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    env["PYTHONPATH"] = REPO
    res = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                         cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert res.returncode != 0
    assert "test_automation_new.db" not in res.stdout
