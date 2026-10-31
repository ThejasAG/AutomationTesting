import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app_knowledge_agent.scenario_generator import (
    _E2E_SPEC_PATH,
    _setup_segment_is_complete,
    _generate_e2e_phase,
    SpecPhase,
    generate_e2e_flow,
    parse_e2e_spec,
    write_e2e_flow,
)


@pytest.fixture
def complete_setup_steps():
    return [
        "tap Nylai Kitchen2",
        "tap Add Guest",
        "verify Guest contacts is visible",
        *(f"select Guest {number}" for number in range(1, 8)),
        "tap Invite",
        "drag duration slider to 1 hr",
        "select first available time slot 16:00",
        "tap bookAppoitment",
        "tap orderLater",
        "tap Home",
        "verify Home is visible",
    ]


def test_setup_requires_seven_guests_and_returns_home(complete_setup_steps):
    assert _setup_segment_is_complete([
        {"role": "consumer", "steps": complete_setup_steps},
    ])


def test_setup_rejects_path_that_does_not_end_home(complete_setup_steps):
    complete_setup_steps[-1] = "tap Wallet"
    assert not _setup_segment_is_complete([
        {"role": "consumer", "steps": complete_setup_steps},
    ])


def test_setup_rejects_missing_guest_selection(complete_setup_steps):
    complete_setup_steps.remove("select Guest 7")
    assert not _setup_segment_is_complete([
        {"role": "consumer", "steps": complete_setup_steps},
    ])


def test_setup_accepts_equivalent_button_and_verification_wording(complete_setup_steps):
    complete_setup_steps[complete_setup_steps.index(
        "select first available time slot 16:00")] = "select first available slot at 16:00"
    complete_setup_steps[complete_setup_steps.index("tap orderLater")] = "press ORDER LATER"
    complete_setup_steps[-1] = "ensure Home is visible"
    assert _setup_segment_is_complete([
        {"role": "consumer", "steps": complete_setup_steps},
    ])


def test_invalid_setup_retry_includes_specific_correction(complete_setup_steps):
    invalid_steps = list(complete_setup_steps)
    invalid_steps.remove("select Guest 7")

    class Provider:
        prompts = []

        def generate(self, *, user_prompt, **kwargs):
            self.prompts.append(user_prompt)
            steps = invalid_steps if len(self.prompts) == 1 else complete_setup_steps
            return SimpleNamespace(content=json.dumps({
                "segments": [{"role": "consumer", "steps": steps}],
            }))

    provider = Provider()
    phase = SpecPhase(0, "Test Setup", "Create the booking", ("consumer",))
    result = _generate_e2e_phase(
        phase,
        {"setup": "Host books a table", "checklist": []},
        {"consumer": {"screens": []}},
        "",
        provider,
    )

    assert result is not None
    assert len(provider.prompts) == 2
    assert "RETRY CORRECTION" in provider.prompts[1]


def test_setup_spec_matches_the_required_party_size_and_booking_order():
    spec = parse_e2e_spec(_E2E_SPEC_PATH.read_text(encoding="utf-8"))
    setup = spec["phases"][0]
    assert setup.number == 0
    assert "exactly seven" in setup.body
    assert "one participant, and six additional guests" in setup.body
    assert setup.body.index("seven") < setup.body.index("Invite")


def test_partial_mode_cannot_skip_a_failed_setup_phase(monkeypatch):
    import app_knowledge_agent.scenario_generator as generator

    monkeypatch.setattr(generator, "_generate_e2e_phase", lambda *args: None)
    spec = {
        "title": "Cross-app setup",
        "phases": [SpecPhase(0, "Test Setup", "", ("consumer",))],
        "checklist": [],
    }
    assert generate_e2e_flow(spec, {"screens": []}, object(), allow_partial=True) is None


def test_write_e2e_flow_creates_a_variant_and_preserves_existing_flow():
    base_name = "End-to-End Automation Test Flow"
    existing = SimpleNamespace(
        name=base_name,
        description="Original scenario",
        segments=[{"steps": ["original"]}],
    )
    query = Mock()
    query.filter.return_value.first.side_effect = [existing, None]
    db = Mock()
    db.query.return_value = query
    flow = {
        "name": base_name,
        "description": "Generated scenario",
        "segments": [{"role": "consumer", "steps": ["tap Home"]}],
    }

    assert write_e2e_flow(db, flow) == 1

    created = db.add.call_args.args[0]
    assert created.name == f"{base_name} (Variant 2)"
    assert created.id.endswith("_v2")
    assert existing.name == base_name
    assert existing.description == "Original scenario"
    db.commit.assert_called_once_with()