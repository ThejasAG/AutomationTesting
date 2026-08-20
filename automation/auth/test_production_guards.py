"""The security guards must FAIL CLOSED when APP_ENV is production.

Each check reloads the module under a patched environment, because the guards
run at import time — testing them any other way tests nothing.
"""
import importlib
import os
import sys

import pytest


def _load(**env):
    """Import auth.security fresh under the given environment."""
    old = dict(os.environ)
    os.environ.update({k: v for k, v in env.items() if v is not None})
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
    sys.modules.pop("automation.auth.security", None)
    try:
        return importlib.import_module("automation.auth.security")
    finally:
        os.environ.clear()
        os.environ.update(old)
        sys.modules.pop("automation.auth.security", None)


def test_production_refuses_to_start_without_a_signing_key():
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        _load(APP_ENV="production", JWT_SECRET_KEY=None)


def test_unknown_app_env_is_treated_as_production():
    # A typo must not silently downgrade to the dev fallback.
    with pytest.raises(RuntimeError):
        _load(APP_ENV="prodcution", JWT_SECRET_KEY=None)


def test_dev_still_starts_without_a_key():
    m = _load(APP_ENV="dev", JWT_SECRET_KEY=None)
    assert m.IS_PRODUCTION is False
    assert m.SECRET_KEY  # a dev fallback exists


def test_production_key_is_never_the_committed_constant():
    m = _load(APP_ENV="production", JWT_SECRET_KEY="a" * 64)
    assert m.SECRET_KEY == "a" * 64
    assert "09d25e094faa6ca2556c818166b7a9563b93f7099f6f0f4caa6cf63b88e8d3e7" != m.SECRET_KEY


def test_agent_token_mismatch_is_rejected():
    from fastapi import HTTPException
    m = _load(APP_ENV="dev", JWT_SECRET_KEY="x" * 64, AGENT_TOKEN="right-token")
    assert m.require_agent("right-token") == "agent"
    with pytest.raises(HTTPException) as e:
        m.require_agent("wrong-token")
    assert e.value.status_code == 401
    with pytest.raises(HTTPException):
        m.require_agent("")          # missing header is not a free pass


def test_production_without_an_agent_token_refuses_service():
    from fastapi import HTTPException
    m = _load(APP_ENV="production", JWT_SECRET_KEY="x" * 64, AGENT_TOKEN=None)
    with pytest.raises(HTTPException) as e:
        m.require_agent("anything")
    assert e.value.status_code == 503


def test_secrets_are_redacted_from_child_logger_output():
    """Every module logs through a NAMED logger, so redaction that only covers
    the root logger protects nothing in practice."""
    import io
    import logging
    from automation.utils.security import install_secret_filter

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root = logging.getLogger()
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.INFO)
    try:
        install_secret_filter()
        secret = "ghp_AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH1234"
        logging.getLogger().info("root %s", secret)
        logging.getLogger("builds").info("child token=%s" % secret)
        logging.getLogger("a.b.c").info("deep token=%s" % secret)
        handler.flush()
        assert secret not in buf.getvalue(), "secret leaked into logs"
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
