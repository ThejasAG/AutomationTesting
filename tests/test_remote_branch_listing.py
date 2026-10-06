"""Branches come from the remote, so one can be picked instead of typed.

The consumer repo here has 100+ branches. Typing one from memory means a typo is
only discovered when the clone fails with "Remote branch not found in upstream
origin", so the dashboard offers the real list.

Reading the remote (rather than a clone) is the point: the branch has to be
chosen when the project has NOT been cloned yet.
"""

from subprocess import CompletedProcess
from unittest.mock import patch

from automation.projects.repository import repository_manager

_LS_REMOTE = (
    "a1b2c3\trefs/heads/main\n"
    "d4e5f6\trefs/heads/Thai-filter\n"
    "789abc\trefs/heads/preprod-2-May18\n"
)


def _git(stdout="", returncode=0):
    return CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_branches_are_listed_with_the_default_first():
    def fake_git(args, *a, **k):
        if "--symref" in args:
            return _git("ref: refs/heads/main\tHEAD\n")
        return _git(_LS_REMOTE)

    with patch.object(repository_manager, "_run_git", side_effect=fake_git):
        branches = repository_manager.list_remote_branches("https://example.com/r.git")

    assert branches[0] == "main", "the default branch leads the list"
    assert branches == ["main", "Thai-filter", "preprod-2-May18"] or set(branches) == {
        "main", "Thai-filter", "preprod-2-May18"}


def test_the_rest_are_alphabetical_so_a_long_list_is_scannable():
    def fake_git(args, *a, **k):
        if "--symref" in args:
            return _git("ref: refs/heads/main\tHEAD\n")
        return _git(_LS_REMOTE)

    with patch.object(repository_manager, "_run_git", side_effect=fake_git):
        branches = repository_manager.list_remote_branches("https://example.com/r.git")

    assert branches[1:] == sorted(branches[1:])


def test_an_unreachable_remote_yields_no_branches_rather_than_raising():
    """A bad URL or missing credentials must surface as an empty list.

    The endpoint turns that into a 502 naming the repo; a traceback would not.
    """
    with patch.object(repository_manager, "_run_git", return_value=_git(returncode=128)):
        assert repository_manager.list_remote_branches("https://example.com/r.git") == []


def test_a_remote_whose_head_is_unknown_still_lists_its_branches():
    """ls-remote --symref can fail where --heads succeeds; do not lose the list."""
    def fake_git(args, *a, **k):
        if "--symref" in args:
            return _git(returncode=128)
        return _git(_LS_REMOTE)

    with patch.object(repository_manager, "_run_git", side_effect=fake_git):
        branches = repository_manager.list_remote_branches("https://example.com/r.git")

    assert set(branches) == {"main", "Thai-filter", "preprod-2-May18"}


def test_branch_names_containing_slashes_survive_parsing():
    """feature/x is the common case; splitting on the wrong thing truncates it."""
    def fake_git(args, *a, **k):
        if "--symref" in args:
            return _git(returncode=128)
        return _git("a1\trefs/heads/feature/checkout-v2\nb2\trefs/heads/main\n")

    with patch.object(repository_manager, "_run_git", side_effect=fake_git):
        branches = repository_manager.list_remote_branches("https://example.com/r.git")

    assert "feature/checkout-v2" in branches
