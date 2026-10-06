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

_TEST_DB = os.path.join(tempfile.mkdtemp(prefix="automation-tests-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"

# Belt and braces: never let a stray import point the suite at the real agent
# token or platform either.
os.environ.setdefault("APP_ENV", "test")


def pytest_report_header(config):
    return f"database: {os.environ['DATABASE_URL']}"
