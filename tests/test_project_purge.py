"""Purging a project removes its traces and KEEPS its test history.

Two rules this pins down, both learned from the live database:

1. History survives. A test run records that something was tested and what
   happened; that stays true once the project is gone, so runs are DETACHED
   (project_id -> NULL), never deleted.

2. A shared bundle id is never stolen. The locator store is keyed by bundle id,
   and this fleet runs the same app under two projects — 'org.vyapy.sarls.
   vyaconsumer' (prod) and '...vyaconsumerstaging'. The prefix match that exists to
   tolerate build-suffix drift would otherwise let the prod project drop the
   staging project's entry and blind a project nobody touched.
"""
import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import automation.projects.purge as purge_mod
from automation.projects.purge import (_locator_key_for, find_orphans, plan_purge,
                                       purge_project)

PROJ = "proj-under-test"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """An isolated root: its own DB, repos/, baselines/ and locator store."""
    (tmp_path / "repos" / PROJ).mkdir(parents=True)
    (tmp_path / "repos" / PROJ / "src.js").write_text("x")
    (tmp_path / "baselines" / PROJ).mkdir(parents=True)
    (tmp_path / "baselines" / PROJ / "home.png").write_text("png")

    locs = tmp_path / "learned_locators.json"
    locs.write_text(json.dumps({"com.app.under.test": {"a": 1},
                                "com.someone.else": {"b": 2}}))

    monkeypatch.setattr(purge_mod, "_ROOT", str(tmp_path))
    monkeypatch.setattr(purge_mod, "BASELINE_ROOT", str(tmp_path / "baselines"))
    monkeypatch.setattr(purge_mod, "LEARNED_LOCATORS", str(locs))

    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE test_projects (id TEXT PRIMARY KEY, name TEXT, "
                       "app_bundle_id TEXT)"))
        c.execute(text("CREATE TABLE test_runs (id TEXT PRIMARY KEY, project_id TEXT, "
                       "status TEXT)"))
        c.execute(text("CREATE TABLE saved_scenarios (id TEXT PRIMARY KEY, "
                       "project_id TEXT, bundle_id TEXT)"))
        c.execute(text("CREATE TABLE project_settings (id TEXT PRIMARY KEY, "
                       "project_id TEXT, github_token TEXT)"))
        c.execute(text("INSERT INTO test_projects VALUES "
                       "(:p,'Under Test','com.app.under.test')"), {"p": PROJ})
        c.execute(text("INSERT INTO test_runs VALUES ('run-1',:p,'passed')"), {"p": PROJ})
        c.execute(text("INSERT INTO test_runs VALUES ('run-2',:p,'failed')"), {"p": PROJ})
        c.execute(text("INSERT INTO saved_scenarios VALUES ('sc-1',:p,NULL)"), {"p": PROJ})
        c.execute(text("INSERT INTO project_settings VALUES ('ps-1',:p,'ghp_secret')"),
                  {"p": PROJ})

    db = sessionmaker(bind=engine)()
    yield tmp_path, db
    db.close()


# ── history is kept ──────────────────────────────────────────────────────────
def test_runs_are_detached_not_deleted(sandbox):
    _root, db = sandbox
    purge_project(db, PROJ, repos_base=str(_root / "repos"))
    rows = db.execute(text("SELECT id, project_id, status FROM test_runs "
                           "ORDER BY id")).fetchall()
    assert [r[0] for r in rows] == ["run-1", "run-2"]     # both still there
    assert all(r[1] is None for r in rows)                # detached
    assert [r[2] for r in rows] == ["passed", "failed"]   # outcome preserved


def test_the_plan_says_history_is_kept(sandbox):
    root, db = sandbox
    plan = plan_purge(db, PROJ, repos_base=str(root / "repos"))
    assert plan.runs_detached == 2
    assert "test_runs" not in plan.rows          # never listed for deletion
    assert "KEEP" in plan.render()


# ── traces are removed ───────────────────────────────────────────────────────
def test_the_clone_and_baselines_are_removed(sandbox):
    root, db = sandbox
    purge_project(db, PROJ, repos_base=str(root / "repos"))
    assert not (root / "repos" / PROJ).exists()
    assert not (root / "baselines" / PROJ).exists()


def test_project_scoped_rows_including_secrets_are_deleted(sandbox):
    root, db = sandbox
    purge_project(db, PROJ, repos_base=str(root / "repos"))
    for table in ("test_projects", "saved_scenarios", "project_settings"):
        col = "id" if table == "test_projects" else "project_id"
        assert db.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {col} = :p"),
                          {"p": PROJ}).scalar() == 0, table


# ── the locator store ────────────────────────────────────────────────────────
def test_only_this_projects_locator_entry_is_dropped(sandbox):
    root, db = sandbox
    purge_project(db, PROJ, repos_base=str(root / "repos"))
    data = json.loads((root / "learned_locators.json").read_text())
    assert "com.app.under.test" not in data
    assert "com.someone.else" in data


def test_a_bundle_id_shared_with_a_surviving_project_is_not_dropped(sandbox):
    root, db = sandbox
    db.execute(text("INSERT INTO test_projects VALUES "
                    "('other','Other','com.app.under.test')"))
    db.commit()
    plan = plan_purge(db, PROJ, repos_base=str(root / "repos"))
    assert plan.locator_key is None
    assert "also uses" in plan.locator_kept_because


# The real collision on this machine: prod 'vyaconsumer' is a PREFIX of the staging
# key, so a naive lenient match hands one project's purge the other's entry.
def test_a_prefix_match_never_steals_a_longer_owned_key():
    assert _locator_key_for("org.vyapy.sarls.vyaconsumer",
                            ["org.vyapy.sarls.vyaconsumerstaging"]) is None


def test_the_true_owner_still_drops_its_own_key(tmp_path, monkeypatch):
    store = tmp_path / "l.json"
    store.write_text(json.dumps({"org.vyapy.sarls.vyaconsumerstaging": {}}))
    monkeypatch.setattr(purge_mod, "LEARNED_LOCATORS", str(store))
    assert _locator_key_for("org.vyapy.sarls.vyaconsumerstaging",
                            ["org.vyapy.sarls.vyaconsumer"]) == \
        "org.vyapy.sarls.vyaconsumerstaging"


def test_suffix_drift_still_resolves_when_unambiguous(tmp_path, monkeypatch):
    # The store carries the running app's id; the project row carries the plain one.
    store = tmp_path / "l.json"
    store.write_text(json.dumps({"com.x.appstaging": {}}))
    monkeypatch.setattr(purge_mod, "LEARNED_LOCATORS", str(store))
    assert _locator_key_for("com.x.app", []) == "com.x.appstaging"


# ── dry run and orphans ──────────────────────────────────────────────────────
def test_a_dry_run_changes_nothing(sandbox):
    root, db = sandbox
    purge_project(db, PROJ, repos_base=str(root / "repos"), dry_run=True)
    assert (root / "repos" / PROJ).exists()
    assert (root / "baselines" / PROJ).exists()
    assert db.execute(text("SELECT COUNT(*) FROM test_projects")).scalar() == 1
    assert json.loads((root / "learned_locators.json").read_text()).get("com.app.under.test")


def test_a_clone_with_no_project_row_is_reported_as_orphaned(sandbox):
    root, db = sandbox
    (root / "repos" / "left-behind").mkdir()
    found = find_orphans(db, repos_base=str(root / "repos"))
    assert "left-behind" in found["clones"]
    assert PROJ not in found["clones"]          # this one still has its row


def test_an_orphaned_clone_can_be_purged_without_a_db_row(sandbox):
    root, db = sandbox
    (root / "repos" / "left-behind").mkdir()
    plan = purge_project(db, "left-behind", repos_base=str(root / "repos"))
    assert not (root / "repos" / "left-behind").exists()
    assert plan.exists_in_db is False
