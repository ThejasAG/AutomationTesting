"""Every process must resolve the SAME database.

`.env` used to be loaded by api/main.py and agent/main.py only. Anything else — a
CLI run, a worker, an ad-hoc script — silently fell back to the in-repo
`test_automation_new.db` while the API used the configured database. Two processes,
two databases, no error. A project created by a script was therefore invisible to the
backend, which is how a freshly-created staging project appeared not to exist.

These run the resolution in SUBPROCESSES on purpose: `automation.database.config`
computes DATABASE_URL at import time, so importing it in-process would only ever show
this suite's own (conftest-isolated) value.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[1])
STALE_DB = "test_automation_new.db"


def _resolve(env_extra: dict, code: str = None) -> str:
    """Resolve DATABASE_URL in a clean subprocess with *env_extra* applied."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("DATABASE_URL", "APP_ENV")}
    env["PYTHONPATH"] = REPO
    env.update(env_extra)
    src = code or "from automation.database.config import DATABASE_URL; print(DATABASE_URL)"
    r = subprocess.run([sys.executable, "-c", textwrap.dedent(src)],
                       capture_output=True, text=True, env=env, cwd=REPO, timeout=120)
    assert r.returncode == 0, f"resolution failed: {r.stderr[-2000:]}"
    return r.stdout.strip().splitlines()[-1]


def test_1_database_url_points_at_db_a(tmp_path):
    """When DATABASE_URL points to DB-A, database code uses DB-A."""
    db_a = tmp_path / "db-a.db"
    got = _resolve({"DATABASE_URL": f"sqlite:///{db_a}"})
    assert got == f"sqlite:///{db_a}"
    assert STALE_DB not in got


def test_2_a_standalone_process_matches_the_api(tmp_path):
    """The API and a standalone process must agree.

    Both are resolved here through their own import paths: the API's entry module
    (which loads .env the way uvicorn does) and a bare database import.
    """
    db = tmp_path / "shared.db"
    env = {"DATABASE_URL": f"sqlite:///{db}"}
    standalone = _resolve(env)
    via_api_entry = _resolve(env, """
        from automation.config import load_env
        load_env()                      # what automation/api/main.py does
        from automation.database.config import DATABASE_URL
        print(DATABASE_URL)
    """)
    assert standalone == via_api_entry == f"sqlite:///{db}"


def test_3_database_url_from_dotenv_is_respected(tmp_path):
    """A .env value is used when nothing is set in the environment."""
    env_file = tmp_path / ".env"
    db = tmp_path / "from-dotenv.db"
    env_file.write_text(f"DATABASE_URL=sqlite:///{db}\n")
    got = _resolve({}, f"""
        import automation.config as C
        C.ENV_FILE = __import__('pathlib').Path({str(env_file)!r})
        C._loaded = False
        print(C.database_url())
    """)
    assert got == f"sqlite:///{db}"


def test_4_a_test_can_override_without_touching_development_config(tmp_path):
    """An explicit environment variable beats .env — this is how conftest isolates
    the suite, and it must keep working."""
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=sqlite:///from-dotenv.db\n")
    chosen = tmp_path / "chosen.db"
    got = _resolve({"DATABASE_URL": f"sqlite:///{chosen}"}, f"""
        import automation.config as C
        C.ENV_FILE = __import__('pathlib').Path({str(env_file)!r})
        C._loaded = False
        print(C.database_url())
    """)
    assert got == f"sqlite:///{chosen}", "an explicit DATABASE_URL must win over .env"


def test_5_no_silent_fallback_to_the_stale_database(tmp_path):
    """With DATABASE_URL configured elsewhere, nothing may resolve the stale file."""
    db = tmp_path / "configured.db"
    got = _resolve({"DATABASE_URL": f"sqlite:///{db}"})
    assert STALE_DB not in got


def test_5b_an_unset_database_url_does_not_resolve_the_stale_file(tmp_path):
    """With nothing configured at all, the documented development default is used —
    never the stale in-repo database that happens to be lying around."""
    empty = tmp_path / ".env"
    empty.write_text("")
    got = _resolve({}, f"""
        import automation.config as C
        C.ENV_FILE = __import__('pathlib').Path({str(empty)!r})
        C._loaded = False
        print(C.database_url())
    """)
    assert STALE_DB not in got, f"silently fell back to the stale database: {got}"
    assert got == C_default(), got


def C_default() -> str:
    from automation.config import DEFAULT_DATABASE_URL
    return DEFAULT_DATABASE_URL


def test_dotenv_is_loaded_in_exactly_one_place():
    """The fix is a single canonical loader, not load_dotenv() sprinkled per file."""
    import ast

    hits = []
    for path in Path(REPO).rglob("*.py"):
        s = str(path)
        if "/.venv/" in s or "/__pycache__/" in s or "/tests/" in s:
            continue
        try:
            tree = ast.parse(path.read_text())
        except Exception:
            continue
        # Parse rather than grep: the loader modules mention load_dotenv() in
        # comments telling people not to call it, and a text match cannot tell a
        # warning from the thing it warns about.
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "id", None) == "load_dotenv"):
                hits.append(str(path.relative_to(REPO)))
                break
    assert hits == ["automation/config.py"], f"load_dotenv() outside the loader: {hits}"
