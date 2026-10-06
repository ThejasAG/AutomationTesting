"""A Yarn project must be installed by Yarn, into a real node_modules tree.

Two failures on the reporting machine, in one run:

    WARNING: node_modules Installed — node_modules directory not found.
    WARNING: npm reported peer-dependency conflicts (ERESOLVE) and they were
             bypassed with --legacy-peer-deps
    ➤ YN0000: │ ESM support for PnP uses the experimental loader API

The PnP line is the first bug: the guard that writes `nodeLinker: node-modules`
was an equality test against ["corepack", "yarn"], and pinning the Yarn major
changed that value to ["corepack", "yarn@3", "--"]. The guard silently stopped
matching, Yarn defaulted to Plug'n'Play, and Metro had no tree to resolve from.

The npm line is the second: when the yarn install failed, the code fell through
to `npm install` in a project with a Berry lockfile — the exact corruption that
turned axios 1.7.7 into 1.20.0 and started this whole investigation.
"""
import os

import pytest

from automation.projects import preparation as prep
from automation.projects.preparation import preparation_service as svc


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "app"
    r.mkdir()
    (r / "package.json").write_text('{"name":"app","dependencies":{}}')
    (r / "yarn.lock").write_text(
        '# yarn\n\n__metadata:\n  version: 6\n  cacheKey: 8\n')
    monkeypatch.setattr(prep.repository_manager, "get_repo_path", lambda pid: str(r))
    return str(r)


def test_01_the_linker_guard_survives_a_pinned_yarn_major(repo, monkeypatch):
    """The regression: pinning the major must not disable the PnP guard."""
    wrote = []
    monkeypatch.setattr(svc, "_ensure_node_modules_linker",
                        lambda pid, path: wrote.append(path))
    monkeypatch.setattr(svc, "_run_in_repo", lambda pid, cmd: (True, None))
    svc._install_node_deps("p1", repo)
    assert wrote, "the Plug'n'Play guard never ran — Metro gets no node_modules"


def test_02_a_failed_yarn_install_never_falls_back_to_npm(repo, monkeypatch):
    """npm cannot read a Berry lockfile; reaching for it corrupts the tree."""
    ran = []

    def _fake(pid, cmd):
        ran.append(cmd)
        return False, "some yarn failure"

    monkeypatch.setattr(svc, "_ensure_node_modules_linker", lambda pid, p: None)
    monkeypatch.setattr(svc, "_run_in_repo", _fake)
    ok, err = svc._install_node_deps("p1", repo)
    assert not ok
    assert not any(c and c[0] == "npm" for c in ran), \
        f"npm was run in a Yarn project: {ran}"
    assert "npm" in err and "discard" in err, "the reason must be explained"


def test_03_a_project_with_no_yarn_lock_still_uses_npm(tmp_path, monkeypatch):
    """Only YARN projects are protected; an npm project installs with npm."""
    r = tmp_path / "npmapp"
    r.mkdir()
    (r / "package.json").write_text('{"name":"a","dependencies":{}}')
    monkeypatch.setattr(prep.repository_manager, "get_repo_path", lambda pid: str(r))
    ran = []
    monkeypatch.setattr(svc, "_run_in_repo",
                        lambda pid, cmd: (ran.append(cmd), (True, None))[1])
    svc._install_node_deps("p1", str(r))
    assert ran and ran[0][0] == "npm", ran


# ── .yarnrc.yml repair ──────────────────────────────────────────────────────

def test_04_a_pnp_linker_is_switched_to_node_modules(repo):
    """Yarn writes `nodeLinker: pnp` itself, so 'the project chose it' is not
    a safe assumption — and Metro cannot use it either way."""
    rc = os.path.join(repo, ".yarnrc.yml")
    open(rc, "w").write("nodeLinker: pnp\n")
    svc._ensure_node_modules_linker("p1", repo)
    assert "nodeLinker: node-modules" in open(rc).read()
    assert "pnp" not in open(rc).read()


def test_05_an_explicit_node_modules_choice_is_left_alone(repo):
    rc = os.path.join(repo, ".yarnrc.yml")
    open(rc, "w").write("nodeLinker: node-modules\nsomeOther: value\n")
    svc._ensure_node_modules_linker("p1", repo)
    assert open(rc).read() == "nodeLinker: node-modules\nsomeOther: value\n"


def test_06_a_missing_yarnrc_is_created(repo):
    svc._ensure_node_modules_linker("p1", repo)
    assert "nodeLinker: node-modules" in open(os.path.join(repo, ".yarnrc.yml")).read()
