"""The one place process configuration is loaded.

Every process that touches the database — API, agent, CLI, workers, scripts, tests —
must resolve the SAME database. Before this module, `.env` was loaded by
`api/main.py` and `agent/main.py` only, so anything else (a CLI run, a worker, an
ad-hoc script) silently fell back to the in-repo `test_automation_new.db` while the
API used the configured one. Two processes, two databases, no error: a project
created by a script was invisible to the backend, which is exactly the failure that
made a "staging project" appear not to exist.

`database/config.py` imports this before reading DATABASE_URL, so importing anything
that reaches the database is enough to get the right configuration. Nothing else
should call load_dotenv().

Precedence (highest first):
  1. A variable already set in the environment — so `DATABASE_URL=… python …` and the
     test suite's conftest both win. load_dotenv() never overrides these.
  2. .env at the repository root.
  3. The documented development default (see DEFAULT_DATABASE_URL).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"

# The development default, used only when neither the environment nor .env says
# otherwise. It is deliberately NOT the historical in-repo test_automation_new.db:
# that file still exists, is stale, and silently resolving to it is the bug this
# module exists to prevent. A fresh machine with no .env gets an obviously-named,
# obviously-local file instead, and the resolution is logged.
DEFAULT_DATABASE_URL = f"sqlite:///{(PROJECT_ROOT / 'platform-dev.db').as_posix()}"

_loaded = False


def load_env(*, override: bool = False) -> None:
    """Load .env into os.environ. Idempotent; safe to call from anywhere.

    Never overrides variables already set, so an explicit DATABASE_URL on the command
    line and the test suite's conftest both keep precedence.
    """
    global _loaded
    if _loaded and not override:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:            # python-dotenv absent: environment only.
        logger.debug("python-dotenv not installed; using the environment as-is")
        _loaded = True
        return
    if ENV_FILE.is_file():
        load_dotenv(ENV_FILE, override=override)
    _loaded = True


def database_url() -> str:
    """The configured database URL, fully resolved.

    Relative sqlite paths are anchored to the repository root so the same URL means
    the same file regardless of the process's working directory.
    """
    load_env()
    url = os.getenv("DATABASE_URL")
    if not url:
        url = DEFAULT_DATABASE_URL
        logger.warning(
            "DATABASE_URL is not set (checked the environment and %s) — falling back "
            "to the development default %s. Set DATABASE_URL to be explicit.",
            ENV_FILE, url)
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        relative = url.removeprefix("sqlite:///")
        url = f"sqlite:///{(PROJECT_ROOT / relative).as_posix()}"
    return url
