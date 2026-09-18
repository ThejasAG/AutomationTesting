"""Execute must build the branch the card is showing.

The branch picker chose a branch, but /prepare took only device_id and
generate_yaml — so the pipeline prepared whatever was already checked out and
silently rebuilt the previous branch. Nothing in the UI said so: the card kept
showing the branch that had been picked while the build used another.
"""

from unittest.mock import MagicMock, patch

from automation.projects.preparation import preparation_tracker


def test_the_tracker_passes_the_branch_through():
    with patch("automation.projects.preparation.preparation_service") as svc:
        svc.prepare_for_execution.return_value = MagicMock(
            ok=True, to_dict=lambda: {"ok": True})

        preparation_tracker.start("p1", device_id="SIM-A", branch="preprod-2-May18")
        # The worker runs on a thread; wait for it to finish.
        for t in list(getattr(preparation_tracker, "_threads", {}).values()):
            t.join(timeout=5)
        else:
            import time
            for _ in range(50):
                if svc.prepare_for_execution.called:
                    break
                time.sleep(0.05)

    kwargs = svc.prepare_for_execution.call_args.kwargs
    assert kwargs.get("branch") == "preprod-2-May18", (
        "the picked branch must reach prepare_for_execution, or Execute builds "
        "whatever happened to be checked out")


def test_no_branch_keeps_the_projects_default():
    """Omitting it must not force a branch — the project's default still applies."""
    with patch("automation.projects.preparation.preparation_service") as svc:
        svc.prepare_for_execution.return_value = MagicMock(
            ok=True, to_dict=lambda: {"ok": True})

        preparation_tracker.start("p2", device_id="SIM-A")
        import time
        for _ in range(50):
            if svc.prepare_for_execution.called:
                break
            time.sleep(0.05)

    assert svc.prepare_for_execution.call_args.kwargs.get("branch") is None


def test_the_api_body_accepts_a_branch():
    """The endpoint's model must carry it, or the UI's value is dropped silently."""
    from automation.api.v1.routers.projects import ValidateBody

    body = ValidateBody(device_id="SIM-A", branch="Thai-filter")

    assert body.branch == "Thai-filter"
    assert ValidateBody().branch is None, "branch stays optional"
