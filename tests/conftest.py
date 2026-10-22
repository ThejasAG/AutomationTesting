"""Keep the test suite off the real database.

`automation/agent/main.py` calls load_dotenv() at import time, and .env points
DATABASE_URL at the live platform database. Importing that module from a test —
which several do — therefore repointed the WHOLE pytest process at production,
and any test that opened a session without an isolating fixture wrote to it.
That is how rows named `agent-a` / `UDID-X` reached the live devices table.

Setting DATABASE_URL here fixes it at the root: conftest is imported before any
test module, and load_dotenv() does not override variables that are already set,
so nothing downstream can drag the suite back onto the real database.
"""
import os
import tempfile

import pytest

_TEST_DB = os.path.join(tempfile.mkdtemp(prefix="automation-tests-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"

# Belt and braces: never let a stray import point the suite at the real agent
# token or platform either.
os.environ.setdefault("APP_ENV", "test")


def pytest_report_header(config):
    return f"database: {os.environ['DATABASE_URL']}"


@pytest.fixture(autouse=True)
def _no_real_app_relaunch(monkeypatch):
    """No unit test may restart an app on a live simulator.

    FlowRunner._relaunch_business runs `xcrun simctl terminate/launch` for real
    (an Appium relaunch could fire late, see its docstring), so a test that walks
    into it -- @open_reservation on an empty board, a leftover table sheet --
    would kill whatever the simulator is running. Tests that check the relaunch
    replace this stub on their runner instance."""
    try:
        from automation.scenarios.cross_app_flows import FlowRunner
    except Exception:
        yield
        return
    monkeypatch.setattr(FlowRunner, "_relaunch_business", lambda self, notes, tag: True)
    yield
