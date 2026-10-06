"""Preflight: is the environment's app actually on the chosen simulator?

Appium's own failure for this is "App with bundle identifier '…' unknown", raised at
session-create — AFTER booting, Metro and a ~30s WebDriverAgent build. It names neither
the device nor what the device does have, so it reads like an Appium fault rather than
the wrong environment/device pair it almost always is.
"""
import pytest

from automation.api.v1.routers import scenario as ROUTER
from automation.scenarios import service as S
from automation.projects.builder import app_builder

PHONE = "B1093E61-C510-4E6E-8A60-C2D05D150F64"
PROD_BUSINESS = "org.vyapy.sarls.vyabusinessipad"
STG_BUSINESS = "org.vyapy.sarls.vyabusinessipadstaging"


@pytest.fixture
def fake_device(monkeypatch):
    """A phone carrying the staging Business app but not the prod one — the real
    iPhone 16 layout that produced the report."""
    present = [STG_BUSINESS, "org.vyapy.sarls.vyaconsumer",
               "com.apple.Preferences", "com.facebook.WebDriverAgentRunner.xctrunner"]
    monkeypatch.setattr(app_builder, "installed_bundles", lambda _d: list(present))
    monkeypatch.setattr(app_builder, "is_installed", lambda _d, b: b in present)
    return present


def test_an_installed_app_passes_silently(fake_device):
    assert S._app_missing_detail(PHONE, STG_BUSINESS) is None


def test_a_missing_app_is_named_along_with_the_device_contents(fake_device):
    detail = S._app_missing_detail(PHONE, PROD_BUSINESS)
    assert detail is not None
    assert PROD_BUSINESS in detail                 # what was asked for
    assert STG_BUSINESS in detail                  # what is actually there
    assert "not installed" in detail
    assert "Latest build" in detail                # and how to fix it


def test_apple_and_appium_apps_are_not_offered_as_alternatives(fake_device):
    """"Install Preferences instead" is not advice."""
    detail = S._app_missing_detail(PHONE, PROD_BUSINESS)
    assert "com.apple.Preferences" not in detail
    assert "WebDriverAgent" not in detail


def test_a_device_with_nothing_installed_still_explains_itself(monkeypatch):
    monkeypatch.setattr(app_builder, "installed_bundles", lambda _d: [])
    monkeypatch.setattr(app_builder, "is_installed", lambda _d, _b: False)
    detail = S._app_missing_detail(PHONE, PROD_BUSINESS)
    assert "no third-party apps" in detail


def test_the_check_runs_before_metro_and_webdriveragent():
    """It has to come first — booting, Metro and the WDA build all succeed against a
    device without the app, so checking later wastes ~40s per failed run."""
    import inspect
    src = inspect.getsource(S.scenario_events)
    assert src.index("_app_missing_detail") < src.index("Starting the JS bundler")
    assert src.index("_app_missing_detail") < src.index("building WebDriverAgent")


def test_the_batch_path_checks_too():
    import inspect
    src = inspect.getsource(ROUTER._batch_events)
    assert "_app_missing_detail" in src
    assert src.index("_app_missing_detail") < src.index("Starting Metro")
