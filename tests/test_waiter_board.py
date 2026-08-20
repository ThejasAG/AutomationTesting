"""Two SEPARATE defects on the waiter bookings board.

A: a coordinate tap on addNewEvent lands on whatever overlay is on top of it.
B: the reservation card locator breaks when the status suffix moves, and used to
   fall back to a DIFFERENT customer's card.

Frames below are the real measured ones from the iPad/iPhone in this fleet.
"""
from automation.scenarios.cross_app_flows import FlowRunner as F


def _el(label, x, y, w, h, typ="GenericElement", eid=""):
    return {"id": eid, "label": label, "type": typ, "x": x, "y": y, "w": w, "h": h,
            "cx": int(x + w / 2), "cy": int(y + h / 2)}


APP_IPAD = _el("STG-VyaBusiness", 0, 0, 1210, 834, "Application")
FAB = _el("addNewEvent", 700, 698, 50, 50)
# measured: full-width 48pt strip, and its text contains none of the old keywords
TOAST_IPAD = _el('6 Each child in a list should have a unique "key" prop.', 10, 708, 1190, 48)
TOAST_IPHONE = _el("8 Possible Unhandled Promise Rejection (id: 0):", 10, 801, 382, 48)
APP_IPHONE = _el("STG-VyaConsumer", 0, 0, 402, 874, "Application")


# ── A: overlay detection ─────────────────────────────────────────────────────
def test_logbox_strip_found_by_geometry_on_ipad():
    assert F._logbox_strip([APP_IPAD, FAB, TOAST_IPAD]) is TOAST_IPAD


def test_logbox_strip_found_by_geometry_on_iphone():
    assert F._logbox_strip([APP_IPHONE, TOAST_IPHONE]) is TOAST_IPHONE


def test_real_controls_are_not_mistaken_for_a_toast():
    assert F._logbox_strip([APP_IPAD, FAB, _el("homeBtn", 0, 100, 60, 48)]) is None


def test_toast_over_the_fab_is_detected_as_occluding():
    # REAL device order: idb lists the toast BEFORE addNewEvent even though the
    # toast is what receives the tap. Relying on "front-most last" missed this.
    els = [APP_IPAD, TOAST_IPAD, FAB]
    assert F._occluding(FAB, els) is TOAST_IPAD


def test_toast_detected_regardless_of_list_position():
    for els in ([APP_IPAD, TOAST_IPAD, FAB], [APP_IPAD, FAB, TOAST_IPAD]):
        assert F._occluding(FAB, els) is TOAST_IPAD


def test_toast_that_does_not_cover_the_fab_is_not_occluding():
    # the toast's y varies between launches; only flag it when it actually overlaps
    low = _el("6 Possible unhandled promise rejection", 10, 60, 1190, 48)
    assert F._occluding(FAB, [APP_IPAD, low, FAB]) is None


def test_booking_card_over_the_fab_is_detected_as_occluding():
    card = _el("SnehaGunagacardReserved", 700, 716, 492, 138)
    assert F._occluding(FAB, [APP_IPAD, FAB, card]) is card


def test_unobstructed_fab_is_not_occluded():
    assert F._occluding(FAB, [APP_IPAD, FAB]) is None


def test_full_screen_container_is_not_treated_as_an_overlay():
    # the Application frame contains every point; it is an ancestor, not an overlay
    assert F._occluding(FAB, [FAB, APP_IPAD]) is None


# ── B: status-resilient card selection ───────────────────────────────────────
# Real labels observed on the board.
def _card(label, cy, w=492):
    return _el(label, 700, cy - 69, w, 138)


RESERVED = ("reserved", "confirmationpending")


def test_picks_the_diners_reserved_card():
    els = [_card("RoopaDcardReserved", 800), _card("SnehaGunagacardReserved", 400)]
    lbl, diag = F._choose_card(els, "roopad", RESERVED, 800, "RoopaD", "13:00")
    assert lbl == "RoopaDcardReserved" and diag == ""


def test_never_opens_another_customers_card():
    # the diner has NO assignable card; the old code fell through to someone else's
    els = [_card("SnehaGunagacardReserved", 400), _card("RaajeshAnandavelcardReserved", 600)]
    lbl, diag = F._choose_card(els, "roopad", RESERVED, 800, "RoopaD", "13:00")
    assert lbl is None
    assert "refusing to open someone else's reservation" in diag


def test_status_moved_on_is_reported_not_guessed():
    # RoopaD is on the board, but Reserved -> InProgress happened mid-flow
    els = [_card("RoopaDcardInProgress", 800), _card("RoopaDcardCompleted", 200)]
    lbl, diag = F._choose_card(els, "roopad", RESERVED, 800, "RoopaD", "13:00")
    assert lbl is None
    assert "none in reserved/confirmationpending" in diag
    assert "RoopaDcardInProgress (status=inprogress)" in diag


def test_serve_flow_finds_the_inprogress_card_after_transition():
    # @open_order passes ('inprogress',) — the SAME booking, later in its life
    els = [_card("RoopaDcardInProgress", 800)]
    lbl, _ = F._choose_card(els, "roopad", ("inprogress",), 800, "RoopaD", "13:00")
    assert lbl == "RoopaDcardInProgress"


def test_booked_hour_disambiguates_duplicate_customer_cards():
    els = [_card("RoopaDcardReserved", 300), _card("RoopaDcardReserved", 800)]
    lbl, diag = F._choose_card(els, "roopad", RESERVED, 790, "RoopaD", "13:00")
    assert lbl == "RoopaDcardReserved" and diag == ""


def test_ambiguous_duplicates_fail_loudly_instead_of_guessing():
    # two equally-near cards: guessing opens the wrong reservation
    els = [_card("RoopaDcardReserved", 700), _card("RoopaDcardReserved", 900)]
    lbl, diag = F._choose_card(els, "roopad", RESERVED, 800, "RoopaD", "13:00")
    assert lbl is None
    assert "equally near 13:00" in diag


def test_no_hour_context_with_duplicates_is_ambiguous():
    els = [_card("RoopaDcardReserved", 300), _card("RoopaDcardReserved", 800)]
    lbl, diag = F._choose_card(els, "roopad", RESERVED, None, "RoopaD", "")
    assert lbl is None and "no booked hour to disambiguate" in diag


def test_horizontally_scrolled_narrow_cards_are_still_skipped():
    els = [_el("RoopaDcardReserved", 700, 731, 40, 138)]      # w<=60
    assert F._choose_card(els, "roopad", RESERVED, 800, "RoopaD", "13:00")[0] is None


# ── Select A Table sheet detection ───────────────────────────────────────────
# Before the app-side accessibility fix the sheet collapsed into ONE element
# whose label concatenated everything, so matching the title text was enough.
# After the fix the sheet is decomposed and idb does not always report the title
# — measured on run 3a040e54 seg 4, where the tree held the chips and Apply and
# no title, the sheet went undetected, and it hid the bookings board.

def test_sheet_detected_by_its_controls_when_the_title_is_absent():
    els = [_el("tableChipI1", 402, 378, 50, 42), _el("applyTableBtn", 417, 481, 376, 64)]
    assert F._table_modal_in(els) is True


def test_sheet_detected_by_apply_alone():
    assert F._table_modal_in([_el("applyTableBtn", 417, 481, 376, 64)]) is True


def test_sheet_detected_by_the_legacy_flattened_label():
    # pre-fix builds still report the concatenated single element
    flat = _el("Select A Table I1 I2 O1 O2 O3 O4", 0, 0, 1210, 834)
    assert F._table_modal_in([flat]) is True


def test_sheet_detected_by_its_title_alone():
    assert F._table_modal_in([_el("Select A Table", 354, 261, 502, 56)]) is True


def test_bookings_board_is_not_mistaken_for_the_sheet():
    board = [_el("addNewEvent", 700, 698, 50, 50), _el("homeBtn", 0, 100, 60, 48),
             _el("RoopaDcardReserved", 700, 716, 492, 138), _el("modifyTable", 1014, 103, 56, 36)]
    assert F._table_modal_in(board) is False
