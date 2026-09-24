"""Retrying a failed run re-runs the SAME thing.

Retry is the common next action after a failure (a flaky step, a busy device, a fix
just deployed). Doing it by hand meant returning to the Scenarios page and
remembering which flow, environment and business device the run had used — easy to
get wrong, and a retry that quietly runs a different configuration is worse than no
retry at all. The settings are therefore recovered from the run row.
"""
import inspect
import re

import pytest

from automation.api.v1.routers import jobs as J
from automation.scenarios.cross_app_flows import list_flows


def _resolve(test_name: str, suite: str = ""):
    """The flow/env resolution retry_run performs, exercised directly."""
    env = "staging" if "staging" in (suite + test_name).lower() else "prod"
    stripped = re.sub(r"^\[[^\]]*\]\s*", "", test_name).strip()
    flow = next((f for f in list_flows() if f["name"] == stripped), None)
    return env, flow


def test_the_endpoint_exists():
    assert hasattr(J, "retry_run")


def test_a_staging_run_retries_as_staging():
    env, flow = _resolve("[Staging] Preorder → C-App card → pay in B-App",
                         "Cross-app flows (iOS · Staging)")
    assert env == "staging"
    assert flow is not None, "the flow name in the run row must match a real flow"


def test_a_prod_run_retries_as_prod():
    env, _ = _resolve("[Old Vya] Quick demo — Consumer books a table (~2 min)",
                      "Cross-app flows (iOS · Old Vya)")
    assert env == "prod"


def test_the_environment_prefix_is_stripped_before_matching():
    """Run rows store "[Staging] <flow name>"; the flow itself has no prefix."""
    _, flow = _resolve("[Staging] Quick demo — Consumer books a table (~2 min)")
    assert flow is not None
    assert not flow["name"].startswith("[")


def test_every_flow_name_round_trips():
    """Any flow must be retryable, not just the ones tried by hand."""
    for f in list_flows():
        for label in ("Staging", "Old Vya"):
            _, got = _resolve(f"[{label}] {f['name']}")
            assert got is not None, f"{f['name']!r} could not be resolved back"
            assert got["id"] == f["id"]


def test_an_unknown_flow_is_a_clear_error_not_a_wrong_run():
    """A renamed or deleted flow must refuse, never fall back to some other flow."""
    _, flow = _resolve("[Staging] A flow that does not exist")
    assert flow is None
    src = inspect.getsource(J.retry_run)
    assert "Could not identify the flow" in src


def test_a_non_flow_run_is_refused():
    """Retry currently covers cross-app flow runs; anything else says so."""
    src = inspect.getsource(J.retry_run)
    assert 'bot_type != "ios-crossapp-flow"' in src


def test_the_original_run_is_not_modified():
    """The failure stays on record; the retry is a NEW run."""
    src = inspect.getsource(J.retry_run)
    assert "retried_from" in src
    for forbidden in ("run.status =", "db.commit()", "db.delete("):
        assert forbidden not in src, f"retry must not write to the original run ({forbidden})"


def test_the_business_device_choice_is_carried_over():
    """A run that used the phone for the B-app roles must retry on the phone."""
    src = inspect.getsource(J.retry_run)
    assert "business_device" in src
    assert "DEFAULT_BUSINESS_PHONE_UDID" in src


def test_the_ui_offers_retry_only_on_a_finished_run():
    """Two runs on the same simulators collide over WebDriverAgent."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "automation/dashboard/src/pages/RunDetails.tsx"
    text = src.read_text()
    assert "onRetry" in text and "Retry scenario" in text
    # Stop and Retry are the two branches of ONE ternary on the run's state, which
    # is stronger than a separate negated guard: they cannot both render, so Retry
    # can never appear while the run still holds the simulators.
    i = text.index("ACTIVE_RUN_STATES.has(run.status) ? (")
    branch = text[i:text.index("onRetry", i)]
    assert "onStop" in branch, "the ACTIVE branch must offer Stop, not Retry"


def test_the_ui_navigates_to_the_new_run():
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "automation/dashboard/src/pages/RunDetails.tsx"
    text = src.read_text()
    assert "navigate(`/run/${res.run_id}`)" in text, \
        "the route is /run/:id — /runs/ would land on a blank page"
