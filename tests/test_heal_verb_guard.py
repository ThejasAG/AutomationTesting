"""A self-heal must never swap the action verb of the control it lands on.

The real failure this guards, from the waiter segment of the CASH flow:

    click selectAllItemsBtn — tapped selectAll by id
    click serveItemsBtn     — healed → tapped "addItemsBtn"
    click notifyPaymentBtn  — timed out after 240s (step hung; aborting segment)

`serveItemsBtn` renders only once items are selected (OrderSummary.js guards it
with `servedData.length !== 0 && present`). It was missing for a moment, so the
fuzzy resolver healed it — and difflib scores "serveitemsbtn" against
"additemsbtn" at 0.667, over the 0.62 heal threshold, because both end in the
boilerplate "ItemsBtn". The heal opened the ADD NEW ITEM sheet, a modal. Every
later step then ran against that sheet, and notifyPaymentBtn burned the whole
240s step timeout behind it.

Serving is not adding. The verb IS the intent, so a match that swaps it is a
different button rather than a drifted one.
"""
import difflib

from automation.intelligence.scenario_runner import _verb_group


def _blocked(want: str, candidate: str) -> bool:
    """What _fuzzy_resolve's guard decides for this pair."""
    wv, gv = _verb_group(want), _verb_group(candidate)
    return wv is not None and gv is not None and gv is not wv


# ── the exact pair that broke the run ────────────────────────────────────────
def test_the_original_mis_heal_would_now_be_refused():
    assert _blocked("serveItemsBtn", "addItemsBtn")


def test_that_pair_really_does_clear_the_fuzzy_threshold():
    # Proves the guard is load-bearing: without it, score alone lets this through.
    score = difflib.SequenceMatcher(None, "serveitemsbtn", "additemsbtn").ratio()
    assert score > 0.62


# ── other opposites on these screens ─────────────────────────────────────────
def test_select_never_heals_to_unselect():
    assert _blocked("selectAllItemsBtn", "unSelectAll")


def test_close_never_heals_to_open():
    assert _blocked("closeTableBtn", "openTableBtn")


def test_notify_never_heals_to_add():
    assert _blocked("notifyPaymentBtn", "addItemsBtn")


def test_confirm_never_heals_to_cancel():
    assert _blocked("confirmBtn", "cancelBtn")


# ── ordinary drift must still heal ───────────────────────────────────────────
# The guard exists to stop WRONG heals, not to stop healing. These are the cases
# the feature was built for and they must keep working.
def test_same_verb_still_heals_across_a_renamed_suffix():
    assert not _blocked("serveItemsBtn", "serveBtn")


def test_the_real_tablet_phone_alias_still_heals():
    assert not _blocked("selectAllItemsBtn", "selectAll")


def test_synonyms_inside_one_group_stay_interchangeable():
    assert not _blocked("sendItemsBtn", "submitItemsBtn")


def test_an_unverbed_label_is_left_alone():
    # "SERVE" is the button's visible text; nothing to contradict, so allow it.
    assert not _blocked("serveItemsBtn", "SERVE")


def test_a_step_with_no_verb_never_blocks_anything():
    assert not _blocked("homeBtn", "addItemsBtn")
