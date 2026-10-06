"""The platform must work on a machine that has only a clone of this repo.

Each of these guards a failure that actually happened on THIS machine and would
happen again on a new one: a fresh clone cannot prepare, one network blip fails a
4-hour build, a freshly installed app starts at onboarding so every step fails, and
tooling reads a stale database that is not the configured one.
"""
import inspect
from pathlib import Path

import pytest

from automation.projects import preparation as P
from automation.projects.builder import app_builder
from automation.scenarios import cross_app_flows as F

REPO = Path(__file__).resolve().parents[1]


# ── 1. a fresh clone can prepare itself ──────────────────────────────────────

def test_preparation_generates_a_missing_automation_yaml_by_default():
    """automation.yaml is platform-owned and untracked, so a fresh clone NEVER has
    one. Defaulting to False made the first prepare on any new machine fail with
    'automation.yaml not found' — for a file the platform writes itself."""
    sig = inspect.signature(P.ProjectPreparationService.prepare_for_execution)
    assert sig.parameters["auto_generate_yaml"].default is True


def test_automation_yaml_is_not_expected_from_the_repository():
    """If it were tracked it would carry another machine's artifact path."""
    tracked = (REPO / "automation.yaml").exists()
    assert not tracked, "automation.yaml must not be committed — it is machine-specific"


# ── 2. a network blip must not fail a build ──────────────────────────────────

def test_registry_lookups_are_retried():
    src = inspect.getsource(app_builder._registry)
    assert "for attempt in range(3)" in src, "a single dropped request decides nothing"
    assert "time.sleep" in src, "retries must back off"


def test_a_failed_registry_lookup_is_not_cached():
    """Caching None made one blip permanent for the rest of the process."""
    src = inspect.getsource(app_builder._registry)
    assert "return None" in src
    assert "self._registry_cache[pkg] = None" not in src


def test_a_registry_failure_is_logged_with_its_cause():
    src = inspect.getsource(app_builder._registry)
    assert "after 3 attempts" in src


# ── 3. a freshly installed app starts at first run ───────────────────────────

def test_consumer_home_clears_a_first_run_intro_first():
    """The platform installs apps automatically now, so the first run after ANY
    deploy lands on onboarding, signed out."""
    src = inspect.getsource(F.FlowRunner._consumer_home)
    assert "_first_run_preamble" in src


def test_the_preamble_is_generic_not_app_specific():
    """Onboarding words, not one app's element ids — a newly onboarded app needs no
    code change here."""
    assert "skip" in F.FlowRunner._SKIP_LABELS
    src = inspect.getsource(F.FlowRunner._first_run_preamble)
    assert "org.vyapy" not in src, "no bundle id belongs in the preamble"
    assert "DA24A392" not in src, "no device id belongs in the preamble"


def test_the_preamble_is_a_no_op_when_not_at_first_run(monkeypatch):
    """Every flow calls it unconditionally, so it must do nothing on a normal app."""
    fr = object.__new__(F.FlowRunner)
    monkeypatch.setattr(F.FlowRunner, "_idb_els",
                        lambda self, udid="": [{"label": "homeSearchBar", "cx": 1, "cy": 1}])
    monkeypatch.setattr(F.FlowRunner, "_on_home", lambda self: True)
    tapped = []
    monkeypatch.setattr(F.FlowRunner, "_idb_tap",
                        lambda self, x, y, udid="": tapped.append((x, y)))
    notes = []
    assert fr._first_run_preamble(None, notes) is False
    assert tapped == [], "it tapped something on an app that was already past first run"


def test_the_preamble_dismisses_an_onboarding_carousel(monkeypatch):
    fr = object.__new__(F.FlowRunner)
    fr.credentials = {}
    screens = [[{"label": "Stop Exploring, Start Discovering", "cx": 5, "cy": 5},
                {"label": "Skip", "cx": 351, "cy": 103}],
               [{"label": "homeSearchBar", "cx": 1, "cy": 1}]]
    monkeypatch.setattr(F.FlowRunner, "_idb_els",
                        lambda self, udid="": screens[min(len(tapped), len(screens) - 1)])
    tapped = []
    monkeypatch.setattr(F.FlowRunner, "_idb_tap",
                        lambda self, x, y, udid="": tapped.append((x, y)))
    monkeypatch.setattr(F.FlowRunner, "_on_home", lambda self: bool(tapped))
    monkeypatch.setattr(F.FlowRunner, "_looks_signed_out", lambda self: False)
    notes = []
    assert fr._first_run_preamble(None, notes) is True
    assert tapped == [(351, 103)], "the Skip control was not tapped"


def test_markers_are_stored_already_normalised():
    """They are matched against a normalised blob, so a marker containing a space or
    capital could never match — the screen text has those stripped too. This silently
    disabled the whole preamble once."""
    for m in (*F.FlowRunner._FIRST_RUN_MARKERS, *F.FlowRunner._SKIP_LABELS):
        assert m == F._norm(m), f"{m!r} is not normalised and can never match"


def test_normalisation_makes_spelling_variants_equal():
    assert F._norm("SIGN-IN") == F._norm("Sign In") == F._norm("signIn") == "signin"


def test_sign_in_uses_configured_credentials_not_literals():
    src = inspect.getsource(F.FlowRunner._sign_in_consumer)
    assert 'self.credentials' in src
    assert "@" not in src.split('"""')[2] or "xorstack" not in src, \
        "credentials must come from configuration, never be written in code"


def test_missing_credentials_are_reported_rather_than_guessed(monkeypatch):
    fr = object.__new__(F.FlowRunner)
    fr.credentials = {}
    notes = []
    assert fr._sign_in_consumer(None, notes) is False
    assert any("credentials" in n for n in notes)


# ── 4. tooling must read the configured database ─────────────────────────────

def test_the_stale_repo_database_is_gone():
    assert not (REPO / "test_automation_new.db").exists(), \
        "the stale repo-root database is back; tooling will silently read it again"


@pytest.mark.parametrize("rel", ["install.py", "clear-screenshots.sh"])
def test_tooling_does_not_hardcode_a_database_filename(rel):
    text = (REPO / rel).read_text()
    assert "database_url" in text, f"{rel} must use the configured database"


# ── 5. a deployed app needs its device permissions ───────────────────────────

def test_deployment_grants_location_permission():
    """The home list is GPS-gated: with no location permission the backend returns
    zero restaurants and Home renders "NO DATA FOUND", so every step that looks for
    a restaurant card fails against a screen that is working correctly. app_builder
    only granted this from launch(), which the deploy path never calls."""
    from automation.projects import deployment as D
    src = inspect.getsource(D.ensure_app_on_device)
    assert src.count("_grant_device_permissions") >= 2, \
        "permissions must be granted on both the fresh-install and already-present paths"


def test_permission_grant_is_best_effort():
    """A simulator that refuses a grant must not fail the deployment."""
    from automation.projects import deployment as D
    src = inspect.getsource(D._grant_device_permissions)
    assert "except Exception" in src


def test_permission_grant_reuses_the_existing_builder_helper():
    """Don't reimplement simctl privacy/location — builder already does it."""
    from automation.projects import deployment as D
    src = inspect.getsource(D._grant_device_permissions)
    assert "_configure_ios_location" in src


# ── 6. slow first run on a cold machine must not be a failed run ─────────────

def test_pod_install_timeout_allows_a_cold_cocoapods_cache():
    """First `pod install` on a new Mac downloads the whole spec repo — measured at
    >30 min for a 108-pod project. The old 1800s ceiling killed it seconds after the
    pods had landed, turning a slow first run into a failed one."""
    from automation.projects import builder as B
    assert B.POD_TIMEOUT >= 3600
    assert B.POD_REPO_UPDATE_TIMEOUT > B.POD_TIMEOUT, \
        "--repo-update re-fetches specs by definition, so it needs longer"


def test_pod_timeouts_are_named_not_magic_numbers():
    from automation.projects import builder as B
    src = inspect.getsource(B.AppBuilder._pod_install)
    assert "timeout=1800" not in src and "timeout=2700" not in src


# ── 7. a same-version rebuild must still deploy ──────────────────────────────

def test_a_rebuild_with_the_same_version_is_still_deployed():
    """A config-only rebuild (different API host) keeps the same version and build
    number, so a version-only staleness check left the OLD binary on the device and
    the rebuild appeared to do nothing."""
    from automation.projects import deployment as D
    src = inspect.getsource(D.ensure_app_on_device)
    assert "_artifact_is_newer_than_install" in src


def test_an_unknown_build_time_does_not_force_a_reinstall():
    """Churn is not free: a reinstall wipes login and app state."""
    from automation.projects import deployment as D
    src = inspect.getsource(D._artifact_is_newer_than_install)
    assert "return False" in src
