"""Opt-in PostgreSQL integration coverage.

Skipped entirely unless PYTEST_PG_URL points at a throwaway PostgreSQL:

    PYTEST_PG_URL=postgresql+psycopg2://user:pass@localhost:5432/vya_test \
        .venv/bin/python -m pytest tests/test_postgres_integration.py

The normal suite never needs PostgreSQL. These cover the four things SQLite
genuinely cannot answer, per the 4G audit:

  a) the Alembic baseline builds the whole schema on a real PostgreSQL
  b) the job-claim CAS is atomic under real concurrent writers
  c) the device-reservation CAS is atomic under real concurrent writers
  d) StringList round-trips through a real JSON column

DESTRUCTIVE: creates and drops its own schema. Never point this at production.
"""
import os
import subprocess
import sys
import threading

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

PG_URL = os.getenv("PYTEST_PG_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="PYTEST_PG_URL is not set — PostgreSQL integration skipped")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _guard():
    """Refuse anything that looks like a real database."""
    low = (PG_URL or "").lower()
    for bad in ("vya-platform", "prod", "production"):
        assert bad not in low, f"PYTEST_PG_URL looks like a real database: {bad!r}"


@pytest.fixture(scope="module")
def pg():
    """A schema built by Alembic on the target PostgreSQL, dropped afterwards."""
    _guard()
    engine = create_engine(PG_URL, pool_pre_ping=True)
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))

    env = {**os.environ, "DATABASE_URL": PG_URL, "PYTHONPATH": REPO}
    res = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                         cwd=REPO, env=env, capture_output=True, text=True, timeout=600)
    assert res.returncode == 0, f"alembic upgrade failed:\n{res.stderr[-3000:]}"
    assert "Traceback" not in res.stderr, res.stderr[-3000:]
    yield engine
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    engine.dispose()


# ── (a) schema parity on real PostgreSQL ────────────────────────────────────

def test_a_alembic_builds_the_complete_schema(pg):
    from automation.database.config import Base
    from automation.database import models  # noqa: F401

    insp = inspect(pg)
    live = set(insp.get_table_names()) - {"alembic_version"}
    model = {t.name for t in Base.metadata.sorted_tables}
    assert live == model, f"missing={sorted(model-live)} extra={sorted(live-model)}"

    missing = []
    for t in Base.metadata.sorted_tables:
        got = {c["name"] for c in insp.get_columns(t.name)}
        missing += [f"{t.name}.{c.name}" for c in t.columns if c.name not in got]
    assert not missing, missing

    devices = {tuple(sorted(u["column_names"])) for u in insp.get_unique_constraints("devices")}
    assert ("machine_id", "udid") in devices

    # Indexes too. Phase 4G.5A added six that production had been missing since
    # its tables were built by create_all(); a fresh PostgreSQL must have them all.
    missing_ix = []
    for t in Base.metadata.sorted_tables:
        got = {i["name"] for i in insp.get_indexes(t.name)}
        missing_ix += [f"{t.name}.{ix.name}" for ix in t.indexes if ix.name not in got]
    assert not missing_ix, f"indexes missing on PostgreSQL: {missing_ix}"


def test_a2_the_engine_settings_apply_to_a_real_postgres_url():
    """The audited numbers, on the dialect they were written for."""
    _guard()
    e = create_engine(PG_URL, pool_pre_ping=True, pool_recycle=1800,
                      pool_size=5, max_overflow=10, pool_timeout=30,
                      connect_args={"connect_timeout": 10})
    assert e.pool.size() == 5
    assert e.pool._max_overflow == 10
    assert e.pool._recycle == 1800
    assert e.pool._pre_ping is True
    e.dispose()


# ── (b) job-claim CAS under genuine concurrency ─────────────────────────────

def _seed_run(Session, run_id, agents=()):
    """Seed the run AND the agents that will race for it.

    The agents matter: test_runs.agent_id is a real foreign key to
    execution_agents.id. SQLite ships with foreign_keys=OFF, so inventing agent
    ids there is silently accepted; PostgreSQL enforces the constraint and
    rejects every claim. Seeding real rows tests the CAS rather than the FK.
    """
    from automation.database.models import ExecutionAgent, TestProject, TestRun
    s = Session()
    s.merge(TestProject(id="proj-pg", name="D", git_url="https://e/x.git"))
    for a in agents:
        # `os` is NOT NULL — PostgreSQL enforces that, SQLite does not.
        s.merge(ExecutionAgent(id=a, hostname=f"host-{a}", os="Darwin", status="online"))
    s.merge(TestRun(id=run_id, project_id="proj-pg", test_suite="s", test_name="t",
                    status="queued", job_state="queued", device_name="UDID-X"))
    s.commit(); s.close()


def test_b_only_one_writer_can_claim_a_queued_job(pg):
    """The conditional UPDATE, run by 8 real connections at once."""
    from automation.database.models import TestRun
    Session = sessionmaker(bind=pg)
    agent_ids = [f"a{i}" for i in range(8)]
    _seed_run(Session, "run-claim", agents=agent_ids)

    wins, errors, barrier = [], [], threading.Barrier(8)

    def claim(agent):
        s = Session()
        try:
            barrier.wait(timeout=30)
            n = (s.query(TestRun)
                 .filter(TestRun.id == "run-claim", TestRun.job_state == "queued")
                 .update({"job_state": "assigned", "agent_id": agent},
                         synchronize_session=False))
            s.commit()
            if n:
                wins.append(agent)
        except Exception as exc:           # never let a thread fail silently
            errors.append(f"{agent}: {type(exc).__name__}: {exc}")
            s.rollback()
        finally:
            s.close()

    threads = [threading.Thread(target=claim, args=(a,)) for a in agent_ids]
    for t in threads: t.start()
    for t in threads: t.join(timeout=60)

    assert not errors, f"writers raised: {errors[:3]}"
    assert len(wins) == 1, f"{len(wins)} agents claimed the same job: {wins}"
    s = Session()
    assert s.query(TestRun).filter_by(id="run-claim").one().agent_id == wins[0]
    s.close()


# ── (c) device-reservation CAS under genuine concurrency ────────────────────

def test_c_only_one_run_can_reserve_a_device(pg):
    from automation.database.models import DeviceRecord
    Session = sessionmaker(bind=pg)
    s = Session()
    s.merge(DeviceRecord(id="dev-pg", machine_id="m1", udid="UDID-PG", reserved_by=None))
    s.commit(); s.close()

    wins, barrier = [], threading.Barrier(8)

    def reserve(run_id):
        s = Session()
        try:
            barrier.wait(timeout=30)
            n = (s.query(DeviceRecord)
                 .filter(DeviceRecord.id == "dev-pg")
                 .filter((DeviceRecord.reserved_by.is_(None))
                         | (DeviceRecord.reserved_by == run_id))
                 .update({"reserved_by": run_id, "reserved_state": "busy"},
                         synchronize_session=False))
            s.commit()
            if n:
                wins.append(run_id)
        except Exception:
            s.rollback()
        finally:
            s.close()

    threads = [threading.Thread(target=reserve, args=(f"r{i}",)) for i in range(8)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=60)

    assert len(wins) == 1, f"{len(wins)} runs reserved one device: {wins}"


# ── (d) StringList through a real JSON column ───────────────────────────────

def test_d_stringlist_round_trips_on_postgres(pg):
    from automation.database.models import SavedScenario, TestProject
    Session = sessionmaker(bind=pg)
    s = Session()
    s.merge(TestProject(id="proj-pg2", name="D", git_url="https://e/y.git"))
    s.commit()

    s.merge(SavedScenario(id="sc-ok", name="ok", project_id="proj-pg2",
                          steps=["open app", "tap Login"], covers=["Home"]))
    s.commit(); s.expire_all()
    row = s.query(SavedScenario).filter_by(id="sc-ok").one()
    assert row.steps == ["open app", "tap Login"] and row.covers == ["Home"]

    # a double-encoded value, exactly as the old seed wrote it
    import json
    s.execute(text("update saved_scenarios set steps = CAST(:v AS json) where id = 'sc-ok'"),
              {"v": json.dumps(json.dumps(["a", "b"]))})
    s.commit(); s.expire_all()
    healed = s.query(SavedScenario).filter_by(id="sc-ok").one()
    assert healed.steps == ["a", "b"], "StringList did not heal a corrupt row on PostgreSQL"
    assert isinstance(healed.steps, list)
    s.close()


def test_d2_a_null_json_column_reads_as_a_list(pg):
    from automation.database.models import SavedScenario, TestProject
    Session = sessionmaker(bind=pg)
    s = Session()
    s.merge(TestProject(id="proj-pg3", name="D", git_url="https://e/z.git"))
    s.merge(SavedScenario(id="sc-null", name="n", project_id="proj-pg3"))
    s.commit()
    s.execute(text("update saved_scenarios set steps = NULL, covers = NULL where id = 'sc-null'"))
    s.commit(); s.expire_all()
    row = s.query(SavedScenario).filter_by(id="sc-null").one()
    assert row.steps == [] and row.covers == []
    s.close()
"""Appended to tests/test_postgres_integration.py — Steps 3 and 5."""


# ── Step 5: the 4G.2 repair, against real PostgreSQL ────────────────────────

def ids_of_this_fixture():
    return {"canon", "dbl-steps", "dbl-covers", "null-covers", "malformed"}


def _repair_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "repair_rev", "alembic/versions/9c1f4b2ad70e_repair_saved_scenario_json.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def corrupted(pg):
    """One row of every shape the repair must distinguish."""
    import json
    from automation.database.models import SavedScenario, TestProject
    Session = sessionmaker(bind=pg)
    s = Session()
    s.merge(TestProject(id="proj-rep", name="D", git_url="https://e/r.git"))
    s.commit()
    # Insert by raw SQL. Going through the ORM would route these values through
    # StringList, which heals them on write — the fixture would then contain no
    # corruption at all and the test would pass vacuously.
    ids = ("canon", "dbl-steps", "dbl-covers", "null-covers", "malformed")
    s.execute(text("delete from saved_scenarios where id = ANY(:ids)"), {"ids": list(ids)})
    for sid in ids:
        s.execute(text("insert into saved_scenarios (id, name, project_id) "
                       "values (:i, :n, 'proj-rep')"), {"i": sid, "n": sid})
    s.commit()

    def raw(sid, col, value):
        """Write a literal JSON document, exactly as the old seed would have."""
        s.execute(text(f"update saved_scenarios set {col} = CAST(:v AS json) where id = :i"),
                  {"v": value, "i": sid})

    raw("canon", "steps", json.dumps(["open app", "tap Login"]))
    raw("canon", "covers", json.dumps(["Home"]))
    raw("dbl-steps", "steps", json.dumps(json.dumps(["a", "b"])))
    raw("dbl-covers", "covers", json.dumps(json.dumps([])))
    s.execute(text("update saved_scenarios set covers = NULL where id = 'null-covers'"))
    raw("malformed", "steps", json.dumps("just a sentence"))
    s.commit()

    # Prove the fixture actually planted corruption. Without this the test can
    # pass vacuously if something upstream healed the rows.
    stored = {sid: (st, cv) for sid, st, cv in s.execute(text(
        "select id, steps, covers from saved_scenarios where id = ANY(:ids)"),
        {"ids": list(ids)})}
    assert isinstance(stored["dbl-steps"][0], str), "fixture did not plant corrupt steps"
    assert isinstance(stored["dbl-covers"][1], str), "fixture did not plant corrupt covers"
    assert isinstance(stored["canon"][0], list), "canonical row was not stored as a list"
    assert stored["null-covers"][1] is None
    s.close()
    return Session


def test_e_the_repair_targets_exactly_the_corrupt_values(corrupted, pg):
    rev = _repair_module()
    with pg.connect() as c:
        plan = rev._repairs(c)
    mine = {p[0] for p in plan} & {"canon", "dbl-steps", "dbl-covers",
                                   "null-covers", "malformed"}
    assert sorted(mine) == ["dbl-covers", "dbl-steps"], plan
    # Scoped to this fixture's rows: other tests deliberately leave corrupt rows
    # behind (test_d plants one), and the repair correctly finds those too.
    mine_pairs = {(p[0], p[1]) for p in plan if p[0] in ids_of_this_fixture()}
    assert mine_pairs == {("dbl-steps", "steps"), ("dbl-covers", "covers")}


def test_e2_the_repair_runs_and_is_idempotent_on_postgres(corrupted, pg):
    import json
    from automation.database.models import SavedScenario
    rev = _repair_module()

    before = {}
    with pg.connect() as c:
        for sid, st, cv in c.execute(text("select id, steps, covers from saved_scenarios")):
            before[sid] = (json.dumps(st), json.dumps(cv))

    with pg.begin() as c:
        for row_id, field, _old, new in rev._repairs(c):
            c.execute(text(f"update saved_scenarios set {field} = CAST(:v AS json) where id = :i"),
                      {"v": new, "i": row_id})

    with pg.connect() as c:
        assert rev._repairs(c) == [], "second pass wanted more changes"
        after = {sid: (json.dumps(st), json.dumps(cv))
                 for sid, st, cv in c.execute(text("select id, steps, covers from saved_scenarios"))}

    changed = {k for k in before if before[k] != after[k]}
    mine = changed & {"canon", "dbl-steps", "dbl-covers", "null-covers", "malformed"}
    assert mine == {"dbl-steps", "dbl-covers"}, f"changed {changed}"

    Session = sessionmaker(bind=pg)
    s = Session()
    rows = {r.id: r for r in s.query(SavedScenario).all()}
    assert rows["dbl-steps"].steps == ["a", "b"]
    assert rows["dbl-covers"].covers == []
    assert rows["canon"].steps == ["open app", "tap Login"], "canonical row was altered"
    assert rows["null-covers"].covers == [], "NULL reads as [] via StringList"
    assert rows["malformed"].steps == [], "malformed degrades to []"
    s.close()

    with pg.connect() as c:
        raw_null = c.execute(text("select covers from saved_scenarios where id='null-covers'")).scalar()
    assert raw_null is None, "NULL covers must stay NULL in the column"


def test_e3_stringlist_agrees_across_sqlite_and_postgres(corrupted, pg):
    """The same stored bytes must read identically on both engines."""
    import json, tempfile
    from automation.database.models import Base, SavedScenario, StringList, TestProject
    from sqlalchemy import create_engine as ce

    d = tempfile.mkdtemp()
    lite = ce(f"sqlite:///{d}/parity.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(lite)
    LS = sessionmaker(bind=lite); ls = LS()
    ls.add(TestProject(id="proj-rep", name="D", git_url="https://e/r.git")); ls.commit()

    with pg.connect() as c:
        pg_rows = list(c.execute(text("select id, steps, covers from saved_scenarios order by id")))

    for sid, st, cv in pg_rows:
        ls.add(SavedScenario(id=sid, name=sid, project_id="proj-rep"))
        ls.commit()
        ls.execute(text("update saved_scenarios set steps=:s, covers=:c where id=:i"),
                   {"s": None if st is None else json.dumps(st),
                    "c": None if cv is None else json.dumps(cv), "i": sid})
        ls.commit()
    ls.expire_all()

    PS = sessionmaker(bind=pg); ps = PS()
    for sid, _st, _cv in pg_rows:
        a = ls.query(SavedScenario).filter_by(id=sid).one()
        b = ps.query(SavedScenario).filter_by(id=sid).one()
        assert a.steps == b.steps, f"{sid}: sqlite {a.steps!r} != postgres {b.steps!r}"
        assert a.covers == b.covers, f"{sid}: sqlite {a.covers!r} != postgres {b.covers!r}"
    ls.close(); ps.close()


# ── Step 3: targeted compatibility gaps ─────────────────────────────────────

def test_f_boolean_datetime_and_nullable_round_trip(pg):
    from datetime import datetime
    from automation.database.models import TestProject, TestRun
    Session = sessionmaker(bind=pg)
    s = Session()
    s.merge(TestProject(id="proj-t", name="D", git_url="https://e/t.git"))
    now = datetime.utcnow()
    s.merge(TestRun(id="run-types", project_id="proj-t", test_suite="s", test_name="t",
                    status="passed", job_state="passed", device_name="U",
                    is_flaky=True, crash_detected=False,
                    started_at=now, duration_ms=1234, error_message=None))
    s.commit(); s.expire_all()
    r = s.query(TestRun).filter_by(id="run-types").one()
    assert r.is_flaky is True and r.crash_detected is False
    assert isinstance(r.started_at, datetime)
    assert abs((r.started_at - now).total_seconds()) < 1
    assert r.started_at.tzinfo is None, "naive in, naive out"
    assert r.duration_ms == 1234 and r.error_message is None

    # Every Boolean column carries default=False, so none is ever NULL on insert.
    # Force one NULL to prove PostgreSQL reads it back as None, not False.
    s.execute(text("update test_runs set visual_warning = NULL where id = 'run-types'"))
    s.commit(); s.expire_all()
    assert s.query(TestRun).filter_by(id="run-types").one().visual_warning is None, \
        "a NULL boolean must read back as None, not False"
    s.close()


def test_g_unique_constraint_is_enforced(pg):
    from sqlalchemy.exc import IntegrityError
    from automation.database.models import DeviceRecord
    Session = sessionmaker(bind=pg)
    s = Session()
    s.merge(DeviceRecord(id="d1", machine_id="m-uniq", udid="U-UNIQ")); s.commit()
    s2 = Session()
    s2.add(DeviceRecord(id="d2", machine_id="m-uniq", udid="U-UNIQ"))
    with pytest.raises(IntegrityError):
        s2.commit()
    s2.rollback(); s.close(); s2.close()


def test_h_foreign_key_is_enforced_unlike_sqlite(pg):
    """The difference this phase found: SQLite ships with foreign_keys=OFF.

    test_runs.agent_id references execution_agents.id. SQLite accepts a dangling
    value silently; PostgreSQL rejects it. Any code that relied on SQLite's
    leniency would break on migration.
    """
    from sqlalchemy.exc import IntegrityError

    with pg.begin() as c:
        c.execute(text("insert into test_projects (id,name,git_url) "
                       "values ('proj-fk','D','https://e/f.git') on conflict do nothing"))
        c.execute(text("insert into test_runs (id,project_id,test_suite,test_name,"
                       "status,job_state,device_name) values "
                       "('run-fk','proj-fk','s','t','queued','queued','U') "
                       "on conflict do nothing"))

    # Its own connection: the violation aborts the transaction, and reusing a
    # shared one would fail every later statement including teardown.
    raised = False
    conn = pg.connect()
    try:
        trans = conn.begin()
        conn.execute(text("update test_runs set agent_id='ghost-agent' where id='run-fk'"))
        trans.commit()
    except IntegrityError:
        raised = True
        trans.rollback()
    finally:
        conn.close()
    assert raised, "PostgreSQL did not enforce the agent_id foreign key"


def test_i_transaction_rollback_discards_the_write(pg):
    from automation.database.models import TestProject
    Session = sessionmaker(bind=pg)
    s = Session()
    s.add(TestProject(id="proj-rb", name="rollback", git_url="https://e/rb.git"))
    s.flush()
    assert s.query(TestProject).filter_by(id="proj-rb").first() is not None
    s.rollback()
    assert s.query(TestProject).filter_by(id="proj-rb").first() is None
    s.close()


def test_j_delete_then_insert_replacement_is_atomic(pg):
    """The pattern performance/scenario persistence relies on."""
    import uuid
    from automation.database.models import PerformanceMetric, TestProject, TestRun
    Session = sessionmaker(bind=pg)
    s = Session()
    s.merge(TestProject(id="proj-perf", name="D", git_url="https://e/p.git"))
    s.merge(TestRun(id="run-perf", project_id="proj-perf", test_suite="s", test_name="t",
                    status="passed", job_state="passed", device_name="U"))
    s.commit()

    def report(n):
        s.query(PerformanceMetric).filter(PerformanceMetric.run_id == "run-perf").delete(
            synchronize_session=False)
        for i in range(n):
            s.add(PerformanceMetric(id=str(uuid.uuid4()), run_id="run-perf",
                                    cpu_percent=float(i), network_requests=i))
        s.commit()

    report(5)
    assert s.query(PerformanceMetric).filter_by(run_id="run-perf").count() == 5
    report(5)
    assert s.query(PerformanceMetric).filter_by(run_id="run-perf").count() == 5, \
        "a resubmitted report duplicated the series"
    report(2)
    assert s.query(PerformanceMetric).filter_by(run_id="run-perf").count() == 2
    s.close()
