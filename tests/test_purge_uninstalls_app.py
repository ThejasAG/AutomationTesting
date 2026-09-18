"""A purge must take the installed app with it — but never a shared one.

Purging the row and the clone while leaving the binary installed is how a
"deleted" project keeps driving runs: a debug build carries no main.jsbundle, so
the leftover app loads whatever Metro is up, which need not match anything still
on disk.

The guard matters just as much. Bundle ids are not unique to a project here — the
same app is registered under two projects — so uninstalling on one purge would
pull the app out from under the other.
"""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from automation.projects import purge as purge_mod
from automation.projects.purge import plan_purge, purge_project


@pytest.fixture
def db(tmp_path):
    """A throwaway SQLite session with just the tables a purge touches."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path}/purge.db")
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE test_projects (id TEXT PRIMARY KEY, name TEXT, app_bundle_id TEXT)"))
        c.execute(text("CREATE TABLE test_runs (id TEXT PRIMARY KEY, project_id TEXT)"))
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _add_project(db, pid, name, bundle_id):
    db.execute(text("INSERT INTO test_projects (id, name, app_bundle_id) VALUES (:i,:n,:b)"),
               {"i": pid, "n": name, "b": bundle_id})
    db.commit()


def test_plan_lists_booted_devices_carrying_the_app(db, tmp_path):
    _add_project(db, "p1", "Consumer", "org.example.consumer")

    with patch.object(purge_mod, "_devices_with_app", return_value=["SIM-A", "SIM-B"]):
        plan = plan_purge(db, "p1", repos_base=str(tmp_path))

    assert plan.uninstall_from == ["SIM-A", "SIM-B"]
    assert not plan.app_kept_because


def test_a_shared_bundle_id_is_never_uninstalled(db, tmp_path):
    """Two projects, one app. Purging one must not uninstall the other's app."""
    _add_project(db, "p1", "Staging Consumer", "org.example.consumer")
    _add_project(db, "p2", "Consumer", "org.example.consumer")

    with patch.object(purge_mod, "_devices_with_app", return_value=["SIM-A"]) as devices:
        plan = plan_purge(db, "p1", repos_base=str(tmp_path))

    assert plan.uninstall_from == [], "a bundle another project claims must survive"
    assert "Consumer" in plan.app_kept_because
    devices.assert_not_called(), "must not even look for devices for a shared bundle"


def test_purge_uninstalls_and_records_what_it_removed(db, tmp_path):
    _add_project(db, "p1", "Consumer", "org.example.consumer")
    builder = MagicMock()
    builder.uninstall.return_value = (True, "Uninstalled org.example.consumer")

    with patch.object(purge_mod, "_devices_with_app", return_value=["SIM-A"]), \
         patch.dict("sys.modules", {"automation.projects.builder": MagicMock(app_builder=builder)}):
        plan = purge_project(db, "p1", repos_base=str(tmp_path))

    builder.uninstall.assert_called_once_with("SIM-A", "org.example.consumer", "ios")
    assert plan.uninstalled == ["SIM-A"]


def test_dry_run_uninstalls_nothing(db, tmp_path):
    _add_project(db, "p1", "Consumer", "org.example.consumer")
    builder = MagicMock()

    with patch.object(purge_mod, "_devices_with_app", return_value=["SIM-A"]), \
         patch.dict("sys.modules", {"automation.projects.builder": MagicMock(app_builder=builder)}):
        plan = purge_project(db, "p1", repos_base=str(tmp_path), dry_run=True)

    builder.uninstall.assert_not_called()
    assert plan.uninstall_from == ["SIM-A"], "a dry run still reports what it would remove"
    assert plan.uninstalled == []
    assert db.execute(text("SELECT COUNT(*) FROM test_projects")).scalar() == 1


def test_a_failed_uninstall_warns_rather_than_raising(db, tmp_path):
    """The rows are already committed; a stuck simulator must not blow up the purge."""
    _add_project(db, "p1", "Consumer", "org.example.consumer")
    builder = MagicMock()
    builder.uninstall.return_value = (False, "device is not booted")

    with patch.object(purge_mod, "_devices_with_app", return_value=["SIM-A"]), \
         patch.dict("sys.modules", {"automation.projects.builder": MagicMock(app_builder=builder)}):
        plan = purge_project(db, "p1", repos_base=str(tmp_path))

    assert plan.uninstalled == []
    assert any("could not uninstall" in w for w in plan.warnings)
    assert db.execute(text("SELECT COUNT(*) FROM test_projects")).scalar() == 0


def test_no_bundle_id_means_nothing_to_uninstall(db, tmp_path):
    _add_project(db, "p1", "Web project", None)

    with patch.object(purge_mod, "_devices_with_app") as devices:
        plan = plan_purge(db, "p1", repos_base=str(tmp_path))

    devices.assert_not_called()
    assert plan.uninstall_from == []
