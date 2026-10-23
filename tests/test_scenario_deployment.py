"""Scenario preflight deploys the apps it needs, onto the devices it was assigned.

Preflight used to only CHECK, then tell the user to go install by hand — even though
the platform already knew how to build and install. These cover the deploy decision
tree: present / missing / stale / wrong-variant / no artifact / dead device, and that
each app goes to its OWN device.

Every simctl and build call is stubbed; nothing here touches a real simulator.
"""
import plistlib

import pytest

from automation.projects import deployment as D
from automation.projects.builder import app_builder

CONSUMER = "org.vyapy.sarls.vyaconsumerstaging"
BUSINESS = "org.vyapy.sarls.vyabusinessipadstaging"
PHONE = "AAAAAAAA-1111-2222-3333-444444444444"
TABLET = "BBBBBBBB-5555-6666-7777-888888888888"


@pytest.fixture
def sim(monkeypatch):
    """A fake simulator estate: what is installed, and what install() does to it."""
    state = {
        "installed": {},                  # udid -> {bundle: (version, build)}
        "installs": [],                   # (udid, artifact) in order
        "builds": [],                     # (repo, bundle) in order
        "booted": True,
    }

    def ensure_booted(udid):
        return (True, "booted") if state["booted"] else (False, "could not boot")

    def is_installed(udid, bundle):
        return bundle in state["installed"].get(udid, {})

    def installed_version(udid, bundle):
        v = state["installed"].get(udid, {}).get(bundle)
        return {"version": v[0], "build": v[1]} if v else {"version": None,
                                                           "build": None}

    def install(udid, artifact, platform):
        state["installs"].append((udid, artifact))
        meta = state.get("artifact_meta", {}).get(artifact, (CONSUMER, "2.0", "99"))
        state["installed"].setdefault(udid, {})[meta[0]] = (meta[1], meta[2])
        return True, f"Installed {artifact}"

    monkeypatch.setattr(app_builder, "ensure_ios_booted", ensure_booted)
    monkeypatch.setattr(app_builder, "is_installed", is_installed)
    monkeypatch.setattr(app_builder, "installed_version", installed_version)
    monkeypatch.setattr(app_builder, "install", install)
    return state


def _artifact(tmp_path, bundle, version="2.0", build="99", name="app.app"):
    app = tmp_path / name
    app.mkdir(parents=True, exist_ok=True)
    with open(app / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleIdentifier": bundle, "CFBundleExecutable": "x",
                       "CFBundleShortVersionString": version,
                       "CFBundleVersion": build}, f)
    with open(app / "x", "wb") as f:
        f.write(b"\xcf\xfa\xed\xfe")
    return str(app)


@pytest.fixture
def project(monkeypatch, tmp_path):
    """A project that owns CONSUMER and has a matching artifact on disk."""
    art = _artifact(tmp_path, CONSUMER)
    monkeypatch.setattr(D, "project_for_bundle",
                        lambda b: {"id": "p1", "name": "Staging Consumer",
                                   "platform": "ios", "branch": "any",
                                   "app_path": art} if b == CONSUMER else None)
    return art


# ── the decision tree ────────────────────────────────────────────────────────

def test_a_missing_app_is_deployed(sim, project):
    """Requirement 1: missing -> the platform installs it, no user action."""
    req = D.AppRequirement(role="consumer", bundle_id=CONSUMER, device_id=PHONE)
    res = D.ensure_app_on_device(req)
    assert res.installed and res.action == "installed"
    assert sim["installs"] == [(PHONE, project)]


def test_an_already_installed_app_is_not_reinstalled(sim, project):
    """Requirement 2: reinstalling costs ~30s AND wipes login state, which breaks
    scenarios that assume a signed-in app."""
    sim["installed"][PHONE] = {CONSUMER: ("2.0", "99")}
    req = D.AppRequirement(role="consumer", bundle_id=CONSUMER, device_id=PHONE)
    res = D.ensure_app_on_device(req)
    assert res.installed and res.action == "already-present"
    assert sim["installs"] == []


def test_a_stale_version_is_updated(sim, project):
    """Requirement 4: installed 1.0 but the artifact is 2.0 -> reinstall."""
    sim["installed"][PHONE] = {CONSUMER: ("1.0", "1")}
    req = D.AppRequirement(role="consumer", bundle_id=CONSUMER, device_id=PHONE)
    res = D.ensure_app_on_device(req)
    assert res.action == "reinstalled"
    assert res.previous_version == "1.0"
    assert sim["installs"] == [(PHONE, project)]


def test_a_missing_artifact_blocks_with_a_clear_message(sim, monkeypatch):
    """Requirement 6."""
    monkeypatch.setattr(D, "project_for_bundle",
                        lambda b: {"id": "p1", "name": "Staging Consumer",
                                   "platform": "ios", "branch": "any",
                                   "app_path": None})
    monkeypatch.setattr(D, "find_artifact", lambda *a, **k: None)
    req = D.AppRequirement(role="staging consumer", bundle_id=CONSUMER,
                           device_id=PHONE)
    with pytest.raises(D.DeploymentBlocked) as e:
        D.ensure_app_on_device(req, allow_build=False)
    msg = str(e.value)
    assert "DEPLOYMENT BLOCKED" in msg
    assert CONSUMER in msg and PHONE in msg          # names bundle AND device
    assert "No matching build artifact" in msg


def test_an_unavailable_device_is_reported_as_a_device_error(sim, project):
    """Requirement 7: a dead simulator must not read as a missing app."""
    sim["booted"] = False
    req = D.AppRequirement(role="consumer", bundle_id=CONSUMER, device_id=PHONE)
    with pytest.raises(D.DeploymentBlocked) as e:
        D.ensure_app_on_device(req)
    assert "device is not available" in str(e.value).lower()


def test_no_project_configured_for_the_bundle_is_reported(sim, monkeypatch):
    monkeypatch.setattr(D, "project_for_bundle", lambda b: None)
    req = D.AppRequirement(role="consumer", bundle_id="com.nope.unknown",
                           device_id=PHONE)
    with pytest.raises(D.DeploymentBlocked) as e:
        D.ensure_app_on_device(req, allow_build=False)
    assert "No project is configured" in str(e.value)


def test_an_install_that_does_not_take_is_caught(sim, project, monkeypatch):
    """`simctl install` exiting 0 is not proof — verify FROM the device."""
    monkeypatch.setattr(app_builder, "install", lambda *a, **k: (True, "ok"))
    req = D.AppRequirement(role="consumer", bundle_id=CONSUMER, device_id=PHONE)
    with pytest.raises(D.DeploymentBlocked) as e:
        D.ensure_app_on_device(req)
    assert "does not list" in str(e.value)


# ── multiple apps, multiple devices ──────────────────────────────────────────

def test_each_app_is_deployed_to_its_own_assigned_device(sim, monkeypatch, tmp_path):
    """Requirement 5: two apps, two simulators — the ordinary case."""
    ca = _artifact(tmp_path / "c", CONSUMER, name="c.app")
    ba = _artifact(tmp_path / "b", BUSINESS, name="b.app")
    sim["artifact_meta"] = {ca: (CONSUMER, "2.0", "99"), ba: (BUSINESS, "2.0", "99")}
    monkeypatch.setattr(D, "project_for_bundle", lambda b: {
        "id": "p", "name": "n", "platform": "ios", "branch": "any",
        "app_path": ca if b == CONSUMER else ba})
    results = D.prepare_scenario([
        D.AppRequirement(role="consumer", bundle_id=CONSUMER, device_id=PHONE),
        D.AppRequirement(role="business", bundle_id=BUSINESS, device_id=TABLET),
    ])
    assert all(r.installed for r in results)
    assert sim["installs"] == [(PHONE, ca), (TABLET, ba)]


def test_preflight_report_shows_each_app_and_its_device(sim, project):
    sim["installed"][PHONE] = {CONSUMER: ("2.0", "99")}
    req = D.AppRequirement(role="consumer", bundle_id=CONSUMER, device_id=PHONE)
    out = D.preflight_report([D.ensure_app_on_device(req)], passed=True)
    assert "SCENARIO PREFLIGHT" in out
    assert CONSUMER in out and PHONE[:8] in out
    assert "Installed : YES" in out and "Preflight: PASS" in out


# ── the scenario only requires what it actually drives ───────────────────────

def test_a_consumer_only_flow_does_not_require_the_business_app():
    """A one-segment consumer flow used to demand BOTH apps, sending the user off to
    build an app that flow never launches."""
    import inspect
    from automation.scenarios.cross_app_flows import FlowRunner
    src = inspect.getsource(FlowRunner._preflight)
    assert 'seg["role"]' in src and '"segments"' in src   # roles come from the flow
    assert 'if "consumer" in roles:' in src
    assert 'if roles - {"consumer"}:' in src
