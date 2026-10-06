"""The Inspector must find idb wherever this machine keeps it.

On a second Mac the Inspector showed:

    Could not read the UI tree: [Errno 2] No such file or directory:
    '/usr/local/bin/idb'

inspector.py hardcoded that path. It is where Intel Homebrew puts idb; Apple
Silicon uses /opt/homebrew/bin, a virtualenv uses its own bin, and a machine
without idb has none. So the Inspector worked where it was written and, on any
other machine, reported a raw ENOENT naming a path the operator has no reason
to recognise — with nothing said about what idb is or how to get it.

Resolution is shared with the scenario runner rather than reimplemented here:
scenarios/idb_path.py already honours IDB_BINARY and falls back to PATH.
"""
import inspect as _inspect

import pytest
from fastapi import HTTPException

from automation.api.v1.routers import inspector


def test_01_no_hardcoded_absolute_path_remains():
    """Structural: the bug was a literal, so assert the literal is gone."""
    src = _inspect.getsource(inspector)
    code = "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith(("#", '"', "'")))
    assert '"/usr/local/bin/idb"' not in code, \
        "the Inspector still pins one machine's idb path"


def test_02_the_env_override_is_honoured(monkeypatch, tmp_path):
    fake = tmp_path / "idb"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("IDB_BINARY", str(fake))
    assert inspector._idb_path() == str(fake)


def test_03_path_lookup_is_used_when_no_override(monkeypatch, tmp_path):
    """An install anywhere on PATH is accepted — /opt/homebrew on Apple Silicon,
    a virtualenv's own bin, anywhere. The path must EXIST: idb_binary() ends in
    a hardcoded default, so a returned string is not evidence of a real tool."""
    elsewhere = tmp_path / "opt-homebrew-bin-idb"
    elsewhere.write_text("#!/bin/sh\n")
    elsewhere.chmod(0o755)
    monkeypatch.delenv("IDB_BINARY", raising=False)
    monkeypatch.setattr("automation.scenarios.idb_path.shutil.which",
                        lambda n: str(elsewhere) if n == "idb" else None)
    assert inspector._idb_path() == str(elsewhere)


def test_04_a_missing_idb_explains_itself(monkeypatch):
    """The failure must say what idb is and how to install it — not ENOENT."""
    monkeypatch.delenv("IDB_BINARY", raising=False)
    monkeypatch.setattr("automation.scenarios.idb_path.shutil.which",
                        lambda n: None)
    # which() finds nothing, so idb_binary() returns its hardcoded default —
    # which must NOT be accepted just because it is a non-empty string.
    monkeypatch.setattr("os.path.isfile", lambda p: False)
    with pytest.raises(HTTPException) as e:
        inspector._idb_path()
    assert e.value.status_code == 503, "a missing tool is not a bad gateway"
    detail = e.value.detail
    assert "idb" in detail and "brew" in detail, "no installation instructions"
    assert "unaffected" in detail, \
        "must say that scenario execution still works without it"


def test_05_the_resolver_is_shared_with_the_runner():
    """One resolver, not two: the runner already had a working one."""
    src = _inspect.getsource(inspector._idb_path)
    assert "idb_path" in src and "idb_binary" in src
