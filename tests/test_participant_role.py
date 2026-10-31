"""The participant role: a second diner account on its OWN iPhone.

User decision 2026-10-29: the full-event scenario (host + participant + 5 guests)
runs the participant on a third simulator, while every other flow stays on the two
it uses today. So the participant's iPhone is booted only by a flow that has a
participant segment, the readiness check never waits for it, it never shares a sim
with the consumer, and every C-App handler acts on the phone of the segment that
runs it.
"""
import json
import types

from automation.agentic import preflight
from automation.scenarios import cross_app_config as cfgmod
from automation.scenarios import cross_app_flows as caf
from automation.scenarios.cross_app_flows import FlowRunner

DEVICES = {"consumer": "HOST-PHONE", "participant": "GUEST-PHONE",
           "waiter": "IPAD", "kitchen": "IPAD"}


def runner(segments, devices=DEVICES, credentials=None):
    fr = FlowRunner.__new__(FlowRunner)
    fr.devices = dict(devices)
    fr.credentials = credentials or {}
    fr.flow = {"segments": [{"num": str(i + 1), "name": "", "role": r, "steps": []}
                            for i, r in enumerate(segments)]}
    return fr


def test_each_role_runs_on_its_own_simulator():
    fr = runner(["consumer", "participant", "waiter"])
    assert fr._role_udid("consumer") == "HOST-PHONE"
    assert fr._role_udid("participant") == "GUEST-PHONE"
    assert fr._role_udid("waiter") == fr._role_udid("kitchen") == "IPAD"


def test_c_app_handlers_act_on_the_phone_of_the_running_segment():
    fr = runner(["consumer", "participant"])
    fr._cur_role = "participant"
    assert fr._consumer_udid() == "GUEST-PHONE"
    fr._cur_role = "consumer"
    assert fr._consumer_udid() == "HOST-PHONE"
    del fr._cur_role                       # a handler called outside a segment
    assert fr._consumer_udid() == "HOST-PHONE"


def test_only_a_flow_with_a_participant_segment_uses_the_third_phone():
    assert runner(["consumer", "waiter", "kitchen"])._flow_udids() == ["HOST-PHONE", "IPAD"]
    assert runner(["consumer", "participant", "waiter"])._flow_udids() == \
        ["GUEST-PHONE", "HOST-PHONE", "IPAD"]


def test_participant_signs_in_with_its_own_login():
    creds = {"consumer": {"email": "host@x", "password": "p"}}
    fr = runner(["participant"], credentials=creds)
    fr._cur_role = "participant"
    notes = []
    assert FlowRunner._sign_in_consumer(fr, types.SimpleNamespace(d=None), notes) is False
    assert "no participant credentials" in notes[-1], "never the host's login on the guest phone"


def test_readiness_check_does_not_wait_for_the_participant_phone(monkeypatch):
    monkeypatch.setattr(cfgmod, "load_config", lambda: {"devices": DEVICES})
    booted = {"devices": {"rt": [{"udid": "HOST-PHONE"}, {"udid": "IPAD"}]}}
    monkeypatch.setattr(preflight.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(stdout=json.dumps(booted)))
    out = preflight._simulators()
    assert out["ok"], out
    assert out["detail"] == "2 booted"


def test_participant_never_shares_the_consumer_phone(monkeypatch):
    from automation.projects import simulators
    monkeypatch.setattr(simulators, "sdk_major", lambda: 26)
    sims = [
        {"udid": "PRO", "name": "iPhone 17 Pro", "state": "Booted", "ios": "26.5"},
        {"udid": "SEVENTEEN", "name": "iPhone 17", "state": "Shutdown", "ios": "26.5"},
        {"udid": "IPAD", "name": "iPad Pro 11-inch (M5)", "state": "Booted", "ios": "26.5"},
    ]
    out = cfgmod.localize_devices({"consumer": "PRO", "participant": "PRO",
                                   "waiter": "IPAD", "kitchen": "IPAD"}, sims=sims)
    assert out["consumer"] == "PRO"
    assert out["participant"] == "SEVENTEEN"
    foreign = cfgmod.localize_devices({"consumer": "PRO", "participant": "OTHER-MAC",
                                       "waiter": "IPAD", "kitchen": "IPAD"}, sims=sims)
    assert foreign["participant"] == "SEVENTEEN"


def test_participant_segments_count_as_consumer_side_in_results():
    assert "participant" in caf._CONSUMER_ROLES and "participant" in caf._ROLES
    assert "participant" in cfgmod.ROLES and cfgmod.ROLE_KIND["participant"] == "iPhone"
