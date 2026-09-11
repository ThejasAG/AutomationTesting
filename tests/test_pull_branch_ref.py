"""A pull must target exactly one ref, and say so when it cannot.

Reported from a second Mac:

    git pull --rebase failed: From https://github.com/xorstack-admin/vya-consumer
     * branch preprod-2-May18 -> FETCH_HEAD
    fatal: Cannot rebase onto multiple branches.

git only says that when the fetch resolved to more than one ref. The clone had
a single branch of that name, no same-named tag, and the default refspec — so
the ambiguity came from the ARGUMENT, not the repository.

Two things made that hard to see, and both are fixed here:
  - `pull --rebase origin <name>` lets git interpret <name> loosely
  - the error was truncated to its first 400 characters, and git prints its
    "fatal:" line LAST, so the decisive part was the part being cut
"""
import pytest

from automation.projects.repository import repository_manager as rm


class _Res:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


@pytest.fixture
def spy(tmp_path, monkeypatch):
    """Capture the git argv a pull would run, without running git."""
    calls = []

    def _fake(args, cwd=None, **kw):
        calls.append(list(args))
        return _Res(0)

    monkeypatch.setattr(rm, "_run_git", _fake)
    monkeypatch.setattr(rm, "get_repo_path", lambda pid: str(tmp_path))
    monkeypatch.setattr(rm, "is_cloned", lambda pid: True)
    monkeypatch.setattr(rm, "_stash_venv", lambda p: None)
    monkeypatch.setattr(rm, "_restore_venv", lambda p, s: None)
    monkeypatch.setattr(rm, "_reset_worktree", lambda p: None)
    return calls


def _pull_argv(calls):
    return next((c for c in calls if "pull" in c), None)


def test_01_the_branch_is_fully_qualified(spy):
    rm.pull("proj", "preprod-2-May18")
    argv = _pull_argv(spy)
    assert argv is not None, spy
    assert argv[-1] == "refs/heads/preprod-2-May18", argv
    assert "preprod-2-May18" != argv[-1], "the bare name is ambiguous with a tag"


def test_02_surrounding_whitespace_is_stripped(spy):
    rm.pull("proj", "  preprod-2-May18  ")
    assert _pull_argv(spy)[-1] == "refs/heads/preprod-2-May18"


@pytest.mark.parametrize("bad", ["main master", "a\nb", "", "   ", "x\ty"])
def test_03_a_multi_ref_branch_name_is_refused(spy, bad):
    """The reported failure: two names reaching git as one argument."""
    with pytest.raises(RuntimeError) as e:
        rm.pull("proj", bad)
    assert "single ref" in str(e.value)
    assert _pull_argv(spy) is None, "git ran with an ambiguous ref"


def test_04_autostash_is_still_applied(spy):
    """_reset_worktree deliberately keeps ios/Podfile.lock modified; rebase
    refuses to start with unstaged changes without this."""
    rm.pull("proj", "main")
    argv = _pull_argv(spy)
    assert "rebase.autoStash=true" in argv and "--rebase" in argv


def test_05_the_error_keeps_gits_fatal_line(tmp_path, monkeypatch):
    """git prints 'fatal:' LAST, so truncating the first 400 chars hides the
    one line that says what went wrong."""
    long_fetch = "\n".join(f" * branch line-{i} -> FETCH_HEAD" for i in range(40))
    stderr = f"From https://example.com/repo\n{long_fetch}\nfatal: Cannot rebase onto multiple branches."

    def _fake(args, cwd=None, **kw):
        if "pull" in args:
            return _Res(1, "", stderr)
        return _Res(0)

    monkeypatch.setattr(rm, "_run_git", _fake)
    monkeypatch.setattr(rm, "get_repo_path", lambda pid: str(tmp_path))
    monkeypatch.setattr(rm, "is_cloned", lambda pid: True)
    monkeypatch.setattr(rm, "_stash_venv", lambda p: None)
    monkeypatch.setattr(rm, "_restore_venv", lambda p, s: None)
    monkeypatch.setattr(rm, "_reset_worktree", lambda p: None)

    with pytest.raises(RuntimeError) as e:
        rm.pull("proj", "main")
    assert "fatal: Cannot rebase onto multiple branches." in str(e.value), \
        "the decisive line was truncated away"
