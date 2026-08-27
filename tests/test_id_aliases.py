"""Tablet <-> phone accessibility-id aliases.

The SAME control has different ids in the two Business builds, measured in the app source:
    App/Screens/Event/OrderSummary.js:689        tablet 'selectAll'
    App/MobileScreens/Event/OrderSummary.js:726  phone  'selectAllItemsBtn'
    App/Screens/Event/OrderSummary.js:675        tablet 'unSelectAll'
    App/MobileScreens/Event/OrderSummary.js:718  phone  'unSelectItemsBtn'

Without aliasing, a scenario recorded on the iPad hunts a control the phone build does
not contain and burns its whole step timeout — which is what stopped the Business
scenarios from running on the phone at all.
"""
from automation.intelligence.scenario_runner import ScenarioRunner
from automation.scenarios.cross_app_flows import FlowRunner


def test_both_runners_share_one_map():
    """They used to hold separate copies, so a pair fixed in the cross-app flows stayed
    broken on the Scenarios page."""
    assert FlowRunner._ID_ALIASES is ScenarioRunner.ID_ALIASES


def test_the_tablet_and_phone_spellings_resolve_to_each_other():
    assert ScenarioRunner.id_candidates("selectAll") == ("selectAll", "selectAllItemsBtn")
    assert ScenarioRunner.id_candidates("selectAllItemsBtn") == ("selectAllItemsBtn", "selectAll")
    assert ScenarioRunner.id_candidates("unSelectAll") == ("unSelectAll", "unSelectItemsBtn")
    assert ScenarioRunner.id_candidates("unSelectItemsBtn") == ("unSelectItemsBtn", "unSelectAll")


def test_the_alias_map_is_symmetric():
    for k, vs in ScenarioRunner.ID_ALIASES.items():
        for v in vs:
            assert k in ScenarioRunner.ID_ALIASES.get(v, ()), f"{v} does not map back to {k}"


def test_an_id_the_same_in_both_builds_is_left_alone():
    # Verified present in BOTH App/Screens and App/MobileScreens.
    for shared in ("addNewEvent", "saveBtn", "anyBtn", "sendItemsBtn", "serveItemsBtn",
                   "notifyPaymentBtn", "addItemsBtn", "assignToBtn", "closeTableBtn",
                   "orderReadyBtn", "orderCloseBtn", "AssignTableBtn"):
        assert ScenarioRunner.id_candidates(shared) == (shared,)


def test_the_written_id_is_always_tried_first():
    """The alias is a fallback, never a substitution — on the device that DOES have the
    written id, aliasing must not send the tap somewhere else."""
    for ident in ScenarioRunner.ID_ALIASES:
        assert ScenarioRunner.id_candidates(ident)[0] == ident


def test_the_business_scenario_ids_are_all_resolvable_on_either_build():
    """Every id the five saved Business scenarios click is either shared or aliased."""
    used = ["addNewEvent", "saveBtn", "anyBtn", "signInBtn", "clickCheckBox",
            "AssignTableBtn", "addItemsBtn", "assignToBtn", "assignProductsBtn",
            "selectAll", "selectAllItemsBtn", "sendItemsBtn", "serveItemsBtn",
            "notifyPaymentBtn", "cashPaymentBtn", "paymentConfirmBtn", "closeTableBtn",
            "orderReadyBtn", "orderCloseBtn"]
    for ident in used:
        assert ScenarioRunner.id_candidates(ident)[0] == ident
