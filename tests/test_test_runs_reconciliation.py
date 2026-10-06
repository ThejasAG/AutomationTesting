"""Phase 4G.5B — reconciling test_runs with the models.

Closes the last drift from create_all() + _add_missing_columns(): three foreign
keys, three NOT NULL columns loosened, and timeline/error_message given the
models' String type. flaky_score is deliberately RETAINED.

timeline is the one that matters beyond tidiness. jobs.py does
json.loads(job.timeline) and json.dumps() on the way back; as a `json` column on
PostgreSQL psycopg2 decodes it first and json.loads(list) raises TypeError.
SQLite cannot show that difference, so only String is correct on both.
"""
import json
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect, text

from automation.database.config import Base
from automation.database import models  # noqa: F401

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REV = "c7a4f9e2b118"
PRIOR = "b3e21c07f4d9"

FKS = {
    ("test_runs", "project_id", "test_projects"),
    ("test_runs", "agent_id", "execution_agents"),
    ("test_projects", "group_id", "application_groups"),
}
LOOSENED = ("test_suite", "test_name", "status")


def _alembic(args, db, ok=True):
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db}", "PYTHONPATH": REPO}
    r = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=300)
    if ok:
        assert r.returncode == 0, f"{args} failed:\n{r.stderr[-2500:]}"
        assert "Traceback" not in r.stderr, r.stderr[-2500:]
    return r


@pytest.fixture
def drifted(tmp_path):
    """A database shaped like production was: create_all(), then the old
    test_runs definition forced back, then stamped at the prior revision."""
    db = tmp_path / "drift.db"
    eng = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(eng)
    with eng.begin() as c:
        c.execute(text("DROP TABLE test_runs"))
        c.execute(text("""
            CREATE TABLE test_runs (
                id VARCHAR(36) NOT NULL,
                test_suite VARCHAR(255) NOT NULL,
                test_name VARCHAR(255) NOT NULL,
                status VARCHAR(50) NOT NULL,
                started_at DATETIME, completed_at DATETIME, duration_ms INTEGER,
                device_name VARCHAR(255), os_version VARCHAR(50), platform VARCHAR(50),
                app_version VARCHAR(50), build_number VARCHAR(50), environment VARCHAR(50),
                error_message TEXT, created_at DATETIME, triggered_by VARCHAR(255),
                branch VARCHAR(255), commit_sha VARCHAR(255), timeline JSON,
                flaky_score FLOAT, project_id VARCHAR, is_flaky BOOLEAN DEFAULT 0,
                risk_score INTEGER, agent_id VARCHAR, job_state VARCHAR DEFAULT 'queued',
                bot_type VARCHAR(20) DEFAULT 'ios', report_summary TEXT,
                report_generated_at DATETIME, planned_scenarios JSON,
                attempts INTEGER DEFAULT 1, flaky_detected BOOLEAN DEFAULT False,
                visual_warning BOOLEAN DEFAULT False, crash_detected BOOLEAN DEFAULT False,
                machine_id VARCHAR(120),
                PRIMARY KEY (id))"""))
        for n, col in (("ix_test_runs_id", "id"), ("ix_test_runs_machine_id", "machine_id"),
                       ("ix_test_runs_test_name", "test_name"),
                       ("ix_test_runs_test_suite", "test_suite")):
            c.execute(text(f"CREATE INDEX {n} ON test_runs ({col})"))
        c.execute(text("INSERT INTO test_projects (id,name,git_url) VALUES ('p1','D','https://e/x.git')"))
        c.execute(text("INSERT INTO execution_agents (id,hostname,os) VALUES ('a1','h','Darwin')"))
        for i in range(4):
            c.execute(text(
                "INSERT INTO test_runs (id,project_id,agent_id,test_suite,test_name,status,"
                "job_state,device_name,timeline,error_message,flaky_score) VALUES "
                f"('r{i}','p1','a1','s','t{i}','passed','passed','U',"
                f"'[{{\"e\":\"x{i}\"}}]','boom {i}',NULL)"))
    _alembic(["stamp", PRIOR], db)
    return db


def _rows(db):
    eng = create_engine(f"sqlite:///{db}")
    with eng.connect() as c:
        cols = [r[1] for r in c.execute(text("pragma table_info(test_runs)"))]
        return cols, {r[0]: [None if v is None else str(v) for v in r]
                      for r in c.execute(text(f"select {','.join(cols)} from test_runs order by id"))}


# ── schema outcome ──────────────────────────────────────────────────────────

def test_01_all_three_foreign_keys_are_created(drifted):
    _alembic(["upgrade", "head"], drifted)
    insp = inspect(create_engine(f"sqlite:///{drifted}"))
    got = {(t, f["constrained_columns"][0], f["referred_table"])
           for t in ("test_runs", "test_projects") for f in insp.get_foreign_keys(t)}
    assert FKS <= got, f"missing: {FKS - got}"


def test_02_exactly_the_three_columns_become_nullable(drifted):
    _alembic(["upgrade", "head"], drifted)
    insp = inspect(create_engine(f"sqlite:///{drifted}"))
    cols = {c["name"]: c for c in insp.get_columns("test_runs")}
    for n in LOOSENED:
        assert cols[n]["nullable"] is True, f"{n} is still NOT NULL"
    assert cols["id"]["nullable"] is False, "the primary key must stay NOT NULL"
    not_null = {n for n, c in cols.items() if not c["nullable"]}
    assert not_null == {"id"}, f"unexpected NOT NULL columns: {not_null}"


def test_03_timeline_and_error_message_become_string(drifted):
    _alembic(["upgrade", "head"], drifted)
    cols = {c["name"]: c for c in inspect(create_engine(f"sqlite:///{drifted}")).get_columns("test_runs")}
    assert str(cols["timeline"]["type"]).upper().startswith("VARCHAR"), cols["timeline"]["type"]
    assert str(cols["error_message"]["type"]).upper().startswith("VARCHAR"), cols["error_message"]["type"]
    assert "JSON" not in str(cols["timeline"]["type"]).upper()


def test_04_flaky_score_is_retained(drifted):
    _alembic(["upgrade", "head"], drifted)
    cols = {c["name"] for c in inspect(create_engine(f"sqlite:///{drifted}")).get_columns("test_runs")}
    assert "flaky_score" in cols, "flaky_score was dropped — it must be retained"


def test_05_existing_indexes_survive(drifted):
    before = {i["name"] for i in inspect(create_engine(f"sqlite:///{drifted}")).get_indexes("test_runs")}
    _alembic(["upgrade", "head"], drifted)
    after = {i["name"] for i in inspect(create_engine(f"sqlite:///{drifted}")).get_indexes("test_runs")}
    assert before <= after, f"indexes lost in the rewrite: {before - after}"


# ── data preservation ───────────────────────────────────────────────────────

def test_06_every_row_and_value_survives_the_rewrite(drifted):
    cols_before, before = _rows(drifted)
    _alembic(["upgrade", "head"], drifted)
    cols_after, after = _rows(drifted)
    assert cols_after == cols_before, "column set or order changed"
    assert set(after) == set(before), "rows disappeared"
    assert after == before, "a value changed during the table rewrite"


def test_07_timeline_and_error_message_values_are_untouched(drifted):
    _, before = _rows(drifted)
    _alembic(["upgrade", "head"], drifted)
    cols, after = _rows(drifted)
    ti, ei = cols.index("timeline"), cols.index("error_message")
    for k in before:
        assert after[k][ti] == before[k][ti]
        assert after[k][ei] == before[k][ei]
        assert json.loads(after[k][ti]), "timeline is no longer parseable JSON text"


def test_08_no_other_table_changes(drifted):
    eng = create_engine(f"sqlite:///{drifted}")
    def counts():
        insp = inspect(eng)
        with eng.connect() as c:
            return {t: c.execute(text(f'select count(*) from "{t}"')).scalar()
                    for t in insp.get_table_names() if t != "alembic_version"}
    before = counts()
    _alembic(["upgrade", "head"], drifted)
    assert counts() == before


# ── foreign keys actually enforce ───────────────────────────────────────────

def test_09_foreign_keys_reject_dangling_references(drifted):
    import sqlite3
    _alembic(["upgrade", "head"], drifted)
    conn = sqlite3.connect(str(drifted))
    conn.execute("PRAGMA foreign_keys=ON")
    for sql in ("update test_runs set project_id='ghost' where id='r0'",
                "update test_runs set agent_id='ghost' where id='r0'",
                "update test_projects set group_id='ghost' where id='p1'"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("BEGIN"); conn.execute(sql)
        conn.execute("ROLLBACK")
    assert conn.execute("select count(*) from test_runs").fetchone()[0] == 4
    conn.close()


def test_10_no_orphans_exist_after_migration(drifted):
    _alembic(["upgrade", "head"], drifted)
    eng = create_engine(f"sqlite:///{drifted}")
    with eng.connect() as c:
        for t, col, ref in FKS:
            n = c.execute(text(
                f"select count(*) from {t} x left join {ref} r on x.{col}=r.id "
                f"where x.{col} is not null and r.id is null")).scalar()
            assert n == 0, f"{t}.{col} has {n} orphans"


# ── round trip + parity ─────────────────────────────────────────────────────

def test_11_downgrade_then_upgrade_preserves_every_row(drifted):
    _alembic(["upgrade", "head"], drifted)
    _, at_head = _rows(drifted)
    _alembic(["downgrade", PRIOR], drifted)
    _alembic(["upgrade", "head"], drifted)
    _, again = _rows(drifted)
    assert again == at_head, "a down/up cycle altered data"


def test_12_downgrade_removes_the_foreign_keys(drifted):
    _alembic(["upgrade", "head"], drifted)
    _alembic(["downgrade", PRIOR], drifted)
    insp = inspect(create_engine(f"sqlite:///{drifted}"))
    got = {(t, f["constrained_columns"][0], f["referred_table"])
           for t in ("test_runs", "test_projects") for f in insp.get_foreign_keys(t)}
    assert not (FKS & got), f"downgrade left {FKS & got}"


def test_13_a_fresh_database_reaches_head_with_no_drift(tmp_path):
    """The revision must be a no-op where the baseline already did the work."""
    db = tmp_path / "fresh.db"
    _alembic(["upgrade", "head"], db)
    res = _alembic(["check"], db, ok=False)
    out = res.stdout + res.stderr
    # flaky_score is deliberately retained on a DRIFTED db, but a fresh one
    # never had it, so a fresh build must be perfectly clean.
    assert "No new upgrade operations detected" in out, out[-1500:]


def test_14_the_migration_is_idempotent(drifted):
    _alembic(["upgrade", "head"], drifted)
    _, first = _rows(drifted)
    r = _alembic(["upgrade", "head"], drifted)
    assert "Running upgrade" not in r.stdout + r.stderr
    _, second = _rows(drifted)
    assert second == first


def test_15_the_revision_uses_no_engine_specific_sql():
    src = open(f"{REPO}/alembic/versions/c7a4f9e2b118_reconcile_test_runs.py").read()
    for bad in ("PRAGMA", "sqlite_master", "op.execute(", "AUTOINCREMENT"):
        assert bad not in src, f"revision contains {bad}"
    assert "batch_alter_table" in src, "SQLite needs batch mode for these operations"


def test_16_no_cascade_behaviour_was_invented():
    """The models declare no ondelete/onupdate; the migration must not add any."""
    import ast
    # Parse it: the docstring legitimately explains WHY no cascade is set, so a
    # substring check would fail on its own explanation.
    tree = ast.parse(open(f"{REPO}/alembic/versions/c7a4f9e2b118_reconcile_test_runs.py").read())
    kwargs = {k.arg for n in ast.walk(tree) if isinstance(n, ast.Call) for k in n.keywords}
    assert "ondelete" not in kwargs and "onupdate" not in kwargs, \
        f"the migration passes cascade behaviour the models do not declare: {kwargs}"
    from automation.database.models import TestProject, TestRun
    for m in (TestRun, TestProject):
        for fk in m.__table__.foreign_key_constraints:
            assert fk.ondelete is None and fk.onupdate is None


def test_17_timeline_reads_back_as_a_string(drifted):
    """jobs.py does json.loads(job.timeline) — it must not receive a list."""
    from sqlalchemy.orm import sessionmaker
    from automation.database.models import TestRun
    _alembic(["upgrade", "head"], drifted)
    s = sessionmaker(bind=create_engine(f"sqlite:///{drifted}"))()
    r = s.query(TestRun).filter_by(id="r0").one()
    assert isinstance(r.timeline, str), f"timeline is {type(r.timeline).__name__}"
    assert json.loads(r.timeline) == [{"e": "x0"}]
    s.close()
