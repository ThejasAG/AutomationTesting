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


def test_different_cards_tied_on_the_hour_fail_loudly():
    # two DIFFERENT cards equally near the hour: guessing opens the wrong booking
    els = [_card("RoopaDcardReserved", 700), _card("RoopaDcardConfirmationPending", 900)]
    lbl, diag = F._choose_card(els, "roopad", RESERVED, 800, "RoopaD", "13:00")
    assert lbl is None
    assert "equally near 13:00" in diag


def test_identical_duplicates_in_one_row_open_the_first_and_say_so():
    # MEASURED on the phone: the board lays bookings side by side within an hour row —
    # two RoopaDReservedCard at y=1550 (x=80 and x=361), 12:00 row at cy=1556. They
    # differ in nothing observable, so refusing dead-ends the flow for no gain.
    els = [_card("RoopaDReservedCard", 800), _card("RoopaDReservedCard", 800)]
    lbl, diag = F._choose_card(els, "roopad", RESERVED, 800, "RoopaD", "12:00")
    assert lbl == "RoopaDReservedCard"
    assert "2 identical" in diag and "12:00" in diag


def test_no_hour_context_with_DIFFERENT_cards_is_ambiguous():
    els = [_card("RoopaDcardReserved", 300), _card("RoopaDcardConfirmationPending", 800)]
    lbl, diag = F._choose_card(els, "roopad", RESERVED, None, "RoopaD", "")
    assert lbl is None and "no booked hour to disambiguate" in diag


def test_no_hour_context_with_IDENTICAL_cards_opens_the_first():
    # same reasoning as the tied-on-the-hour case: nothing observable separates them
    els = [_card("RoopaDcardReserved", 300), _card("RoopaDcardReserved", 800)]
    lbl, diag = F._choose_card(els, "roopad", RESERVED, None, "RoopaD", "")
    assert lbl == "RoopaDcardReserved" and "2 identical" in diag


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


# ── card label conventions differ BY DEVICE ──────────────────────────────────
# iPad  : <Name>card<Status>   e.g. RoopaDcardReserved
# iPhone: <Name><Status>Card   e.g. RoopaDReservedCard   (from a real phone run)

def test_ipad_and_phone_label_forms_both_parse():
    assert F._split_card("RoopaDcardReserved") == ("RoopaD", "reserved")
    assert F._split_card("RoopaDReservedCard") == ("RoopaD", "reserved")


def test_multiword_status_beats_its_substring():
    assert F._split_card("RoopaDInProgressCard") == ("RoopaD", "inprogress")
    assert F._split_card("RoopaDConfirmationPendingCard") == ("RoopaD", "confirmationpending")


def test_name_is_not_eaten_by_a_greedy_split():
    # a naive '(.+?)([A-Z]\w*)Card' yields name='Roopa', status='DReserved'
    name, _ = F._split_card("RoopaDReservedCard")
    assert name == "RoopaD"


def test_phone_board_picks_the_diners_reserved_card():
    # the exact labels from the failing phone run
    els = [_card("RaajeshAnandavelExpiredCard", 300),
           _card("RoopaDExpiredCard", 400),
           _card("RaajeshAnandavelReservedCard", 700),
           _card("RoopaDReservedCard", 800)]
    lbl, diag = F._choose_card(els, "roopad", RESERVED, 800, "RoopaD", "12:00")
    assert lbl == "RoopaDReservedCard", diag
    assert diag == ""


def test_phone_board_still_refuses_another_diner():
    els = [_card("RaajeshAnandavelReservedCard", 700)]
    lbl, diag = F._choose_card(els, "roopad", RESERVED, 700, "RoopaD", "12:00")
    assert lbl is None and "someone else" in diag


# ── the same control has different ids in the tablet vs phone builds ─────────
# App/Screens/Event/OrderSummary.js      -> selectAll   / unSelectAll
# App/MobileScreens/Event/OrderSummary.js-> selectAllItemsBtn / unSelectItemsBtn
# The flows hardcode the phone spelling, so 'click selectAllItemsBtn' could never
# resolve on the iPad and burned its full 150s timeout.

def test_select_all_ids_are_treated_as_one_control():
    assert "selectAll" in F._id_candidates("selectAllItemsBtn")
    assert "selectAllItemsBtn" in F._id_candidates("selectAll")


def test_unselect_ids_are_treated_as_one_control():
    assert "unSelectItemsBtn" in F._id_candidates("unSelectAll")
    assert "unSelectAll" in F._id_candidates("unSelectItemsBtn")


def test_the_written_id_is_always_tried_first():
    assert F._id_candidates("selectAllItemsBtn")[0] == "selectAllItemsBtn"


def test_an_id_with_no_alias_is_unchanged():
    assert F._id_candidates("sendItemsBtn") == ("sendItemsBtn",)


# ── the EXPANDED LogBox viewer is a different shape from the toast ───────────
# toast : full-width 48pt strip, ✕ at its right edge
# viewer: full screen + a Dismiss/Minimize pair (measured: Dismiss (0,804,197,48))

def _viewer_els():
    return [_el("Log 8 of 30", 153, 52, 86, 21),
            _el("Console Warning", 12, 94, 369, 28),
            _el("Dismiss", 0, 804, 197, 48),
            _el("Minimize", 197, 804, 196, 48)]


def test_logbox_viewer_is_not_matched_by_the_toast_strip_rule():
    # Dismiss is 197 of 393 wide — nowhere near the full-width strip
    assert F._logbox_strip(_viewer_els()) is None


def test_viewer_needs_both_dismiss_and_minimize_to_be_identified():
    only_dismiss = [_el("Dismiss", 0, 804, 197, 48)]
    labels = {e["label"] for e in only_dismiss}
    assert not ({"Dismiss", "Minimize"} <= labels)
    assert {"Dismiss", "Minimize"} <= {e["label"] for e in _viewer_els()}


def test_viewer_is_collapsed_via_minimize_not_dismiss():
    # Dismiss closes ONE of ~28 stacked logs (header counts "Log 8 of 29" -> "8 of 28");
    # Minimize collapses the whole viewer in a single tap.
    import inspect
    from automation.scenarios.cross_app_flows import FlowRunner
    src = inspect.getsource(FlowRunner._dismiss_logbox_viewer)
    assert '== "Minimize"' in src
    assert '== "Dismiss"' not in src.split("btn =")[1]
