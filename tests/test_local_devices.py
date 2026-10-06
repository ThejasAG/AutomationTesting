"""Role devices must exist on the Mac the run is on.

The Quick demo failed "DEPLOYMENT BLOCKED … Could not boot simulator DA24A392:
Invalid device or device pair" -- DA24A392 is one developer's iPhone, hardcoded
as the default. Every other Mac has different simulator UDIDs.
"""
from automation.scenarios.cross_app_config import local_udid_for, localize_devices

OTHER_MAC = {"consumer": "DA24A392-FF1B-4283-A5CE-CDDE0D000D21",
             "waiter": "D19D3EC7-5494-4B69-AC7B-3AB8AE0B4D1B",
             "kitchen": "D19D3EC7-5494-4B69-AC7B-3AB8AE0B4D1B"}

SIMS = [  # list_ios_simulators() order: booted first, then by name
    {"udid": "PHONE-PRO", "name": "iPhone 16 Pro", "state": "Booted", "ios": "18.1"},
    {"udid": "IPAD-AIR", "name": "iPad Air 11-inch (M2)", "state": "Shutdown", "ios": "18.1"},
    {"udid": "IPAD-PRO", "name": "iPad Pro 11-inch (M4)", "state": "Shutdown", "ios": "18.1"},
    {"udid": "PHONE-16", "name": "iPhone 16", "state": "Shutdown", "ios": "18.1"},
]


def test_foreign_ids_are_replaced_by_local_sims_of_the_right_kind():
    out = localize_devices(OTHER_MAC, sims=SIMS)
    assert out["consumer"] == "PHONE-PRO"
    assert out["waiter"] == "IPAD-PRO", "same model as the default (iPad Pro 11)"
    assert out["kitchen"] == out["waiter"], "waiter+kitchen share one iPad"


def test_ids_that_exist_here_are_kept():
    mine = {"consumer": "PHONE-16", "waiter": "IPAD-AIR", "kitchen": "IPAD-PRO"}
    assert localize_devices(mine, sims=SIMS) == mine


def test_booted_wins_over_model_match():
    sims = [dict(s, state="Booted") if s["udid"] == "IPAD-AIR" else s for s in SIMS]
    assert local_udid_for("iPad", OTHER_MAC["waiter"], sims=sims) == "IPAD-AIR"


def test_exclude_keeps_the_business_phone_off_the_consumer_phone():
    assert local_udid_for("iPhone", "B1093E61-C510-4E6E-8A60-C2D05D150F64",
                          exclude=["PHONE-PRO"], sims=SIMS) == "PHONE-16"


def test_no_simctl_changes_nothing():
    assert localize_devices(OTHER_MAC, sims=[]) == OTHER_MAC


def test_sims_older_than_xcode_are_not_picked(monkeypatch):
    # Xcode 26's XCTest cannot load on iOS 18: WDA dies, ECONNREFUSED :8100.
    from automation.projects import simulators
    monkeypatch.setattr(simulators, "sdk_major", lambda: 26)
    sims = [
        {"udid": "OLD-PHONE", "name": "iPhone 16 Pro", "state": "Booted", "ios": "18.1"},
        {"udid": "NEW-PHONE", "name": "iPhone 17 Pro", "state": "Shutdown", "ios": "26.5"},
        {"udid": "NEW-AIR", "name": "iPhone Air", "state": "Shutdown", "ios": "26.5"},
        {"udid": "NEW-IPAD", "name": "iPad Pro 11-inch (M5)", "state": "Shutdown", "ios": "26.5"},
    ]
    out = localize_devices({"consumer": "OLD-PHONE", "waiter": "x", "kitchen": "x"}, sims=sims)
    assert out["consumer"] == "NEW-PHONE", "an existing but too-old sim is replaced; Pro preferred"
    assert out["waiter"] == out["kitchen"] == "NEW-IPAD"


def test_only_old_sims_still_returns_something(monkeypatch):
    from automation.projects import simulators
    monkeypatch.setattr(simulators, "sdk_major", lambda: 26)
    old = [{"udid": "A", "name": "iPhone 16", "state": "Booted", "ios": "18.1"}]
    assert simulators.testable(old) == old, "never turn a working pick into none"


def test_prepare_picker_skips_old_runtime(monkeypatch):
    import json
    from automation.projects import builder, simulators
    monkeypatch.setattr(simulators, "sdk_major", lambda: 26)
    data = {"devices": {
        "com.apple.CoreSimulator.SimRuntime.iOS-18-1": [
            {"udid": "OLD", "name": "iPhone 16 Pro", "state": "Booted", "isAvailable": True}],
        "com.apple.CoreSimulator.SimRuntime.iOS-26-5": [
            {"udid": "NEW", "name": "iPhone 17 Pro", "state": "Booted", "isAvailable": True}]}}
    monkeypatch.setattr(builder, "_run", lambda *a, **k: (True, json.dumps(data)))
    udid, note = builder.app_builder.resolve_ios_device("OLD", prefer="iPhone")
    assert udid == "NEW" and "older than Xcode" in note


def test_older_xcode_does_not_filter_older_runtimes(monkeypatch):
    # Only Xcode 26's XCTest needs an iOS 26 runtime; Xcode 16 drives iOS 17 fine,
    # so a user's saved iOS 17 iPad must not be swapped out there.
    from automation.projects import simulators
    monkeypatch.setattr(simulators, "sdk_major", lambda: 18)
    sims = [{"udid": "IPAD-17", "name": "iPad Pro 11-inch (M4)", "state": "Shutdown", "ios": "17.5"},
            {"udid": "IPAD-18", "name": "iPad Pro 11-inch (M4)", "state": "Booted", "ios": "18.1"}]
    assert local_udid_for("iPad", "IPAD-17", sims=sims) == "IPAD-17"


def test_unknown_sdk_is_not_cached(monkeypatch):
    from automation.projects import simulators
    monkeypatch.setattr(simulators, "_SDK_MAJOR", None)
    outs = iter(["", "iphonesimulator26.5"])
    monkeypatch.setattr(simulators.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": next(outs)})())
    assert simulators.sdk_major() == 0
    assert simulators.sdk_major() == 26, "an SDK installed later must be seen"
